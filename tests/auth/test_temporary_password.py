"""A live temporary password is kept, never replaced by a later "forgot
password". See `AuthService.issue_temporary_password`."""

from datetime import UTC, datetime, timedelta

from app.auth.mail import temporary_password_reminder_email
from app.auth.service import AuthService
from app.core.config import settings
from app.db.models.user import User

LIFETIME = timedelta(minutes=settings.TEMP_PASSWORD_EXPIRE_MINUTES)


class FakeSession:
    def __init__(self, user: User | None) -> None:
        self.user = user
        self.commits = 0

    async def scalar(self, _query):
        return self.user

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def make_user(issued_ago: timedelta | None) -> User:
    user = User(username="jane", email="jane@example.com", password_hash="x", is_active=True)
    if issued_ago is not None:
        user.temp_password_hash = "old-hash"
        user.temp_password_expires_at = datetime.now(UTC) - issued_ago + LIFETIME
    return user


async def issue(user: User | None):
    session = FakeSession(user)
    return await AuthService(session).issue_temporary_password("jane@example.com"), session


async def test_first_request_issues_a_password():
    user = make_user(None)
    issued, session = await issue(user)
    assert issued is not None and issued[1] is not None
    assert user.temp_password_hash != "old-hash" and session.commits == 1


async def test_request_within_a_minute_sends_nothing():
    user = make_user(timedelta(seconds=10))
    issued, session = await issue(user)
    assert issued is None
    assert user.temp_password_hash == "old-hash" and session.commits == 0


async def test_later_request_keeps_the_live_password_and_reminds():
    user = make_user(timedelta(minutes=5))
    before = user.temp_password_expires_at
    issued, session = await issue(user)
    assert issued == (user, None)
    assert user.temp_password_hash == "old-hash"
    assert user.temp_password_expires_at == before and session.commits == 0


async def test_repeated_requests_never_replace_it():
    user = make_user(timedelta(minutes=5))
    for _ in range(5):
        await issue(user)
    assert user.temp_password_hash == "old-hash"


async def test_expired_password_is_replaced():
    user = make_user(LIFETIME + timedelta(minutes=1))
    issued, _ = await issue(user)
    assert issued is not None and issued[1] is not None
    assert user.temp_password_hash != "old-hash"


async def test_unknown_or_suspended_account_sends_nothing():
    assert (await issue(None))[0] is None
    user = make_user(None)
    user.is_active = False
    assert (await issue(user))[0] is None


def test_reminder_names_the_account_but_has_no_password():
    user = make_user(timedelta(minutes=5))
    email = temporary_password_reminder_email(user)
    assert "jane" in email.text and "old-hash" not in email.text
    assert all(label != "Temporary password" for label, _ in email.facts)
