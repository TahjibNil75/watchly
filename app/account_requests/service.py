"""Account-request business logic.

Knows nothing about HTTP: it raises the domain errors below and the routes map
them onto status codes. Sending the emails is not done here either — the caller
hands what these methods return to `mail.py`.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, and_, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.account_requests.schemas import AccountRequestCreate
from app.core.config import settings
from app.core.permissions import INVITERS
from app.db.models.account_request import AccountRequest, AccountRequestStatus
from app.db.models.invitation import Invitation
from app.db.models.user import User, UserRole
from app.invitations.service import InvitationService

logger = logging.getLogger(__name__)

#: What an approved request's account starts as. Whoever approved it can
#: change the role afterwards, as for any user.
APPROVED_ROLE = UserRole.VIEWER


class AccountRequestError(Exception):
    """Base class for account-request failures."""


class AccountRequestNotFoundError(AccountRequestError):
    def __init__(self) -> None:
        super().__init__("No such account request.")


class AccountRequestClosedError(AccountRequestError):
    """The request was already approved or rejected."""

    def __init__(self, status: AccountRequestStatus) -> None:
        self.status = status
        super().__init__(f"This request has already been {status.value}.")


class AccountRequestNotRejectedError(AccountRequestError):
    """Only a rejected request has anything to unblock."""

    def __init__(self, status: AccountRequestStatus) -> None:
        self.status = status
        super().__init__(f"This request is {status.value}, not rejected.")


def _status_filter(status: AccountRequestStatus) -> ColumnElement[bool]:
    match status:
        case AccountRequestStatus.APPROVED:
            return AccountRequest.approved_at.is_not(None)
        case AccountRequestStatus.REJECTED:
            return and_(
                AccountRequest.approved_at.is_(None),
                AccountRequest.rejected_at.is_not(None),
            )
        case AccountRequestStatus.PENDING:
            return and_(
                AccountRequest.approved_at.is_(None),
                AccountRequest.rejected_at.is_(None),
            )


class AccountRequestService:
    """Account-request operations for a single request's session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def request(self, payload: AccountRequestCreate) -> AccountRequest | None:
        """Save a request for an account at `payload.email`.

        Returns the request, or None when there is nothing to ask: the address
        already has an account, an invitation it can still accept, a request
        waiting for an answer, or a request that was rejected. A rejection is
        sticky: from then on the address can only get access by an admin or
        DevOps user inviting it directly, or unblocking it (:meth:`unblock`). The caller must answer the same either way,
        so the form does not reveal which addresses those are.
        """
        email = payload.email.lower()
        now = datetime.now(UTC)
        already = (
            select(User.id).where(func.lower(User.email) == email),
            # Approving would replace it with one for a Viewer, whatever role
            # it offers now.
            select(Invitation.id).where(
                Invitation.email == email,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
                Invitation.expires_at > now,
            ),
            # Pending, or rejected for good.
            select(AccountRequest.id).where(
                AccountRequest.email == email,
                AccountRequest.approved_at.is_(None),
            ),
        )
        for statement in already:
            if await self.session.scalar(statement) is not None:
                logger.info("Account request ignored: the address has no use for one.")
                return None

        account_request = AccountRequest(
            email=email, full_name=payload.full_name, message=payload.message
        )
        self.session.add(account_request)
        try:
            await self.session.commit()
        except IntegrityError:
            # Lost a race with the same address asking twice at once.
            await self.session.rollback()
            logger.info("Account request ignored: the same address asked twice at once.")
            return None

        await self.session.refresh(account_request)
        logger.info("Account request %s received.", account_request.id)
        return account_request

    async def reviewers(self) -> list[str]:
        """The addresses to tell about a new request: every active user who
        may answer it."""
        rows = await self.session.scalars(
            select(User.email)
            .where(User.role.in_(INVITERS), User.is_active.is_(True))
            .order_by(User.id)
        )
        return list(rows)

    async def list_requests(
        self,
        limit: int = 50,
        offset: int = 0,
        status: AccountRequestStatus | None = None,
    ) -> tuple[list[AccountRequest], int]:
        """Return one page of requests, newest first, plus the total matching."""
        filters = [] if status is None else [_status_filter(status)]

        total = await self.session.scalar(
            select(func.count()).select_from(AccountRequest).where(*filters)
        )
        rows = await self.session.scalars(
            select(AccountRequest)
            .where(*filters)
            .order_by(AccountRequest.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), total or 0

    async def purge(self) -> int:
        """Delete pending requests whose address has an account by now, and
        approved ones past the newest ACCOUNT_REQUEST_HISTORY_KEEP. Rejected
        requests are never deleted: each is what keeps its address from asking
        again. Called on every tick; returns how many were deleted."""
        # Invited directly while the request waited, and accepted: approving it
        # now could only answer "already has an account".
        result = await self.session.execute(
            delete(AccountRequest)
            .where(
                _status_filter(AccountRequestStatus.PENDING),
                AccountRequest.email.in_(select(func.lower(User.email))),
            )
            .execution_options(synchronize_session=False)
        )
        deleted = result.rowcount or 0
        approved = _status_filter(AccountRequestStatus.APPROVED)
        kept = (
            select(AccountRequest.id)
            .where(approved)
            .order_by(AccountRequest.approved_at.desc(), AccountRequest.id.desc())
            .limit(settings.ACCOUNT_REQUEST_HISTORY_KEEP)
        )
        result = await self.session.execute(
            delete(AccountRequest)
            .where(approved, AccountRequest.id.not_in(kept))
            .execution_options(synchronize_session=False)
        )
        deleted += result.rowcount or 0
        await self.session.commit()
        return deleted

    async def _get_pending(self, request_id: int) -> AccountRequest:
        # Locked, so two people answering the same request cannot both succeed.
        account_request = await self.session.scalar(
            select(AccountRequest)
            .where(AccountRequest.id == request_id)
            .with_for_update()
        )
        if account_request is None:
            raise AccountRequestNotFoundError
        if account_request.status is not AccountRequestStatus.PENDING:
            raise AccountRequestClosedError(account_request.status)
        return account_request

    async def approve(
        self, actor: User, request_id: int
    ) -> tuple[AccountRequest, Invitation, str]:
        """Approve a request on `actor`'s behalf: invite its address to join as
        a Viewer. Returns the request, the invitation and the invitation's raw
        token, which must go straight into the email.

        The request is closed and the invitation created in one commit, so a
        request never reads as approved without a link to send.

        Raises:
            AccountRequestNotFoundError: no such request.
            AccountRequestClosedError: it was already approved or rejected.
            UserAlreadyExistsError, RoleNotGrantableError, InvitationConflictError:
                as for :meth:`InvitationService.invite`. The request stays pending.
        """
        account_request = await self._get_pending(request_id)
        account_request.approved_at = datetime.now(UTC)
        account_request.decided_by_id = actor.id
        try:
            # Commits, taking the two changes above with it.
            invitation, token = await InvitationService(self.session).invite(
                actor, account_request.email, APPROVED_ROLE
            )
        except Exception:
            await self.session.rollback()
            raise

        await self.session.refresh(account_request)
        logger.info(
            "User %s approved account request %s (invitation %s).",
            actor.id, account_request.id, invitation.id,
        )
        return account_request, invitation, token

    async def reject(self, actor: User, request_id: int) -> AccountRequest:
        """Turn a request down on `actor`'s behalf.

        Raises:
            AccountRequestNotFoundError: no such request.
            AccountRequestClosedError: it was already approved or rejected.
        """
        account_request = await self._get_pending(request_id)
        account_request.rejected_at = datetime.now(UTC)
        account_request.decided_by_id = actor.id
        await self.session.commit()
        await self.session.refresh(account_request)
        logger.info("User %s rejected account request %s.", actor.id, account_request.id)
        return account_request

    async def unblock(self, request_id: int) -> AccountRequest:
        """Let a rejected address ask for an account again.

        Deletes the rejection, which is all that was blocking it: the address is
        then treated like any other. Returns the deleted request, for the
        caller to name in its answer.

        Raises:
            AccountRequestNotFoundError: no such request.
            AccountRequestNotRejectedError: it is pending or approved.
        """
        # Locked, so it cannot be unblocked twice at once.
        account_request = await self.session.scalar(
            select(AccountRequest)
            .where(AccountRequest.id == request_id)
            .with_for_update()
        )
        if account_request is None:
            raise AccountRequestNotFoundError
        if account_request.status is not AccountRequestStatus.REJECTED:
            raise AccountRequestNotRejectedError(account_request.status)

        await self.session.delete(account_request)
        await self.session.commit()
        logger.info("Rejected account request %s unblocked.", request_id)
        return account_request
