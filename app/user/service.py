"""User-management business logic.

Knows nothing about HTTP: it raises the domain errors below and
`app/user/routes.py` maps them onto status codes.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import can_change_role, can_suspend_user
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

        should_be_active = not suspended
        if target.is_active == should_be_active:
            return target

        target.is_active = should_be_active
        await self.session.commit()
        await self.session.refresh(target)
        return target
