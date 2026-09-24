"""Invitation business logic.

Knows nothing about HTTP: it raises the domain errors below and the routes map
them onto status codes. Sending the email is not done here either — the caller
gets the raw token back from `invite()` and hands it to `mail.py`.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import UserAlreadyExistsError
from app.core.config import settings
from app.core.permissions import can_invite_role
from app.core.security import generate_hash_password, hash_token, new_link_token
from app.db.models.invitation import Invitation, InvitationStatus
from app.db.models.user import User, UserRole
from app.invitations.schemas import AcceptInvitationRequest


class InvitationError(Exception):
    """Base class for invitation failures."""


class InvitationNotFoundError(InvitationError):
    """No invitation with this id, or none matches this token."""

    def __init__(self) -> None:
        super().__init__("No such invitation.")


class InvitationUnusableError(InvitationError):
    """The invitation exists but can no longer be accepted."""


class InvitationClosedError(InvitationError):
    """The invitation was already accepted, so it can no longer be withdrawn."""

    def __init__(self) -> None:
        super().__init__("This invitation has already been accepted.")


class InvitationConflictError(InvitationError):
    """Another invitation for this address was created at the same moment."""

    def __init__(self) -> None:
        super().__init__("Another invitation for this address was just created. Try again.")


class RoleNotGrantableError(InvitationError):
    """The actor's role may not hand out this role — see `INVITABLE_BY`."""

    def __init__(self, actor_role: UserRole, role: UserRole, action: str) -> None:
        self.actor_role = actor_role
        self.role = role
        self.action = action
        super().__init__(
            f"A {actor_role.value!r} user cannot {action} for the {role.value!r} role."
        )


