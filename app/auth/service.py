"""Auth business logic.

Knows nothing about HTTP: it raises the domain errors below and
`app/auth/routes.py` maps them onto status codes.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import SignupRequest
from app.core.config import settings
from app.core.security import (
    generate_hash_password,
    generate_temporary_password,
    verify_password,
)
from app.db.models.user import User, UserRole

# Verified against when no user matches, so a login attempt for an unknown
# account costs the same bcrypt work as one for a real account. Without it,
# response timing tells an attacker which usernames/emails exist.
_DUMMY_PASSWORD_HASH = generate_hash_password("timing-attack-placeholder")

#: A second "forgot password" this soon after the first is ignored, so the form
#: cannot be used to flood someone's inbox. The first password still works.
_RESEND_COOLDOWN = timedelta(minutes=1)


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

        A temporary password from "forgot password" is accepted too, while it
        lasts. Signing in with one makes it the account's password and sets
        `must_change_password`, which confines the session to choosing a new
        one. Signing in either way uses up a pending temporary password.

        Raises:
            InvalidCredentialsError: no such user, or the password is wrong.
            InactiveUserError: credentials are valid but the account is suspended.
        """
        user = await self.get_user_by_identifier(identifier)

        # Always run bcrypt, even with no user, to keep the timing flat.
        password_ok = verify_password(
            password, user.password_hash if user else _DUMMY_PASSWORD_HASH
        )
        used_temporary = (
            user is not None
            and not password_ok
            and _temporary_password_live(user)
            and verify_password(password, user.temp_password_hash)
        )
        if user is None or not (password_ok or used_temporary):
            raise InvalidCredentialsError

        # A suspended user (is_active=False) is refused a token here, and
        # `last_activity` below is deliberately left untouched for them.
        # Checked only after the password is proven, so a wrong password never
        # reveals whether the account exists or is suspended.
        if not user.is_active:
            raise InactiveUserError

        if used_temporary:
            # Only now does the old password stop working: someone has shown
            # they can read the account's mailbox.
            user.password_hash = user.temp_password_hash
            user.must_change_password = True
        user.clear_temporary_password()
        user.last_activity = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def issue_temporary_password(self, email: str) -> tuple[User, str] | None:
        """Give the account at `email` a temporary password, for "forgot password".

        Returns the user and the password to email them, or None when nothing
        should be sent: no active account has that address, or one was sent
        within the last minute. The caller must answer the same either way, so
        the form does not reveal which addresses have accounts; the bcrypt work
        happens every time for the same reason.

        The account's current password keeps working, so a stranger asking
        cannot lock anyone out. Asking again replaces the temporary password.
        """
        password = generate_temporary_password()
        password_hash = generate_hash_password(password)

        user = await self.session.scalar(
            select(User).where(func.lower(User.email) == email.lower())
        )
        if user is None or not user.is_active:
            return None

        now = datetime.now(UTC)
        lifetime = timedelta(minutes=settings.TEMP_PASSWORD_EXPIRE_MINUTES)
        expires = user.temp_password_expires_at
        if expires is not None and expires - now > lifetime - _RESEND_COOLDOWN:
            return None

        user.temp_password_hash = password_hash
        user.temp_password_expires_at = now + lifetime
        await self.session.commit()
        await self.session.refresh(user)
        return user, password

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



def _temporary_password_live(user: User) -> bool:
    return (
        user.temp_password_hash is not None
        and user.temp_password_expires_at is not None
        and user.temp_password_expires_at > datetime.now(UTC)
    )
