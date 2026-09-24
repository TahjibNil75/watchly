from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
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
    UserAlreadyExistsError,
)
from app.core.config import settings
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.user import UserRead
from app.user.schemas import EmailConfirmRequest
from app.user.service import EmailLinkExpiredError, EmailLinkNotFoundError, UserService
from app.utils.jwt import create_token_pair

router = APIRouter(prefix="/auth", tags=["auth"])


def issue_tokens(user: User) -> TokenResponse:
    pair = create_token_pair(
        subject=str(user.id),
        extra_claims={"username": user.username, "role": user.role.value},
    )
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
    )


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
    responses={409: {"description": "Email or username already registered"}},
)
async def signup(
    payload: SignupRequest,
    service: AuthService = Depends(get_auth_service),
) -> SignupResponse:
    """Create a user with the `Viewer` role and return an access/refresh pair."""
    try:
        user = await service.signup(payload)
    except UserAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return SignupResponse(user=UserRead.model_validate(user), tokens=issue_tokens(user))


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
    service: AuthService = Depends(get_auth_service),
) -> LoginResponse:
    """Authenticate against `username` or `email` and return a token pair.

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

    return LoginResponse(user=UserRead.model_validate(user), tokens=issue_tokens(user))


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
