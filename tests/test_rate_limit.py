"""Per-client rate limits on the public auth endpoints (app/core/rate_limit.py)."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import update
from starlette.requests import Request

from app.core.config import RateLimit, Settings, settings
from app.core.rate_limit import client_key
from app.db.models.rate_limit import RateLimitBucket
from app.db.session import AsyncSessionLocal
from tests.helpers import AUTH, login, utcnow


@pytest.fixture
def login_limit(monkeypatch) -> RateLimit:
    """A small login limit, so tests reach it in a few requests."""
    limit = RateLimit(hits=3, seconds=60)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN", limit)
    return limit


async def test_logins_past_the_limit_are_refused_even_with_the_right_password(
    client, make_user, login_limit
):
    await make_user("alice")
    for _ in range(login_limit.hits):
        assert (await login(client, "alice", "wrong")).status_code == 401

    response = await login(client, "alice")

    assert response.status_code == 429
    assert response.json()["detail"].startswith("Too many requests from your network.")
    assert 1 <= int(response.headers["Retry-After"]) <= login_limit.seconds


async def test_every_account_shares_one_clients_count(client, make_user, login_limit):
    # One client trying a password on each of many accounts is still one client.
    for name in ("ann", "bob", "cat"):
        await make_user(name)
        await login(client, name, "Password1")

    await make_user("dan")
    assert (await login(client, "dan")).status_code == 429


async def test_another_network_has_its_own_count(client, make_client, make_user, login_limit):
    await make_user("alice")
    for _ in range(login_limit.hits + 1):
        await login(client, "alice", "wrong")

    elsewhere = await make_client("198.51.100.7")

    # 401, not 429: the attempt is counted, and the password is wrong.
    assert (await login(elsewhere, "alice", "wrong")).status_code == 401


async def test_one_ipv6_64_counts_as_one_client(make_client, make_user, login_limit):
    await make_user("alice")
    first = await make_client("2001:db8:1:2::1")
    second = await make_client("2001:db8:1:2:ffff::99")

    for _ in range(login_limit.hits):
        await login(first, "alice", "wrong")

    assert (await login(second, "alice")).status_code == 429


async def test_the_window_reopens_when_it_closes(client, make_user, login_limit):
    await make_user("alice")
    for _ in range(login_limit.hits + 1):
        await login(client, "alice", "wrong")
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(RateLimitBucket).values(resets_at=utcnow() - timedelta(seconds=1))
        )
        await session.commit()

    assert (await login(client, "alice")).status_code == 200


async def test_forgot_password_is_limited_whichever_addresses_are_asked_for(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "RATE_LIMIT_FORGOT_PASSWORD", RateLimit(2, 3600))

    responses = [
        await client.post(f"{AUTH}/forgot-password", json={"email": f"user{i}@example.com"})
        for i in range(3)
    ]

    assert [r.status_code for r in responses] == [202, 202, 429]


async def test_signup_is_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_SIGNUP", RateLimit(1, 3600))
    payload = {"email": "x@example.com", "password": "Password-1", "confirm_password": "Password-1"}

    first = await client.post(f"{AUTH}/signup", json={**payload, "username": "first"})
    second = await client.post(
        f"{AUTH}/signup", json={**payload, "username": "second", "email": "y@example.com"}
    )

    assert first.status_code == 201
    assert second.status_code == 429


async def test_limits_can_be_turned_off(client, make_user, login_limit, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    await make_user("alice")

    for _ in range(login_limit.hits * 2):
        assert (await login(client, "alice")).status_code == 200


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("203.0.113.10", "203.0.113.10"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("::ffff:203.0.113.10", "203.0.113.10"),
        ("testclient", "testclient"),
        (None, "unknown"),
    ],
)
def test_client_key(host, key):
    scope = {"type": "http", "headers": []}
    if host is not None:
        scope["client"] = (host, 1234)

    assert client_key(Request(scope)) == key


@pytest.mark.parametrize(
    ("value", "limit"),
    [
        ("10/minute", RateLimit(10, 60)),
        ("5/hour", RateLimit(5, 3600)),
        ("20/15 minutes", RateLimit(20, 900)),
        ("1 / 2 days", RateLimit(1, 172_800)),
        ("3/SECOND", RateLimit(3, 1)),
    ],
)
def test_rate_limit_settings_parse(value, limit):
    assert Settings(RATE_LIMIT_LOGIN=value).RATE_LIMIT_LOGIN == limit


@pytest.mark.parametrize("value", ["ten/minute", "10 per minute", "10/fortnight", "0/minute"])
def test_malformed_rate_limit_settings_are_rejected(value):
    with pytest.raises(ValidationError):
        Settings(RATE_LIMIT_LOGIN=value)
