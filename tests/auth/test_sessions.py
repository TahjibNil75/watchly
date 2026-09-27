"""Sessions: the refresh cookie, POST /auth/refresh and POST /auth/logout."""

from datetime import timedelta

from sqlalchemy import select, update

from app.auth.cookies import REFRESH_COOKIE
from app.core.config import settings
from app.core.security import hash_token
from app.db.models.refresh_token import RefreshToken
from app.db.session import AsyncSessionLocal
from tests.helpers import (
    AUTH,
    ME,
    NEW_PASSWORD,
    PASSWORD,
    bearer,
    clears_refresh_cookie,
    load_user,
    login,
    refresh_cookie,
    refresh_tokens_of,
    refresh_with,
    sign_in,
    update_user,
    utcnow,
)


async def test_the_refresh_cookie_is_locked_down(client, make_user):
    await make_user("alice")

    response = await login(client, "alice")

    [header] = [
        h for h in response.headers.get_list("set-cookie") if h.startswith(f"{REFRESH_COOKIE}=")
    ]
    attributes = {part.strip().lower() for part in header.split(";")}
    assert "httponly" in attributes
    assert "secure" in attributes
    assert "samesite=strict" in attributes
    assert f"path={AUTH}".lower() in attributes
    assert f"max-age={settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400}" in attributes


async def test_the_refresh_token_is_never_in_a_body_and_stored_only_as_a_hash(
    client, make_user
):
    user = await make_user("alice")

    response = await login(client, "alice")

    token = refresh_cookie(client)
    assert token not in response.text
    [row] = await refresh_tokens_of(user.id)
    assert row.token_hash == hash_token(token)
    assert row.token_hash != token


async def test_refresh_returns_a_working_access_token_and_rotates_the_cookie(
    client, make_user
):
    await make_user("alice")
    await sign_in(client, "alice")
    first = refresh_cookie(client)

    response = await client.post(f"{AUTH}/refresh")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    second = refresh_cookie(client)
    assert second and second != first
    me = await client.get(ME, headers=bearer(body["access_token"]))
    assert me.status_code == 200


async def test_refresh_keeps_a_session_going_indefinitely(client, make_user):
    await make_user("alice")
    await sign_in(client, "alice")

    for _ in range(5):
        assert (await client.post(f"{AUTH}/refresh")).status_code == 200


async def test_refresh_stamps_last_activity(client, make_user):
    user = await make_user("alice")
    await sign_in(client, "alice")
    await update_user(user.id, last_activity=utcnow() - timedelta(days=1))
    before = utcnow()

    await client.post(f"{AUTH}/refresh")

    assert (await load_user(user.id)).last_activity >= before


async def test_refresh_without_a_cookie_is_refused(client):
    response = await client.post(f"{AUTH}/refresh")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert clears_refresh_cookie(response)


async def test_refresh_with_an_unknown_token_is_refused_and_clears_the_cookie(client):
    response = await refresh_with(client, "made-up-token")

    assert response.status_code == 401
    assert clears_refresh_cookie(response)


async def test_a_replayed_refresh_token_ends_the_whole_session(client, make_user):
    await make_user("alice")
    await sign_in(client, "alice")
    stolen = refresh_cookie(client)
    assert (await client.post(f"{AUTH}/refresh")).status_code == 200
    current = refresh_cookie(client)

    # Someone presents the copy that was already used...
    replay = await refresh_with(client, stolen)
    assert replay.status_code == 401
    assert clears_refresh_cookie(replay)

    # ...so the rightful holder's newer token is dead too.
    assert (await refresh_with(client, current)).status_code == 401


async def test_a_replay_ends_only_that_session(client, make_client, make_user):
    await make_user("alice")
    laptop, phone = client, await make_client()
    await sign_in(laptop, "alice")
    await sign_in(phone, "alice")
    stolen = refresh_cookie(laptop)
    await laptop.post(f"{AUTH}/refresh")

    await refresh_with(laptop, stolen)

    assert (await phone.post(f"{AUTH}/refresh")).status_code == 200


async def test_an_expired_refresh_token_is_refused(client, make_user):
    user = await make_user("alice")
    await sign_in(client, "alice")
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await session.commit()

    response = await client.post(f"{AUTH}/refresh")

    assert response.status_code == 401
    assert clears_refresh_cookie(response)


async def test_a_suspended_user_cannot_refresh(client, make_user):
    user = await make_user("alice")
    await sign_in(client, "alice")
    # Suspension revokes sessions too; this is the race where one survives it.
    await update_user(user.id, is_active=False)

    response = await client.post(f"{AUTH}/refresh")

    assert response.status_code == 403
    assert response.json()["detail"] == "This account has been suspended."
    assert clears_refresh_cookie(response)


async def test_logout_ends_the_session_and_clears_the_cookie(client, make_user):
    await make_user("alice")
    await sign_in(client, "alice")
    token = refresh_cookie(client)

    response = await client.post(f"{AUTH}/logout")

    assert response.status_code == 204
    assert clears_refresh_cookie(response)
    assert refresh_cookie(client) is None
    assert (await refresh_with(client, token)).status_code == 401


async def test_logout_after_refreshing_ends_the_session(client, make_user):
    await make_user("alice")
    await sign_in(client, "alice")
    await client.post(f"{AUTH}/refresh")
    rotated = refresh_cookie(client)

    await client.post(f"{AUTH}/logout")

    assert (await refresh_with(client, rotated)).status_code == 401


async def test_logout_leaves_other_devices_signed_in(client, make_client, make_user):
    await make_user("alice")
    phone = await make_client()
    await sign_in(client, "alice")
    await sign_in(phone, "alice")

    await client.post(f"{AUTH}/logout")

    assert (await phone.post(f"{AUTH}/refresh")).status_code == 200


async def test_logout_without_a_session_still_succeeds(client):
    assert (await client.post(f"{AUTH}/logout")).status_code == 204
    assert (
        await client.post(
            f"{AUTH}/logout", headers={"Cookie": f"{REFRESH_COOKIE}=made-up-token"}
        )
    ).status_code == 204


async def test_signing_in_sweeps_out_expired_refresh_tokens(client, make_user):
    old = await make_user("old")
    async with AsyncSessionLocal() as session:
        session.add(
            RefreshToken(
                user_id=old.id,
                family_id="f" * 32,
                token_hash=hash_token("expired"),
                expires_at=utcnow() - timedelta(minutes=1),
            )
        )
        await session.commit()
    await make_user("alice")

    await sign_in(client, "alice")

    async with AsyncSessionLocal() as session:
        swept = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token("expired"))
        )
    assert swept is None


async def test_changing_the_password_signs_out_every_other_device(
    client, make_client, make_user
):
    await make_user("alice")
    phone = await make_client()
    laptop_access = await sign_in(client, "alice")
    phone_access = await sign_in(phone, "alice")

    changed = await client.post(
        f"{ME}/password",
        headers=bearer(laptop_access),
        json={
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )
    assert changed.status_code == 200, changed.text

    # Every access token issued before the change is refused at once...
    for token in (laptop_access, phone_access):
        response = await client.get(ME, headers=bearer(token))
        assert response.status_code == 401
        assert "session has ended" in response.json()["detail"]
    # ...the other device cannot refresh its way back in...
    assert (await phone.post(f"{AUTH}/refresh")).status_code == 401
    # ...and the device that made the change got a fresh session.
    refreshed = await client.post(f"{AUTH}/refresh")
    assert refreshed.status_code == 200
    me = await client.get(ME, headers=bearer(refreshed.json()["access_token"]))
    assert me.status_code == 200
