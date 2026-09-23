"""Reusable JWT helpers.

Deliberately free of FastAPI imports so any layer (routes, services, workers,
scripts) can create and read tokens. Failures raise :class:`TokenError`; it is
the caller's job to turn that into an HTTP response.
"""

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt import PyJWTError

from app.core.config import settings


class TokenType(str, enum.Enum):
    ACCESS = "access"
    REFRESH = "refresh"


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or of the wrong type."""


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int  # access-token lifetime, in seconds
    token_type: str = "bearer"


def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        **(extra_claims or {}),
        "sub": str(subject),
        "type": token_type.value,
        "iat": now,
        "nbf": now,
        "exp": now + expires_delta,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(
    subject: str,
    extra_claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Short-lived token sent as `Authorization: Bearer <token>`."""
    return _create_token(
        subject,
        TokenType.ACCESS,
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        extra_claims,
    )


def create_refresh_token(
    subject: str,
    extra_claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Long-lived token, exchanged for a fresh access token."""
    return _create_token(
        subject,
        TokenType.REFRESH,
        expires_delta or timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        extra_claims,
    )


def create_token_pair(
    subject: str, extra_claims: dict[str, Any] | None = None
) -> TokenPair:
    """Build both tokens at once — what signup and login hand back."""
    return TokenPair(
        access_token=create_access_token(subject, extra_claims),
        refresh_token=create_refresh_token(subject, extra_claims),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def decode_token(
    token: str, expected_type: TokenType | None = None
) -> dict[str, Any]:
    """Verify a token's signature and expiry and return its claims.

    Raises :class:`TokenError` if the token is invalid, expired, or is not of
    ``expected_type`` (an access token cannot be used where a refresh token is
    required, and vice versa).
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub", "type"]},
        )
    except PyJWTError as exc:
        raise TokenError(f"Could not validate token: {exc}") from exc

    if expected_type is not None and payload.get("type") != expected_type.value:
        raise TokenError(
            f"Expected a {expected_type.value} token, got {payload.get('type')!r}."
        )
    return payload


def decode_access_token(token: str) -> dict[str, Any]:
    return decode_token(token, expected_type=TokenType.ACCESS)


def decode_refresh_token(token: str) -> dict[str, Any]:
    return decode_token(token, expected_type=TokenType.REFRESH)
