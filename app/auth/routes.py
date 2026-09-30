import math
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    Depends,
    HTTPException,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.cookies import (
    REFRESH_COOKIE,
    clear_refresh_cookie,
    set_refresh_cookie,
)
from app.auth.dependencies import get_auth_service
from app.auth.mail import send_temporary_password, temporary_password_email
from app.auth.schemas import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LoginResponse,
    SignupRequest,
    SignupResponse,
    SignupStatus,
    TokenResponse,
)
from app.auth.service import (
    AccountLockedError,
    AuthService,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    SignupClosedError,
    UserAlreadyExistsError,
)
from app.core.config import settings
from app.core.rate_limit import RATE_LIMITED, rate_limit
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.user import UserRead
from app.user.schemas import EmailConfirmRequest
from app.user.service import EmailLinkExpiredError, EmailLinkNotFoundError, UserService
from app.utils.jwt import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


def access_token_for(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(
            subject=str(user.id),
            extra_claims={
                "username": user.username,
                "role": user.role.value,
                "sv": user.session_version,
            },
        ),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def issue_tokens(
    user: User, service: AuthService, response: Response
) -> TokenResponse:
    """Sign `user` in: start a session, put its refresh token in the cookie,
    and return an access token for the body."""
    set_refresh_cookie(response, await service.start_session(user))
    return access_token_for(user)


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register the first (admin) account",
    responses={
        403: {"description": "The admin already exists; people join by invitation"},
        409: {"description": "Email or username already registered"},
        **RATE_LIMITED,
    },
    dependencies=[rate_limit("signup")],
)
async def signup(
    payload: SignupRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> SignupResponse:
    """Create the first account on a fresh install as `Admin` and sign them
    in: an access token in the body, and the refresh token in an httpOnly
    cookie. Whoever installs the app signs up to administer it.

    Once that account exists every signup gets `403`: people join by accepting
    an invitation from an Admin or DevOps user.

    Each client may try `RATE_LIMIT_SIGNUP` times (`429` past that)."""
    try:
        user = await service.signup(payload)
    except SignupClosedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except UserAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return SignupResponse(
        user=UserRead.model_validate(user),
        tokens=await issue_tokens(user, service, response),
    )


@router.get(
    "/signup",
    response_model=SignupStatus,
    summary="Whether signup is open (only before the admin exists)",
)
async def signup_status(
    service: AuthService = Depends(get_auth_service),
) -> SignupStatus:
    """Tells the sign-in pages whether to offer "Create an account". Public."""
    return SignupStatus(open=await service.signup_open())


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Log in with a username or email",
    responses={
        401: {"description": "Incorrect username/email or password"},
        403: {"description": "Account is inactive"},
        429: {
            "description": "Too many wrong passwords, so sign-in is locked for now; "
            "or too many attempts from this client. Retry after `Retry-After` seconds"
        },
    },
    dependencies=[rate_limit("login")],
)
async def login(
    payload: LoginRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> LoginResponse:
    """Authenticate against `username` or `email`. Returns an access token and
    sets the refresh token as an httpOnly cookie; see `POST /auth/refresh`.

    A temporary password from `POST /auth/forgot-password` works here too. The
    user then comes back with `must_change_password: true`, and may do nothing
    but `POST /users/me/password` until they have chosen a new one.

    `MAX_FAILED_LOGIN_ATTEMPTS` wrong passwords in a row lock sign-in for
    `LOGIN_LOCKOUT_MINUTES` (`429`, with `Retry-After`), whatever password is
    sent — except a temporary password, which still signs in.

    Separately, each client may try `RATE_LIMIT_LOGIN` times, right or wrong,
    whichever accounts (`429` past that), so it cannot try a few passwords on
    every account instead.
    """
    try:
        user = await service.authenticate(payload.identifier, payload.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except AccountLockedError as exc:
        seconds = (exc.until - datetime.now(UTC)).total_seconds()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(max(1, math.ceil(seconds)))},
        ) from exc
    except InactiveUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc

    return LoginResponse(
        user=UserRead.model_validate(user),
        tokens=await issue_tokens(user, service, response),
    )


