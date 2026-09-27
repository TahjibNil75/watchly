"""Small helpers shared by the test modules. Imported by conftest.py only after
it has pinned the environment, so importing `app` here is safe."""

from datetime import UTC, datetime

import httpx
from sqlalchemy import select, update

from app.auth.cookies import REFRESH_COOKIE
from app.db.models.refresh_token import RefreshToken
from app.db.models.user import User
from app.db.session import AsyncSessionLocal

API = "/api/v1"
AUTH = f"{API}/auth"
ME = f"{API}/users/me"
USERS = f"{API}/users"

#: The address the default test client connects from (TEST-NET-3).
CLIENT_IP = "203.0.113.10"

PASSWORD = "Correct-Horse-9"
NEW_PASSWORD = "Brand-New-Pass-7"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def login(
    client: httpx.AsyncClient, identifier: str, password: str = PASSWORD
) -> httpx.Response:
    return await client.post(
        f"{AUTH}/login", json={"identifier": identifier, "password": password}
    )


async def sign_in(
    client: httpx.AsyncClient, identifier: str, password: str = PASSWORD
) -> str:
    """Log in, expecting it to work, and return the access token. The refresh
    cookie lands in `client`'s jar."""
    response = await login(client, identifier, password)
    assert response.status_code == 200, response.text
    return response.json()["tokens"]["access_token"]


def refresh_cookie(client: httpx.AsyncClient) -> str | None:
    return client.cookies.get(REFRESH_COOKIE)


async def refresh_with(client: httpx.AsyncClient, token: str) -> httpx.Response:
    """POST /auth/refresh presenting `token` rather than whatever is in
    `client`'s jar: a copy of a cookie, replayed."""
    return await client.post(
        f"{AUTH}/refresh", headers={"Cookie": f"{REFRESH_COOKIE}={token}"}
    )


def clears_refresh_cookie(response: httpx.Response) -> bool:
    """Whether `response` tells the browser to drop the refresh cookie."""
    return any(
        header.startswith(f"{REFRESH_COOKIE}=") and "max-age=0" in header.lower()
        for header in response.headers.get_list("set-cookie")
    )


async def load_user(user_id: int) -> User:
    """The user as the database has it now."""
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return user


async def update_user(user_id: int, **values: object) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(update(User).where(User.id == user_id).values(**values))
        await session.commit()


async def refresh_tokens_of(user_id: int) -> list[RefreshToken]:
    async with AsyncSessionLocal() as session:
        return list(
            await session.scalars(
                select(RefreshToken)
                .where(RefreshToken.user_id == user_id)
                .order_by(RefreshToken.id)
            )
        )


def utcnow() -> datetime:
    return datetime.now(UTC)
