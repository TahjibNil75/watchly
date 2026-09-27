"""POST /auth/forgot-password, and signing in with the temporary password."""

from datetime import timedelta

import pytest

import app.auth.routes as auth_routes
from app.core import mail
from app.core.config import settings
from tests.helpers import (
    AUTH,
    ME,
    NEW_PASSWORD,
    PASSWORD,
    bearer,
    load_user,
    login,
    sign_in,
    update_user,
    utcnow,
)


@pytest.fixture
def outbox(monkeypatch) -> list[mail.LinkEmail]:
    """The temporary-password emails the app sends, captured instead of sent."""
    sent: list[mail.LinkEmail] = []

    async def capture(email: mail.LinkEmail, user_id: int) -> None:
        sent.append(email)

    monkeypatch.setattr(auth_routes, "send_temporary_password", capture)
    return sent


def temporary_password(email: mail.LinkEmail) -> str:
    return dict(email.facts)["Temporary password"]


async def forgot(client, email: str):
    return await client.post(f"{AUTH}/forgot-password", json={"email": email})


async def test_a_known_address_is_mailed_a_temporary_password(client, make_user, outbox):
    user = await make_user("alice")

    response = await forgot(client, "alice@example.com")

    assert response.status_code == 202
    [email] = outbox
    assert email.to == "alice@example.com"
    assert temporary_password(email) not in response.text
    stored = await load_user(user.id)
    assert stored.temp_password_hash is not None
    lifetime = timedelta(minutes=settings.TEMP_PASSWORD_EXPIRE_MINUTES)
    assert utcnow() < stored.temp_password_expires_at <= utcnow() + lifetime


async def test_the_answer_does_not_reveal_who_has_an_account(client, make_user, outbox):
    await make_user("alice")
    await make_user("suspended", is_active=False)

    known = await forgot(client, "alice@example.com")
    unknown = await forgot(client, "nobody@example.com")
    suspended = await forgot(client, "suspended@example.com")

    assert known.status_code == unknown.status_code == suspended.status_code == 202
    assert known.json() == unknown.json() == suspended.json()
    assert [email.to for email in outbox] == ["alice@example.com"]


async def test_the_address_is_matched_case_insensitively(client, make_user, outbox):
    await make_user("alice")

    await forgot(client, "ALICE@Example.COM")

    assert len(outbox) == 1


async def test_asking_does_not_lock_the_owner_out(client, make_user, outbox):
    await make_user("alice")

    await forgot(client, "alice@example.com")

    assert (await login(client, "alice", PASSWORD)).status_code == 200


async def test_signing_in_with_it_replaces_the_password_and_demands_a_new_one(
    client, make_user, outbox
):
    user = await make_user("alice")
    await forgot(client, "alice@example.com")

    response = await login(client, "alice", temporary_password(outbox[0]))

    assert response.status_code == 200, response.text
    assert response.json()["user"]["must_change_password"] is True
    stored = await load_user(user.id)
    assert stored.temp_password_hash is None
    # Whoever read the mailbox has taken over; the old password is gone.
    assert (await login(client, "alice", PASSWORD)).status_code == 401


async def test_signing_in_with_it_ends_every_other_session(
    client, make_client, make_user, outbox
):
    await make_user("alice")
    other_device = await make_client()
    old_access = await sign_in(other_device, "alice")
    await forgot(client, "alice@example.com")

    await sign_in(client, "alice", temporary_password(outbox[0]))

    assert (await client.get(ME, headers=bearer(old_access))).status_code == 401
    assert (await other_device.post(f"{AUTH}/refresh")).status_code == 401


async def test_a_temporary_sign_in_may_only_choose_a_new_password(client, make_user, outbox):
    await make_user("alice")
    await forgot(client, "alice@example.com")
    temporary = temporary_password(outbox[0])
    access = await sign_in(client, "alice", temporary)

    assert (await client.get(ME, headers=bearer(access))).status_code == 200
    blocked = await client.patch(ME, headers=bearer(access), json={"full_name": "Alice"})
    assert blocked.status_code == 403
    assert "temporary password" in blocked.json()["detail"]

    changed = await client.post(
        f"{ME}/password",
        headers=bearer(access),
        json={
            "current_password": temporary,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["must_change_password"] is False

    fresh = (await client.post(f"{AUTH}/refresh")).json()["access_token"]
    allowed = await client.patch(ME, headers=bearer(fresh), json={"full_name": "Alice"})
    assert allowed.status_code == 200
    assert (await login(client, "alice", NEW_PASSWORD)).status_code == 200


async def test_signing_in_with_the_usual_password_uses_it_up(client, make_user, outbox):
    user = await make_user("alice")
    await forgot(client, "alice@example.com")

    assert (await login(client, "alice", PASSWORD)).status_code == 200

    assert (await load_user(user.id)).temp_password_hash is None
    assert (await login(client, "alice", temporary_password(outbox[0]))).status_code == 401


async def test_an_expired_temporary_password_does_not_work(client, make_user, outbox):
    user = await make_user("alice")
    await forgot(client, "alice@example.com")
    await update_user(user.id, temp_password_expires_at=utcnow() - timedelta(seconds=1))

    response = await login(client, "alice", temporary_password(outbox[0]))

    assert response.status_code == 401


async def test_it_gets_in_while_sign_in_is_locked(client, make_user, outbox):
    user = await make_user("alice", locked_until=utcnow() + timedelta(minutes=10))
    await forgot(client, "alice@example.com")

    response = await login(client, "alice", temporary_password(outbox[0]))

    assert response.status_code == 200
    assert (await load_user(user.id)).locked_until is None


async def test_a_second_request_within_a_minute_is_ignored(client, make_user, outbox):
    await make_user("alice")

    first = await forgot(client, "alice@example.com")
    second = await forgot(client, "alice@example.com")

    assert first.json() == second.json()
    assert len(outbox) == 1
    assert (await login(client, "alice", temporary_password(outbox[0]))).status_code == 200


async def test_asking_again_later_replaces_the_temporary_password(client, make_user, outbox):
    user = await make_user("alice")
    await forgot(client, "alice@example.com")
    # Two minutes on, past the resend cooldown.
    stored = await load_user(user.id)
    await update_user(
        user.id, temp_password_expires_at=stored.temp_password_expires_at - timedelta(minutes=2)
    )

    await forgot(client, "alice@example.com")

    assert len(outbox) == 2
    first, second = (temporary_password(email) for email in outbox)
    assert first != second
    assert (await login(client, "alice", first)).status_code == 401
    assert (await login(client, "alice", second)).status_code == 200
