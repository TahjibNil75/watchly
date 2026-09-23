from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import get_auth_service
from app.auth.schemas import (
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
from app.db.models.user import User
from app.schemas.user import UserRead
from app.utils.jwt import create_token_pair

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_tokens(user: User) -> TokenResponse:
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

    return SignupResponse(user=UserRead.model_validate(user), tokens=_issue_tokens(user))


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
    """Authenticate against `username` or `email` and return a token pair."""
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

    return LoginResponse(user=UserRead.model_validate(user), tokens=_issue_tokens(user))
