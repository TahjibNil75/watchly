"""Auth business logic.

Knows nothing about HTTP: it raises the domain errors below and
`app/auth/routes.py` maps them onto status codes.
"""

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import SignupRequest
from app.core.security import generate_hash_password, verify_password
from app.db.models.user import User, UserRole

# Verified against when no user matches, so a login attempt for an unknown
# account costs the same bcrypt work as one for a real account. Without it,
# response timing tells an attacker which usernames/emails exist.
_DUMMY_PASSWORD_HASH = generate_hash_password("timing-attack-placeholder")


class AuthError(Exception):
    """Base class for auth failures."""


class UserAlreadyExistsError(AuthError):
    def __init__(self, field: str, value: str) -> None:
        self.field = field
        self.value = value
        super().__init__(f"A user with this {field} already exists.")


class InvalidCredentialsError(AuthError):
    """Wrong password, or no such user — deliberately indistinguishable."""

    def __init__(self) -> None:
        super().__init__("Incorrect username/email or password.")


class InactiveUserError(AuthError):
    """The credentials are correct but the account has been suspended."""

    def __init__(self) -> None:
        super().__init__("This account has been suspended.")


class AuthService:
    """Auth operations for a single request's session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_user_by_id(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def get_user_by_email(self, email: str) -> User | None:
        return await self.session.scalar(select(User).where(User.email == email))

    async def get_user_by_username(self, username: str) -> User | None:
        return await self.session.scalar(select(User).where(User.username == username))

    async def get_user_by_identifier(self, identifier: str) -> User | None:
        """Look a user up by username *or* email — whichever the client sent."""
        return await self.session.scalar(
            select(User).where(
                or_(User.username == identifier, User.email == identifier)
            )
        )

    async def user_exists_by_email(self, email: str) -> bool:
        return await self.get_user_by_email(email) is not None

    async def authenticate(self, identifier: str, password: str) -> User:
        """Verify credentials and stamp `last_activity`.

        Raises:
            InvalidCredentialsError: no such user, or the password is wrong.
            InactiveUserError: credentials are valid but the account is suspended.
        """
        user = await self.get_user_by_identifier(identifier)

        # Always run bcrypt, even with no user, to keep the timing flat.
        password_ok = verify_password(
            password, user.password_hash if user else _DUMMY_PASSWORD_HASH
        )
        if user is None or not password_ok:
            raise InvalidCredentialsError

        # A suspended user (is_active=False) is refused a token here, and
        # `last_activity` below is deliberately left untouched for them.
        # Checked only after the password is proven, so a wrong password never
        # reveals whether the account exists or is suspended.
        if not user.is_active:
            raise InactiveUserError

        user.last_activity = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def signup(self, payload: SignupRequest) -> User:
        """Register a new account as a VIEWER.

        `password` / `confirm_password` are already checked to match by
        :class:`~app.auth.schemas.SignupRequest`; only the hash is stored.

        Raises:
            UserAlreadyExistsError: the email or username is taken.
        """
        if await self.user_exists_by_email(payload.email):
            raise UserAlreadyExistsError("email", payload.email)

        # username is unique too, so check it up front rather than letting the
        # insert blow up with an IntegrityError.
        if await self.get_user_by_username(payload.username) is not None:
            raise UserAlreadyExistsError("username", payload.username)

        user = User(
            username=payload.username,
            email=payload.email,
            full_name=payload.full_name,
            password_hash=generate_hash_password(payload.password),
            role=UserRole.VIEWER,
            is_active=True,
        )
        self.session.add(user)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            # Two signups racing for the same email/username: the unique index
            # is the real arbiter, so report it the same way as the check above.
            await self.session.rollback()
            constraint = str(getattr(exc.orig, "constraint_name", "") or exc.orig)
            field = "username" if "username" in constraint else "email"
            raise UserAlreadyExistsError(field, getattr(payload, field)) from exc

        await self.session.refresh(user)
        return user

