"""User-management business logic.

Knows nothing about HTTP: it raises the domain errors below and
`app/user/routes.py` maps them onto status codes.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import AuthService, InactiveUserError, UserAlreadyExistsError
from app.core.config import settings
from app.core.password_policy import check_password
from app.core.permissions import can_change_role, can_suspend_user
from app.core.security import (
    generate_hash_password,
    hash_token,
    new_link_token,
    verify_password,
)
from app.db.models.user import User, UserRole


class UserError(Exception):
    """Base class for user-management failures."""


class UserNotFoundError(UserError):
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__(f"No user with id {user_id}.")


class CannotChangeOwnRoleError(UserError):
    def __init__(self) -> None:
        super().__init__(
            "You cannot change your own role. Ask another admin or DevOps user."
        )


class CannotSuspendSelfError(UserError):
    def __init__(self) -> None:
        super().__init__("You cannot suspend your own account.")


class IncorrectPasswordError(UserError):
    def __init__(self) -> None:
        super().__init__("Your current password is incorrect.")


class SamePasswordError(UserError):
    def __init__(self) -> None:
        super().__init__("Choose a new password that is different from your current one.")


class EmailUnchangedError(UserError):
    def __init__(self) -> None:
        super().__init__("That is already your email address.")


class EmailLinkNotFoundError(UserError):
    """No pending change matches the token."""

    def __init__(self) -> None:
        super().__init__(
            "This link is not valid. It may have been used already, or replaced by "
            "a newer one."
        )


class EmailLinkExpiredError(UserError):
    def __init__(self) -> None:
        super().__init__("This link has expired. Ask for the change again from your profile.")


class InsufficientRankError(UserError):
    """The actor's role does not let it act on a target with this role."""

    def __init__(self, actor_role: UserRole, target_role: UserRole, action: str) -> None:
        self.actor_role = actor_role
        self.target_role = target_role
        self.action = action
        super().__init__(
            f"A {actor_role.value!r} user cannot {action} a user "
            f"with the {target_role.value!r} role."
        )


