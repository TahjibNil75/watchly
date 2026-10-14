"""A wrong `current_password` counts like a failed sign-in, and a locked
account is refused before the password is checked. See
`UserService._check_current_password`."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import generate_hash_password
from app.db.models.user import User
from app.user.service import IncorrectPasswordError, TooManyAttemptsError, UserService

PASSWORD = "Str0ng!Pass-one"


class FakeSession:
    def __init__(self) -> None:
        self.counted = 0
        self.commits = 0

    async def scalar(self, _query):
        self.counted += 1

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def make_user(**fields) -> User:
    return User(
        username="jane",
        email="jane@example.com",
        password_hash=generate_hash_password(PASSWORD),
        is_active=True,
        **fields,
    )


@pytest.mark.parametrize("call", ["password", "email"])
async def test_wrong_password_is_counted(call):
    session = FakeSession()
    service = UserService(session)
    with pytest.raises(IncorrectPasswordError):
        if call == "password":
            await service.change_password(make_user(), "wrong", "An0ther!Pass-two")
        else:
            await service.request_email_change(make_user(), "new@example.com", "wrong")
    assert session.counted == 1 and session.commits == 1


@pytest.mark.parametrize("call", ["password", "email"])
async def test_locked_account_is_refused_without_checking_or_counting(call):
    session = FakeSession()
    service = UserService(session)
    user = make_user(locked_until=datetime.now(UTC) + timedelta(minutes=5))
    with pytest.raises(TooManyAttemptsError):
        # Even the right password is refused while the lock lasts.
        if call == "password":
            await service.change_password(user, PASSWORD, "An0ther!Pass-two")
        else:
            await service.request_email_change(user, "new@example.com", PASSWORD)
    assert session.counted == 0 and session.commits == 0


async def test_expired_lock_does_not_refuse():
    session = FakeSession()
    user = make_user(locked_until=datetime.now(UTC) - timedelta(minutes=1))
    await UserService(session)._check_current_password(user, PASSWORD)
    assert session.counted == 0


async def test_right_password_resets_the_count():
    user = make_user(failed_login_attempts=3)
    await UserService(FakeSession())._check_current_password(user, PASSWORD)
    assert user.failed_login_attempts == 0