def _status_filter(status: InvitationStatus) -> ColumnElement[bool]:
    open_ = and_(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
    now = datetime.now(UTC)
    match status:
        case InvitationStatus.ACCEPTED:
            return Invitation.accepted_at.is_not(None)
        case InvitationStatus.REVOKED:
            return and_(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_not(None))
        case InvitationStatus.EXPIRED:
            return and_(open_, Invitation.expires_at <= now)
        case InvitationStatus.PENDING:
            return and_(open_, Invitation.expires_at > now)


class InvitationService:
    """Invitation operations for a single request's session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _email_taken(self, email: str) -> bool:
        # Case-insensitive, so `Jane@x.com` cannot be invited over an existing
        # `jane@x.com` account.
        return (
            await self.session.scalar(
                select(User.id).where(func.lower(User.email) == email)
            )
            is not None
        )

    async def list_invitations(
        self,
        limit: int = 50,
        offset: int = 0,
        status: InvitationStatus | None = None,
    ) -> tuple[list[Invitation], int]:
        """Return one page of invitations, newest first, plus the total matching."""
        filters = [] if status is None else [_status_filter(status)]

        total = await self.session.scalar(
            select(func.count()).select_from(Invitation).where(*filters)
        )
        rows = await self.session.scalars(
            select(Invitation)
            .where(*filters)
            .order_by(Invitation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), total or 0

    async def invite(
        self, actor: User, email: str, role: UserRole
    ) -> tuple[Invitation, str]:
        """Invite `email` to join with `role`, on `actor`'s behalf.

        `actor` has already been checked to be an inviting role by the route's
        dependency; this adds the rule that depends on *which* role is being
        handed out. Returns the invitation and the raw token — the only time the
        token exists in the clear, so it must go straight into the email.

        Inviting an address that already has a live invitation replaces it: the
        old link stops working. That is how an invitation is re-sent.

        Raises:
            RoleNotGrantableError: the actor may not grant `role`, or may not
                replace the live invitation for this address (which is for a
                role the actor could not have granted).
            UserAlreadyExistsError: this address already has an account.
            InvitationConflictError: lost a race with a concurrent invitation.
        """
        if not can_invite_role(actor.role, role):
            raise RoleNotGrantableError(actor.role, role, "send an invitation")

        email = email.lower()
        if await self._email_taken(email):
            raise UserAlreadyExistsError("email", email)

        live = await self.session.scalars(
            select(Invitation)
            .where(
                Invitation.email == email,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        for existing in live:
            # Replacing is withdrawing: an actor who could not have sent the old
            # invitation must not be able to void it by re-inviting.
            if not can_invite_role(actor.role, existing.role):
                raise RoleNotGrantableError(
                    actor.role, existing.role, "replace an invitation"
                )
            existing.revoked_at = now
        # The unique index allows one live invitation per address, so the old
        # row has to leave it before the new one goes in.
        await self.session.flush()

        token = new_link_token()
        invitation = Invitation(
            email=email,
            role=role,
            token_hash=hash_token(token),
            invited_by_id=actor.id,
            expires_at=now + timedelta(days=settings.INVITATION_EXPIRE_DAYS),
        )
        self.session.add(invitation)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise InvitationConflictError from exc

        await self.session.refresh(invitation)
        return invitation, token

    async def revoke(self, actor: User, invitation_id: int) -> Invitation:
        """Withdraw an invitation on `actor`'s behalf. Idempotent.

        Raises:
            InvitationNotFoundError: no such invitation.
            RoleNotGrantableError: the actor may not grant its role.
            InvitationClosedError: it was already accepted.
        """
        invitation = await self.session.get(Invitation, invitation_id)
        if invitation is None:
            raise InvitationNotFoundError

        if not can_invite_role(actor.role, invitation.role):
            raise RoleNotGrantableError(actor.role, invitation.role, "revoke an invitation")

        if invitation.accepted_at is not None:
            raise InvitationClosedError
        if invitation.revoked_at is not None:
            return invitation

        invitation.revoked_at = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(invitation)
        return invitation

    async def _get_usable(self, token: str, *, lock: bool = False) -> Invitation:
        statement = select(Invitation).where(Invitation.token_hash == hash_token(token))
        if lock:
            statement = statement.with_for_update()
        invitation = await self.session.scalar(statement)
        if invitation is None:
            raise InvitationNotFoundError

        match invitation.status:
            case InvitationStatus.ACCEPTED:
                raise InvitationUnusableError("This invitation has already been used.")
            case InvitationStatus.REVOKED:
                raise InvitationUnusableError("This invitation has been withdrawn.")
            case InvitationStatus.EXPIRED:
                raise InvitationUnusableError(
                    "This invitation has expired. Ask for a new one."
                )

        # The sender must still be entitled to grant this role. Otherwise
        # suspending or demoting a compromised account would leave the links it
        # already sent working, each one minting an account with the role
        # the attacker chose.
        inviter = invitation.invited_by
        if (
            inviter is None
            or not inviter.is_active
            or not can_invite_role(inviter.role, invitation.role)
        ):
            raise InvitationUnusableError(
                "This invitation is no longer valid. Ask for a new one."
            )
        return invitation

    async def preview(self, token: str) -> Invitation:
        """Look an invitation up by its emailed token, without using it.

        Raises:
            InvitationNotFoundError: no invitation matches the token.
            InvitationUnusableError: accepted, revoked, expired, or its sender
                may no longer grant the role.
        """
        return await self._get_usable(token)

    async def accept(self, payload: AcceptInvitationRequest) -> User:
        """Create the account an invitation offers, with the role it fixed.

        The address comes from the invitation, never from the request: holding
        the emailed token is what proves the invitee owns it.

        Raises:
            InvitationNotFoundError: no invitation matches the token.
            InvitationUnusableError: as for :meth:`preview`.
            UserAlreadyExistsError: the username is taken, or the address
                registered on its own in the meantime.
        """
        # Locked, so two clicks on the same link cannot both create an account.
        invitation = await self._get_usable(payload.token, lock=True)

        if await self.session.scalar(
            select(User.id).where(User.username == payload.username)
        ):
            raise UserAlreadyExistsError("username", payload.username)
        if await self._email_taken(invitation.email):
            raise UserAlreadyExistsError("email", invitation.email)

        user = User(
            username=payload.username,
            email=invitation.email,
            full_name=payload.full_name,
            password_hash=generate_hash_password(payload.password),
            role=invitation.role,
            is_active=True,
        )
        self.session.add(user)
        invitation.accepted_at = datetime.now(UTC)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            # Lost a race for the username or address; the unique indexes are
            # the real arbiter, as in AuthService.signup.
            await self.session.rollback()
            constraint = str(getattr(exc.orig, "constraint_name", "") or exc.orig)
            field = "username" if "username" in constraint else "email"
            value = payload.username if field == "username" else invitation.email
            raise UserAlreadyExistsError(field, value) from exc

        await self.session.refresh(user)
        return user
