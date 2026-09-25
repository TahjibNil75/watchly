"""Auth business logic.

Knows nothing about HTTP: it raises the domain errors below and
`app/auth/routes.py` maps them onto status codes.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import SignupRequest
from app.core.config import settings
from app.core.security import (
    generate_hash_password,
    generate_temporary_password,
    hash_token,
    new_link_token,
    verify_password,
)
from app.db.models.refresh_token import RefreshToken
from app.db.models.user import User, UserRole

logger = logging.getLogger(__name__)

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


class InvalidRefreshTokenError(AuthError):
    """Missing, unknown, expired, revoked or already used — the session is over."""

    def __init__(self) -> None:
        super().__init__("Your session has ended. Sign in again.")


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
        one, and signs out every session the old password had. Signing in either
        way uses up a pending temporary password.

        A wrong password counts against the account, and MAX_FAILED_LOGIN_ATTEMPTS
        in a row suspend it; signing in resets the count.

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
        if user is None:
            raise InvalidCredentialsError
        if not (password_ok or used_temporary):
            await self._record_failed_login(user)
            raise InvalidCredentialsError

        # A suspended user (is_active=False) is refused a token here, and
        # `last_activity` below is deliberately left untouched for them.
        # Checked only after the password is proven, so a wrong password never
        # reveals whether the account exists or is suspended — including on the
        # attempt that suspends it, which is answered like any other.
        if not user.is_active:
            raise InactiveUserError

        if used_temporary:
            # Only now does the old password stop working: someone has shown
            # they can read the account's mailbox. Sessions opened with the old
            # one end with it — that is what a reset is for.
            user.password_hash = user.temp_password_hash
            user.must_change_password = True
            await self.end_all_sessions(user.id)
        user.clear_temporary_password()
        user.failed_login_attempts = 0
        user.last_activity = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def _record_failed_login(self, user: User) -> None:
        """Count a wrong password against `user`, suspending the account once
        MAX_FAILED_LOGIN_ATTEMPTS are in a row. Commits.

        Suspending works as it does from the API: sessions are revoked, and
        only a user whose role may reinstate this one's can lift it.
        """
        # Incremented in SQL, so simultaneous attempts cannot each read the
        # same count and let a burst of guesses slip past the limit.
        attempts = await self.session.scalar(
            update(User)
            .where(User.id == user.id)
            .values(failed_login_attempts=User.failed_login_attempts + 1)
            .returning(User.failed_login_attempts)
        )
        # Conditional on is_active in SQL too: of simultaneous attempts past the
        # limit, only the one that actually flips it revokes and logs.
        suspended = attempts >= settings.MAX_FAILED_LOGIN_ATTEMPTS and (
            await self.session.scalar(
                update(User)
                .where(User.id == user.id, User.is_active.is_(True))
                .values(is_active=False)
                .returning(User.id)
            )
        )
        if suspended:
            await self.end_all_sessions(user.id)
            logger.warning(
                "Suspended user %s after %d failed sign-ins in a row.",
                user.id,
                attempts,
            )
        await self.session.commit()

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

    # -- sessions (refresh tokens) ------------------------------------------

    async def start_session(self, user: User) -> str:
        """Begin a new session for `user` and return its first refresh token.

        Also sweeps out every user's expired tokens: they can no longer be
        redeemed, so there is nothing left to catch them being reused for.
        """
        now = datetime.now(UTC)
        await self.session.execute(
            delete(RefreshToken).where(RefreshToken.expires_at <= now)
        )
        token = self._add_refresh_token(user.id, uuid.uuid4().hex, now)
        await self.session.commit()
        return token

    async def rotate_refresh_token(self, token: str) -> tuple[User, str]:
        """Redeem `token`: use it up, and return its user and its successor.

        Also stamps `last_activity`, which would otherwise only move at sign-in.

        Raises:
            InvalidRefreshTokenError: unknown, expired, revoked or already used.
                An already-used token also revokes the rest of its family.
            InactiveUserError: the account has been suspended.
        """
        now = datetime.now(UTC)
        # FOR UPDATE: two requests redeeming the same token queue up here, and
        # the second sees the `used_at` the first one set.
        current = await self.session.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == hash_token(token))
            .with_for_update()
        )
        if current is None or current.revoked_at is not None or current.expires_at <= now:
            raise InvalidRefreshTokenError

        if current.used_at is not None:
            # Someone else holds a copy of this token, and there is no telling
            # whether they or the caller are the rightful owner. End the
            # session for both.
            logger.warning(
                "Refresh token reused for user %s; revoking session %s.",
                current.user_id,
                current.family_id,
            )
            await self._revoke(RefreshToken.family_id == current.family_id, now)
            await self.session.commit()
            raise InvalidRefreshTokenError

        user = await self.session.get(User, current.user_id)
        if user is None:
            raise InvalidRefreshTokenError
        if not user.is_active:
            # Suspending revokes the user's sessions; this catches one started
            # while the suspension was being saved.
            raise InactiveUserError

        current.used_at = now
        successor = self._add_refresh_token(user.id, current.family_id, now)
        user.last_activity = now
        await self.session.commit()
        await self.session.refresh(user)
        return user, successor

    async def end_session(self, token: str) -> None:
        """Revoke the session `token` belongs to. Unknown tokens are ignored."""
        family_id = await self.session.scalar(
            select(RefreshToken.family_id).where(
                RefreshToken.token_hash == hash_token(token)
            )
        )
        if family_id is not None:
            await self._revoke(RefreshToken.family_id == family_id, datetime.now(UTC))
            await self.session.commit()

    async def end_all_sessions(self, user_id: int) -> None:
        """Revoke every session `user_id` has: its refresh tokens, and — by
        bumping `session_version` — every access token already issued. The
        caller commits, and refreshes a loaded `User` before minting a token."""
        await self._revoke(RefreshToken.user_id == user_id, datetime.now(UTC))
        await self.session.execute(
            update(User)
            .where(User.id == user_id)
            .values(session_version=User.session_version + 1)
        )

    def _add_refresh_token(self, user_id: int, family_id: str, now: datetime) -> str:
        token = new_link_token()
        self.session.add(
            RefreshToken(
                user_id=user_id,
                family_id=family_id,
                token_hash=hash_token(token),
                expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            )
        )
        return token

    async def _revoke(self, condition: ColumnElement[bool], now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(condition, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )


def _temporary_password_live(user: User) -> bool:
    return (
        user.temp_password_hash is not None
        and user.temp_password_expires_at is not None
        and user.temp_password_expires_at > datetime.now(UTC)
    )
