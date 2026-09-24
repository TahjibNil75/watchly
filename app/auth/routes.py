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

from app.auth.dependencies import get_auth_service
from app.auth.mail import send_temporary_password, temporary_password_email
from app.auth.schemas import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LoginResponse,
    SignupRequest,
    SignupResponse,
    TokenResponse,
)
from app.auth.service import (
    AuthService,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    UserAlreadyExistsError,
)
from app.core.config import settings
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.user import UserRead
from app.user.schemas import EmailConfirmRequest
from app.user.service import EmailLinkExpiredError, EmailLinkNotFoundError, UserService
from app.utils.jwt import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


#: httpOnly, so the page's scripts (and any injected into it) cannot read it,
#: and scoped to /auth, so it is only sent to be refreshed or revoked.
REFRESH_COOKIE = "watchly_refresh"
REFRESH_COOKIE_PATH = f"{settings.API_V1_PREFIX}/auth"


def set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        path=REFRESH_COOKIE_PATH,
        secure=settings.REFRESH_COOKIE_SECURE,
        httponly=True,
        # Never sent on a request started by another site, which is what keeps
        # /refresh and /logout safe from cross-site request forgery.
        samesite="strict",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        secure=settings.REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


def access_token_for(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(
            subject=str(user.id),
            extra_claims={"username": user.username, "role": user.role.value},
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
    summary="Register a new account",
    responses={409: {"description": "Email or username already registered"}},
)
async def signup(
    payload: SignupRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> SignupResponse:
    """Create a user with the `Viewer` role and sign them in: an access token
    in the body, and the refresh token in an httpOnly cookie."""
    try:
        user = await service.signup(payload)
    except UserAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return SignupResponse(
        user=UserRead.model_validate(user),
        tokens=await issue_tokens(user, service, response),
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Log in with a username or email",
    responses={
        401: {"description": "Incorrect username/email or password"},
        403: {"description": "Account is inactive"},
    },
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
    """
    try:
        user = await service.authenticate(payload.identifier, payload.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
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

    The access token cannot be revoked and works until it expires
    (`ACCESS_TOKEN_EXPIRE_MINUTES`); the client should discard it.
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
    },
)
async def confirm_email(
    payload: EmailConfirmRequest,
    session: AsyncSession = Depends(get_db),
) -> UserRead:
    """Finish an email change started with `POST /users/me/email`. No sign-in
    needed: the token was sent only to the new address, so holding it proves
    the address is yours. The link works once."""
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
