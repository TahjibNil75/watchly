"""Shared fixtures: a throwaway Postgres database and HTTP clients for the app.

The auth code leans on Postgres itself (advisory locks, ON CONFLICT upserts,
SELECT ... FOR UPDATE), so the tests run against a real server rather than
SQLite: the one `docker compose up -d db` starts, in a database of its own
(`watchly_test`, or TEST_POSTGRES_DB), created and migrated on first use and
emptied before every test that touches it.

Settings are read once, when `app.core.config` is first imported, so everything
the tests depend on is pinned in the environment here, before any `app` import,
overriding whatever .env says. That includes blanking SMTP_HOST: a test must
never email anyone.
"""

import os
import subprocess
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import bcrypt

ROOT = Path(__file__).resolve().parent.parent

TEST_DB = os.environ.get("TEST_POSTGRES_DB", "watchly_test")
if not TEST_DB.endswith("_test"):
    # Every table in it is emptied before each test.
    raise RuntimeError(f"Refusing to test against {TEST_DB!r}: its name must end in _test.")

os.environ.update(
    POSTGRES_DB=TEST_DB,
    SECRET_KEY="test-secret-key-0123456789abcdef0123456789abcdef",
    JWT_ALGORITHM="HS256",
    ACCESS_TOKEN_EXPIRE_MINUTES="30",
    REFRESH_TOKEN_EXPIRE_DAYS="7",
    REFRESH_COOKIE_SECURE="true",
    ALLOW_PUBLIC_SIGNUP="true",
    TEMP_PASSWORD_EXPIRE_MINUTES="60",
    MAX_FAILED_LOGIN_ATTEMPTS="5",
    LOGIN_LOCKOUT_MINUTES="15",
    RATE_LIMIT_ENABLED="true",
    RATE_LIMIT_LOGIN="10/minute",
    RATE_LIMIT_SIGNUP="5/hour",
    RATE_LIMIT_FORGOT_PASSWORD="5/hour",
    RATE_LIMIT_EMAIL_LINKS="20/minute",
    MONITORING_ENABLED="false",
    SMTP_HOST="",
    ALERT_DASHBOARD_URL="https://watchly.test",
    DEBUG="false",
)

# Hash at bcrypt's lowest cost: the tests hash and verify hundreds of passwords,
# and the cost factor is not what they are testing. Patched before the app is
# imported, so its dummy hash for unknown users is cheap too.
_gensalt = bcrypt.gensalt
bcrypt.gensalt = lambda rounds=4, prefix=b"2b": _gensalt(rounds, prefix)

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import generate_hash_password  # noqa: E402
from app.db.models.user import User, UserRole  # noqa: E402
from app.db.session import AsyncSessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from tests.helpers import CLIENT_IP, PASSWORD  # noqa: E402


@pytest.fixture(scope="session")
async def database():
    """Create the test database if it is missing, and migrate it to head."""
    server = await asyncpg.connect(
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        database="postgres",
    )
    try:
        if not await server.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB):
            await server.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await server.close()
    # Through the migrations rather than create_all, so the tests see the
    # schema a real install has.
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True)
    yield
    await engine.dispose()


@pytest.fixture
async def clean_db(database):
    """Empty every table, so the test starts from a fresh install."""
    async with engine.begin() as conn:
        name = await conn.scalar(text("SELECT current_database()"))
        assert name == TEST_DB, f"connected to {name!r}, not the test database"
        tables = (
            await conn.scalars(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            )
        ).all()
        names = ", ".join(f'"{table}"' for table in tables)
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


def _client(ip: str) -> httpx.AsyncClient:
    # https, so the client keeps the Secure refresh cookie and sends it back to
    # /api/v1/auth, as a browser would.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 50000)),
        base_url="https://testserver",
    )


@pytest.fixture
async def client(clean_db):
    async with _client(CLIENT_IP) as client:
        yield client


@pytest.fixture
async def make_client(clean_db):
    """Open more clients: other devices, each with a cookie jar of its own, or
    other networks with `ip`."""
    async with AsyncExitStack() as stack:

        async def make(ip: str = CLIENT_IP) -> httpx.AsyncClient:
            return await stack.enter_async_context(_client(ip))

        yield make


@pytest.fixture
def make_user(clean_db):
    """Insert a user directly, bypassing signup (so no first-account admin)."""

    async def make(
        username: str = "alice",
        *,
        password: str = PASSWORD,
        role: UserRole = UserRole.VIEWER,
        **fields,
    ) -> User:
        async with AsyncSessionLocal() as session:
            user = User(
                username=username,
                email=f"{username}@example.com",
                password_hash=generate_hash_password(password),
                role=role,
                **fields,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user

    return make
