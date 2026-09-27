"""The access-token guards in app/auth/dependencies.py, exercised through the
endpoints that use them:

- GET /users/me    get_authenticated_user (lets a temporary password through)
- PATCH /users/me  get_current_user
- GET /users       require_user_manager
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.db.models.user import UserRole
from app.utils.jwt import create_access_token
from tests.helpers import ME, USERS, bearer, sign_in, update_user


def _bad_token(kind: str, user_id: int) -> str:
    claims = {
        "sub": str(user_id),
        "type": "access",
        "sv": 0,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    match kind:
        case "garbage":
            return "not-a-jwt"
        case "wrong secret":
            return jwt.encode(
                claims, "another-secret-key-0123456789abcdef0123456789", algorithm="HS256"
            )
        case "unsigned":
            return jwt.encode(claims, None, algorithm="none")
        case "expired":
            return create_access_token(str(user_id), {"sv": 0}, timedelta(seconds=-1))
        case "not an access token":
            return jwt.encode({**claims, "type": "refresh"}, settings.SECRET_KEY, algorithm="HS256")
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic YWxpY2U6cGFzc3dvcmQ="}, {"Authorization": "Bearer"}],
    ids=["no header", "basic auth", "empty bearer"],
)
async def test_no_bearer_token_is_401(client, headers):
    response = await client.get(ME, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated."
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "kind", ["garbage", "wrong secret", "unsigned", "expired", "not an access token"]
)
async def test_a_bad_access_token_is_401(client, make_user, kind):
    user = await make_user("alice")

    response = await client.get(ME, headers=bearer(_bad_token(kind, user.id)))

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_a_token_for_a_deleted_account_is_401(client, clean_db):
    token = create_access_token("999999", {"sv": 0})

    response = await client.get(ME, headers=bearer(token))

    assert response.status_code == 401
    assert response.json()["detail"] == "User no longer exists."


async def test_suspension_takes_effect_on_the_next_request(client, make_user):
    user = await make_user("alice")
    access = await sign_in(client, "alice")
    assert (await client.get(ME, headers=bearer(access))).status_code == 200

    await update_user(user.id, is_active=False)

    response = await client.get(ME, headers=bearer(access))
    assert response.status_code == 403
    assert response.json()["detail"] == "This account has been suspended."


async def test_ending_all_sessions_revokes_access_tokens_already_issued(client, make_user):
    user = await make_user("alice")
    access = await sign_in(client, "alice")

    await update_user(user.id, session_version=1)

    response = await client.get(ME, headers=bearer(access))
    assert response.status_code == 401
    assert response.json()["detail"] == "This session has ended. Please sign in again."


async def test_a_token_from_before_session_versions_counts_as_version_zero(client, make_user):
    user = await make_user("alice")
    legacy = create_access_token(str(user.id))  # no `sv` claim

    assert (await client.get(ME, headers=bearer(legacy))).status_code == 200

    await update_user(user.id, session_version=1)
    assert (await client.get(ME, headers=bearer(legacy))).status_code == 401


async def test_must_change_password_blocks_everything_but_reading_yourself(client, make_user):
    await make_user("alice", role=UserRole.ADMIN, must_change_password=True)
    access = await sign_in(client, "alice")

    assert (await client.get(ME, headers=bearer(access))).status_code == 200
    assert (
        await client.patch(ME, headers=bearer(access), json={"full_name": "A"})
    ).status_code == 403
    # Even an admin's powers wait for the new password.
    assert (await client.get(USERS, headers=bearer(access))).status_code == 403


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (UserRole.ADMIN, True),
        (UserRole.DEVOPS, True),
        (UserRole.PROJECT_MANAGER, True),
        (UserRole.DEVELOPER, False),
        (UserRole.VIEWER, False),
    ],
)
async def test_the_user_directory_is_for_user_managers_only(client, make_user, role, allowed):
    await make_user("alice", role=role)
    access = await sign_in(client, "alice")

    response = await client.get(USERS, headers=bearer(access))

    assert response.status_code == (200 if allowed else 403)


async def test_roles_are_read_from_the_database_not_the_token(client, make_user):
    admin = await make_user("alice", role=UserRole.ADMIN)
    access = await sign_in(client, "alice")
    assert (await client.get(USERS, headers=bearer(access))).status_code == 200

    # Demoted: the token still says Admin, but the next request is refused.
    await update_user(admin.id, role=UserRole.VIEWER)

    assert (await client.get(USERS, headers=bearer(access))).status_code == 403
