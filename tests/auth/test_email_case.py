"""Emails are case-insensitive at sign-in and are stored in lower case. See
`AuthService.get_user_by_identifier` and `AuthService.signup`."""

from sqlalchemy.dialects import postgresql

from app.auth.schemas import SignupRequest
from app.auth.service import AuthService
from app.db.models.user import User


class FakeSession:
    def __init__(self, scalars=()) -> None:
        self.queries = []
        self.added = []
        self._scalars = list(scalars)

    async def scalar(self, query):
        self.queries.append(query)
        return self._scalars.pop(0) if self._scalars else None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass

    async def refresh(self, _obj) -> None:
        pass


def compiled(query) -> tuple[str, dict]:
    c = query.compile(dialect=postgresql.dialect())
    return str(c), c.params


async def test_identifier_lookup_ignores_email_case():
    session = FakeSession()
    await AuthService(session).get_user_by_identifier("QA.Admin@Example.com")
    sql, params = compiled(session.queries[0])
    assert "lower(users.email) = %(lower_1)s" in sql
    assert "qa.admin@example.com" in params.values()


async def test_username_lookup_stays_case_sensitive():
    session = FakeSession()
    await AuthService(session).get_user_by_identifier("Jane")
    sql, params = compiled(session.queries[0])
    assert "users.username = %(username_1)s" in sql
    assert "Jane" in params.values()


async def test_first_admin_email_is_stored_in_lower_case():
    # not-yet-any-user, advisory-locked recheck, no existing email/username
    session = FakeSession(scalars=[False, False, None, None])

    async def execute(*_a, **_k):
        return None

    session.execute = execute
    payload = SignupRequest(
        username="qa_admin",
        email="QA.Admin@Example.com",
        password="Str0ng@Pass",
        confirm_password="Str0ng@Pass",
    )
    user = await AuthService(session).signup(payload)
    assert isinstance(user, User) and user.email == "qa.admin@example.com"
