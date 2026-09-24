"""Reusable FastAPI dependencies for protected endpoints."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import AuthService
from app.core.permissions import INVITERS, ROLE_MANAGERS, USER_MANAGERS
from app.db.models.user import User, UserRole
from app.db.session import get_db
from app.utils.jwt import TokenError, decode_access_token

bearer_scheme = HTTPBearer(auto_error=False, description="Access token")


def get_auth_service(session: AsyncSession = Depends(get_db)) -> AuthService:
    return AuthService(session)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, Any]:
    """Validate the `Authorization: Bearer <access token>` header.

    Checks the signature and expiry and rejects refresh tokens. Tokens are
    stateless: there is no server-side revocation, so a token stays valid until
    it expires even after the client discards it.
    """
    if credentials is None:
        raise _unauthorized("Not authenticated.")

    try:
        return decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc


async def get_authenticated_user(
    claims: dict[str, Any] = Depends(get_current_claims),
    service: AuthService = Depends(get_auth_service),
) -> User:
    """The authenticated, active user behind the request, even one who still
    has to replace a temporary password. Only the endpoints that let them do
    that should use this; everything else uses :func:`get_current_user`."""
    user = await service.get_user_by_id(int(claims["sub"]))
    if user is None:
        # Token is validly signed but the account is gone.
        raise _unauthorized("User no longer exists.")
    if not user.is_active:
        # A suspension takes hold here, on the very next request, rather than
        # waiting for the suspended user's access token to expire.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been suspended.",
        )
    return user


async def get_current_user(user: User = Depends(get_authenticated_user)) -> User:
    """The authenticated, active user behind the request.

    Someone who signed in with a temporary password is refused until they
    choose a new one: the temporary password travelled by email, so it only
    buys the right to replace it.
    """
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You signed in with a temporary password. Choose a new password "
                "before doing anything else."
            ),
        )
    return user


def require_roles(*allowed: UserRole) -> Callable[..., Awaitable[User]]:
    """Build a dependency that admits only the given roles.

    The role is read from the database via :func:`get_current_user`, never from
    the token's `role` claim — so a demotion takes effect on the next request
    instead of waiting for the old access token to expire.
    """

    async def dependency(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )
        return current_user

    return dependency


#: Guard for endpoints that change other users' roles — admin and DevOps only.
require_role_manager = require_roles(*ROLE_MANAGERS)

#: Guard for the user directory and suspension endpoints. Coarse on purpose:
#: it admits every role with *some* management power, and the service layer
#: decides whether this actor may act on this particular target.
require_user_manager = require_roles(*USER_MANAGERS)

#: Guard for the invitation endpoints. Coarse like the two above: which roles
#: this actor may actually hand out is decided in the service layer.
require_inviter = require_roles(*INVITERS)
