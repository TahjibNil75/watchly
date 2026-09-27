"""POST /auth/login: credentials, suspension, and the lock after repeated
wrong passwords."""

import asyncio
from datetime import timedelta

from app.core.config import settings
from tests.helpers import (
    AUTH,
    PASSWORD,
    load_user,
    login,
    refresh_cookie,
    utcnow,
)

GENERIC_401 = "Incorrect username/email or password."


async def test_login_with_username(client, make_user):
    user = await make_user("alice")

    response = await login(client, "alice")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["id"] == user.id
    assert "password_hash" not in body["user"]
    assert body["tokens"]["token_type"] == "bearer"
    assert body["tokens"]["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert refresh_cookie(client)


async def test_login_with_email(client, make_user):
    user = await make_user("alice")

    response = await login(client, "alice@example.com")

    assert response.status_code == 200
    assert response.json()["user"]["id"] == user.id


async def test_login_stamps_last_activity(client, make_user):
    user = await make_user("alice")
    before = utcnow()

    await login(client, "alice")

    assert (await load_user(user.id)).last_activity >= before


async def test_wrong_password_and_unknown_user_are_indistinguishable(client, make_user):
    await make_user("alice")

    wrong_password = await login(client, "alice", "not-the-password")
    unknown_user = await login(client, "nobody", "not-the-password")

    for response in (wrong_password, unknown_user):
        assert response.status_code == 401
        assert response.json() == {"detail": GENERIC_401}
        assert response.headers["WWW-Authenticate"] == "Bearer"
    assert refresh_cookie(client) is None


async def test_wrong_passwords_are_counted_and_a_sign_in_resets_the_count(client, make_user):
    user = await make_user("alice")

    await login(client, "alice", "wrong-1")
    await login(client, "alice", "wrong-2")
    assert (await load_user(user.id)).failed_login_attempts == 2

    assert (await login(client, "alice")).status_code == 200
    assert (await load_user(user.id)).failed_login_attempts == 0


async def test_too_many_wrong_passwords_lock_sign_in(client, make_user):
    user = await make_user("alice")

    for _ in range(settings.MAX_FAILED_LOGIN_ATTEMPTS):
        # The attempt that trips the lock is answered like any wrong password.
        assert (await login(client, "alice", "wrong")).status_code == 401

    stored = await load_user(user.id)
    assert stored.locked_until is not None
    assert stored.locked_until > utcnow()
    assert stored.failed_login_attempts == 0

    # Now even the right password is refused.
    response = await login(client, "alice")
    assert response.status_code == 429
    assert "forgot password" in response.json()["detail"]
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= settings.LOGIN_LOCKOUT_MINUTES * 60
    assert refresh_cookie(client) is None


async def test_a_burst_of_simultaneous_wrong_passwords_still_locks(client, make_user):
    user = await make_user("alice")

    responses = await asyncio.gather(
        *(login(client, "alice", "wrong") for _ in range(settings.MAX_FAILED_LOGIN_ATTEMPTS))
    )

    assert all(r.status_code == 401 for r in responses)
    assert (await load_user(user.id)).locked_until is not None
    assert (await login(client, "alice")).status_code == 429


async def test_attempts_while_locked_are_not_counted(client, make_user):
    user = await make_user("alice", locked_until=utcnow() + timedelta(minutes=10))

    for _ in range(3):
        assert (await login(client, "alice", "wrong")).status_code == 429

    assert (await load_user(user.id)).failed_login_attempts == 0


async def test_the_lock_lifts_by_itself(client, make_user):
    user = await make_user("alice", locked_until=utcnow() - timedelta(seconds=1))

    response = await login(client, "alice")

    assert response.status_code == 200
    assert (await load_user(user.id)).locked_until is None


async def test_the_lock_does_not_end_open_sessions(client, make_client, make_user):
    # Otherwise anyone who knows a username could sign its owner out.
    await make_user("alice")
    assert (await login(client, "alice")).status_code == 200
    attacker = await make_client()

    for _ in range(settings.MAX_FAILED_LOGIN_ATTEMPTS):
        await login(attacker, "alice", "guess")

    refreshed = await client.post(f"{AUTH}/refresh")
    assert refreshed.status_code == 200


async def test_suspended_user_with_right_password_is_told_so(client, make_user):
    user = await make_user("alice", is_active=False)

    response = await login(client, "alice")

    assert response.status_code == 403
    assert response.json()["detail"] == "This account has been suspended."
    assert refresh_cookie(client) is None
    assert (await load_user(user.id)).last_activity is None


async def test_suspended_user_with_wrong_password_looks_like_any_wrong_password(
    client, make_user
):
    await make_user("alice", is_active=False)

    response = await login(client, "alice", "not-the-password")

    assert response.status_code == 401
    assert response.json() == {"detail": GENERIC_401}


async def test_identifier_shorter_than_three_characters_is_invalid(client):
    response = await login(client, "ab")

    assert response.status_code == 422
    assert PASSWORD not in response.text