def _refresh_refused(status_code: int, detail: str) -> JSONResponse:
    # Returned rather than raised: an HTTPException would drop the cookie
    # cleared here, along with everything else set on the response.
    refused = JSONResponse(
        {"detail": detail},
        status_code=status_code,
        headers={"WWW-Authenticate": "Bearer"},
    )
    clear_refresh_cookie(refused)
    return refused


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Trade the refresh cookie for a new access token",
    responses={
        401: {"description": "No session, or it has expired or been revoked"},
        403: {"description": "Account is inactive"},
    },
)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    service: AuthService = Depends(get_auth_service),
) -> TokenResponse | JSONResponse:
    """Return a new access token for the session in the refresh cookie, and
    replace the cookie: each refresh token works once.

    Presenting one that was already used means someone else has a copy, so the
    whole session is revoked and everyone holding it has to sign in again. A
    client must therefore never send two refreshes at once. On any failure the
    cookie is cleared.
    """
    try:
        if not refresh_token:
            raise InvalidRefreshTokenError
        user, successor = await service.rotate_refresh_token(refresh_token)
    except InvalidRefreshTokenError as exc:
        return _refresh_refused(status.HTTP_401_UNAUTHORIZED, str(exc))
    except InactiveUserError as exc:
        return _refresh_refused(status.HTTP_403_FORBIDDEN, str(exc))

    set_refresh_cookie(response, successor)
    return access_token_for(user)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out: revoke the session in the refresh cookie",
)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    service: AuthService = Depends(get_auth_service),
) -> None:
    """Revoke the refresh cookie's session and clear the cookie. Always `204`,
    signed in or not.

    This device's access token works until it expires
    (`ACCESS_TOKEN_EXPIRE_MINUTES`); the client should discard it. Ending
    *every* session (password change, suspension) revokes access tokens too.
    """
    if refresh_token:
        await service.end_session(refresh_token)
    clear_refresh_cookie(response)


@router.post(
    "/confirm-email",
    response_model=UserRead,
    summary="Confirm a new email address from its emailed link",
    responses={
        403: {"description": "The account has been suspended"},
        404: {"description": "No pending change matches this token"},
        409: {"description": "Another account took the address meanwhile"},
        410: {"description": "The link has expired"},
        **RATE_LIMITED,
    },
    dependencies=[rate_limit("email_links")],
)
async def confirm_email(
    payload: EmailConfirmRequest,
    session: AsyncSession = Depends(get_db),
) -> UserRead:
    """Finish an email change started with `POST /users/me/email`. No sign-in
    needed: the token was sent only to the new address, so holding it proves
    the address is yours. The link works once.

    Each client may try `RATE_LIMIT_EMAIL_LINKS` tokens, shared with the
    invitee's endpoints (`429` past that)."""
    try:
        user = await UserService(session).confirm_email_change(payload.token)
    except EmailLinkNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except EmailLinkExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, str(exc)) from exc
    except InactiveUserError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except UserAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return UserRead.model_validate(user)


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a temporary password",
    responses=RATE_LIMITED,
    dependencies=[rate_limit("forgot_password")],
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    background: BackgroundTasks,
    service: AuthService = Depends(get_auth_service),
) -> ForgotPasswordResponse:
    """Email the account with this address a temporary password to sign in with.
    Signing in with it leads straight to choosing a new password.

    Always `202` with the same answer, whether or not the address has an
    account, so the endpoint cannot be used to find out who does. The email
    goes out after the response for the same reason: waiting on the mail
    server would make the two cases take different times.

    The current password keeps working until the temporary one is used, so
    asking for someone else cannot lock them out. The temporary password lasts
    `TEMP_PASSWORD_EXPIRE_MINUTES`; a second request within a minute is ignored.
    Each client may ask `RATE_LIMIT_FORGOT_PASSWORD` times, whichever addresses
    (`429` past that), so the form cannot be used to mail a list of inboxes.
    """
    issued = await service.issue_temporary_password(payload.email)
    if issued is not None:
        user, password = issued
        background.add_task(
            send_temporary_password, temporary_password_email(user, password), user.id
        )
    minutes = settings.TEMP_PASSWORD_EXPIRE_MINUTES
    return ForgotPasswordResponse(
        detail=(
            "If an account uses that address, a temporary password is on its way. "
            f"It works for {minutes} minutes."
        )
    )
