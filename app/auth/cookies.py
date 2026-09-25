"""The refresh-token cookie. Its own module so that routes elsewhere (a password
change starts a new session) can set it without importing the auth router."""

from fastapi import Response

from app.core.config import settings

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
