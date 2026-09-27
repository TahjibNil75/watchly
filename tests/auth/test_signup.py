"""POST /auth/signup and GET /auth/signup."""

import asyncio

import pytest

from app.core.config import settings
from app.core.security import verify_password
from app.db.models.user import UserRole
from tests.helpers import AUTH, ME, PASSWORD, bearer, load_user, refresh_cookie


def signup_payload(username: str = "jane", **overrides) -> dict:
    return {
        "username": username,
        "email": f"{username}@example.com",
        "full_name": "Jane Doe",
        "password": PASSWORD,
        "confirm_password": PASSWORD,
        **overrides,
    }


async def signup(client, username: str = "jane", **overrides):
    return await client.post(f"{AUTH}/signup", json=signup_payload(username, **overrides))


async def test_first_account_is_the_admin_and_is_signed_in(client):
    response = await signup(client, "founder")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["username"] == "founder"
    assert body["user"]["role"] == UserRole.ADMIN.value
    assert refresh_cookie(client)
    me = await client.get(ME, headers=bearer(body["tokens"]["access_token"]))
    assert me.status_code == 200
    assert me.json()["username"] == "founder"


async def test_later_accounts_are_viewers(client, make_user):
    await make_user("founder", role=UserRole.ADMIN)

    response = await signup(client, "jane")

    assert response.status_code == 201
    assert response.json()["user"]["role"] == UserRole.VIEWER.value


async def test_a_role_in_the_payload_is_ignored(client, make_user):
    await make_user("founder", role=UserRole.ADMIN)

    response = await signup(client, "mallory", role=UserRole.ADMIN.value)

    assert response.status_code == 201
    assert response.json()["user"]["role"] == UserRole.VIEWER.value


async def test_the_password_is_stored_hashed_and_never_returned(client):
    response = await signup(client, "jane")

    assert response.status_code == 201
    assert PASSWORD not in response.text
    assert "password_hash" not in response.json()["user"]
    stored = await load_user(response.json()["user"]["id"])
    assert stored.password_hash != PASSWORD
    assert verify_password(PASSWORD, stored.password_hash)


async def test_duplicate_email_is_a_conflict(client, make_user):
    await make_user("jane")

    response = await signup(client, "someone-else", email="jane@example.com")

    assert response.status_code == 409
    assert response.json()["detail"] == "A user with this email already exists."
    assert refresh_cookie(client) is None


@pytest.mark.xfail(
    strict=True,
    reason="signup compares emails exactly, while forgot-password, invitations and "
    "email changes compare them case-insensitively",
)
async def test_an_email_differing_only_in_case_is_a_conflict(client, make_user):
    await make_user("jane")

    response = await signup(client, "impostor", email="JANE@example.com")

    assert response.status_code == 409


async def test_duplicate_username_is_a_conflict(client, make_user):
    await make_user("jane")

    response = await signup(client, "jane", email="another@example.com")

    assert response.status_code == 409
    assert response.json()["detail"] == "A user with this username already exists."


async def test_closed_signup_refuses_everyone_after_the_admin(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PUBLIC_SIGNUP", False)
    await make_user("founder", role=UserRole.ADMIN)

    response = await signup(client, "jane")

    assert response.status_code == 403
    assert "invitation" in response.json()["detail"]


async def test_closed_signup_does_not_reveal_which_addresses_are_taken(
    client, make_user, monkeypatch
):
    monkeypatch.setattr(settings, "ALLOW_PUBLIC_SIGNUP", False)
    await make_user("founder", role=UserRole.ADMIN)

    taken = await signup(client, "jane", email="founder@example.com")
    free = await signup(client, "joan", email="joan@example.com")

    assert taken.status_code == free.status_code == 403
    assert taken.json() == free.json()


async def test_closed_signup_still_lets_the_first_account_in(client, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PUBLIC_SIGNUP", False)

    response = await signup(client, "founder")

    assert response.status_code == 201
    assert response.json()["user"]["role"] == UserRole.ADMIN.value


@pytest.mark.parametrize(
    ("public", "has_users", "is_open"),
    [
        (True, True, True),
        (True, False, True),
        (False, False, True),
        (False, True, False),
    ],
)
async def test_signup_status(client, make_user, monkeypatch, public, has_users, is_open):
    monkeypatch.setattr(settings, "ALLOW_PUBLIC_SIGNUP", public)
    if has_users:
        await make_user("founder", role=UserRole.ADMIN)

    response = await client.get(f"{AUTH}/signup")

    assert response.status_code == 200
    assert response.json() == {"open": is_open}


async def test_simultaneous_first_signups_make_exactly_one_admin(client):
    responses = await asyncio.gather(*(signup(client, name) for name in ("ann", "bob", "cat")))

    assert [r.status_code for r in responses] == [201, 201, 201]
    roles = sorted(r.json()["user"]["role"] for r in responses)
    assert roles == sorted([UserRole.ADMIN.value, UserRole.VIEWER.value, UserRole.VIEWER.value])


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"confirm_password": "Something-Else-1"}, None),
        ({"password": "short", "confirm_password": "short"}, "password"),
        ({"email": "not-an-email"}, "email"),
        ({"username": "ab"}, "username"),
    ],
)
async def test_invalid_payloads_are_refused_without_echoing_the_password(
    client, overrides, field
):
    response = await client.post(f"{AUTH}/signup", json={**signup_payload(), **overrides})

    assert response.status_code == 422
    # The default 422 body echoes the input back; the app redacts passwords.
    assert PASSWORD not in response.text
    assert "Something-Else-1" not in response.text
    if field:
        assert any(error["loc"][-1] == field for error in response.json()["detail"])