class UserService:
    """User-management operations for a single request's session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: int) -> User:
        user = await self.session.get(User, user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        return user

    async def list_users(
        self,
        limit: int = 50,
        offset: int = 0,
        role: UserRole | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[User], int]:
        """Return one page of users plus the total matching the filters."""
        filters = []
        if role is not None:
            filters.append(User.role == role)
        if is_active is not None:
            filters.append(User.is_active.is_(is_active))

        total = await self.session.scalar(
            select(func.count()).select_from(User).where(*filters)
        )
        rows = await self.session.scalars(
            select(User).where(*filters).order_by(User.id).limit(limit).offset(offset)
        )
        return list(rows), total or 0

    async def change_role(
        self, actor: User, user_id: int, new_role: UserRole
    ) -> User:
        """Assign `new_role` to a user on `actor`'s behalf.

        `actor` has already been checked to be an admin or DevOps user by the
        route's dependency; this adds the rules that depend on *who* the target
        is.

        Raises:
            UserNotFoundError: no such user.
            CannotChangeOwnRoleError: the actor is the target.
            InsufficientRankError: DevOps actor targeting an admin/DevOps user.
        """
        target = await self.get_by_id(user_id)

        # Refusing self-edits is also what keeps the system administrable: the
        # actor is an active role manager and is never the target, so at least
        # one admin/DevOps account always survives any single role change.
        if target.id == actor.id:
            raise CannotChangeOwnRoleError

        if not can_change_role(actor.role, target.role):
            raise InsufficientRankError(actor.role, target.role, "re-role")

        if target.role is new_role:
            return target

        target.role = new_role
        await self.session.commit()
        await self.session.refresh(target)
        return target

    async def set_suspended(
        self, actor: User, user_id: int, suspended: bool
    ) -> User:
        """Suspend or reinstate a user on `actor`'s behalf.

        Suspension is `is_active = False`: login refuses to issue tokens, and
        any token the user already holds stops working on its next request.
        Their sessions are revoked too, so reinstating them does not bring a
        stolen refresh token back to life. Reinstating also clears the failed
        sign-in count and any sign-in lockout, even for an active account, so
        this lifts a lock from MAX_FAILED_LOGIN_ATTEMPTS before it runs out.

        Raises:
            UserNotFoundError: no such user.
            CannotSuspendSelfError: the actor is the target.
            InsufficientRankError: the actor's role may not suspend this
                target's role — see `SUSPENDABLE_BY` in app/core/permissions.py.
        """
        target = await self.get_by_id(user_id)

        # As with roles, blocking self-suspension guarantees at least one
        # active administrator always remains.
        if target.id == actor.id:
            raise CannotSuspendSelfError

        if not can_suspend_user(actor.role, target.role):
            action = "suspend" if suspended else "reinstate"
            raise InsufficientRankError(actor.role, target.role, action)

        if suspended:
            if not target.is_active:
                return target
            target.is_active = False
            await AuthService(self.session).end_all_sessions(target.id)
        else:
            # A fresh start: otherwise one more wrong password would lock the
            # account all over again.
            target.is_active = True
            target.failed_login_attempts = 0
            target.locked_until = None
        await self.session.commit()
        await self.session.refresh(target)
        return target

    # -- the signed-in user's own account ----------------------------------

    async def _email_taken(self, email: str) -> bool:
        # Case-insensitive, like invitations: `Jane@x.com` must not end up on a
        # second account next to `jane@x.com`.
        return (
            await self.session.scalar(select(User.id).where(func.lower(User.email) == email))
            is not None
        )

    async def update_profile(self, user: User, full_name: str | None) -> User:
        user.full_name = full_name
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> User:
        """Replace `user`'s password, given the one they have now.

        Signs out every session the user has, this one included: the caller
        starts a fresh one for the device that made the change. Also lifts
        `must_change_password` and drops any pending temporary password.

        Raises:
            IncorrectPasswordError: `current_password` is wrong.
            SamePasswordError: the new password is the current one.
            WeakPasswordError: the new password breaks a rule, or holds the
                username, email or name.
        """
        if not verify_password(current_password, user.password_hash):
            raise IncorrectPasswordError
        if verify_password(new_password, user.password_hash):
            raise SamePasswordError
        check_password(
            new_password, username=user.username, email=user.email, full_name=user.full_name
        )

        user.password_hash = generate_hash_password(new_password)
        user.must_change_password = False
        user.clear_temporary_password()
        # Same transaction as the password: never one without the other.
        await AuthService(self.session).end_all_sessions(user.id)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def request_email_change(
        self, user: User, new_email: str, current_password: str
    ) -> str:
        """Record `new_email` as waiting for confirmation, and return the raw
        token for the link — the only time it exists in the clear, so it must go
        straight into the email to `new_email`.

        The password is asked for because the address is also a login name: a
        session left open must not be enough to take the account over. Asking
        again replaces any earlier pending change, whose link stops working.

        Raises:
            IncorrectPasswordError: `current_password` is wrong.
            EmailUnchangedError: `new_email` is the current address.
            UserAlreadyExistsError: another account has `new_email`.
        """
        if not verify_password(current_password, user.password_hash):
            raise IncorrectPasswordError

        new_email = new_email.lower()
        if new_email == user.email.lower():
            raise EmailUnchangedError
        if await self._email_taken(new_email):
            raise UserAlreadyExistsError("email", new_email)

        token = new_link_token()
        user.pending_email = new_email
        user.email_token_hash = hash_token(token)
        user.email_token_expires_at = datetime.now(UTC) + timedelta(
            hours=settings.EMAIL_CHANGE_EXPIRE_HOURS
        )
        await self.session.commit()
        await self.session.refresh(user)
        return token

    async def cancel_email_change(self, user: User) -> User:
        """Drop the pending change, if any; its link stops working. Idempotent."""
        _clear_pending_email(user)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def confirm_email_change(self, token: str) -> User:
        """Switch the account to the address the token was emailed to.

        No sign-in needed: holding the token proves the address is the
        requester's, and the request itself needed their password.

        Raises:
            EmailLinkNotFoundError: no pending change matches the token.
            EmailLinkExpiredError: the link is too old.
            InactiveUserError: the account has been suspended.
            UserAlreadyExistsError: another account took the address meanwhile.
                The pending change is dropped, since it can never go through.
        """
        # Locked, so two clicks on the same link cannot race each other.
        user = await self.session.scalar(
            select(User).where(User.email_token_hash == hash_token(token)).with_for_update()
        )
        if user is None or user.pending_email is None:
            raise EmailLinkNotFoundError
        if user.email_token_expires_at is None or user.email_token_expires_at <= datetime.now(UTC):
            raise EmailLinkExpiredError
        if not user.is_active:
            raise InactiveUserError

        new_email = user.pending_email
        _clear_pending_email(user)
        if await self._email_taken(new_email):
            await self.session.commit()
            raise UserAlreadyExistsError("email", new_email)

        user.email = new_email
        try:
            await self.session.commit()
        except IntegrityError as exc:
            # Lost a race for the address; the unique index is the real arbiter.
            await self.session.rollback()
            raise UserAlreadyExistsError("email", new_email) from exc
        await self.session.refresh(user)
        return user


def _clear_pending_email(user: User) -> None:
    user.pending_email = None
    user.email_token_hash = None
    user.email_token_expires_at = None
