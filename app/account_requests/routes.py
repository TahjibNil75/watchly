from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Path,
    Query,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.account_requests.mail import (
    review_email,
    send_approval,
    send_rejection,
    send_review_emails,
)
from app.account_requests.schemas import (
    AccountRequestCreate,
    AccountRequestDecided,
    AccountRequestListResponse,
    AccountRequestRead,
    AccountRequestReceived,
)
from app.account_requests.service import (
    AccountRequestClosedError,
    AccountRequestNotFoundError,
    AccountRequestNotRejectedError,
    AccountRequestService,
)
from app.auth.dependencies import require_inviter
from app.auth.service import UserAlreadyExistsError
from app.core.rate_limit import RATE_LIMITED, rate_limit
from app.db.models.account_request import AccountRequest, AccountRequestStatus
from app.db.models.user import User
from app.db.session import get_db
from app.invitations.service import InvitationConflictError, RoleNotGrantableError

router = APIRouter(prefix="/account-requests", tags=["account requests"])

FORBIDDEN = {403: {"description": "Requires admin/DevOps"}}
NOT_FOUND = {404: {"description": "No such account request"}}
CLOSED = {409: {"description": "The request was already approved or rejected"}}


def get_account_request_service(
    session: AsyncSession = Depends(get_db),
) -> AccountRequestService:
    return AccountRequestService(session)


def _decided(account_request: AccountRequest, email_sent: bool) -> AccountRequestDecided:
    return AccountRequestDecided(
        **AccountRequestRead.model_validate(account_request).model_dump(),
        email_sent=email_sent,
    )


# --- Asking: public ------------------------------------------------------------


@router.post(
    "",
    response_model=AccountRequestReceived,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for an account",
    responses=RATE_LIMITED,
    dependencies=[rate_limit("account_requests")],
)
async def request_account(
    payload: AccountRequestCreate,
    background: BackgroundTasks,
    service: AccountRequestService = Depends(get_account_request_service),
) -> AccountRequestReceived:
    """Ask the admins for an account. No sign-in. Every active Admin and DevOps
    user is emailed, and the request waits under Users for one of them to
    approve or reject it.

    Always `202` with the same answer, so the endpoint cannot be used to find
    out who has an account: nothing is saved or sent when the address already
    has one, has an invitation it can still accept, or already has a request
    waiting, or was rejected before. A rejection sticks until an admin or
    DevOps user unblocks the address, or invites it directly. The emails go out
    after the response for the same reason.

    Each client may ask `RATE_LIMIT_ACCOUNT_REQUESTS` times, whichever
    addresses (`429` past that), so the form cannot be used to flood the
    admins' inboxes.
    """
    account_request = await service.request(payload)
    if account_request is not None:
        emails = [review_email(account_request, to) for to in await service.reviewers()]
        background.add_task(send_review_emails, emails, account_request.id)
    return AccountRequestReceived(
        detail=(
            "Your request has been passed on. If the address can be given an "
            "account, you will get an email once an admin has answered it."
        )
    )


# --- Answering: admin and DevOps -----------------------------------------------


@router.get(
    "",
    response_model=AccountRequestListResponse,
    summary="List account requests",
    responses=FORBIDDEN,
)
async def list_account_requests(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    request_status: AccountRequestStatus | None = Query(
        None, alias="status", description="Filter by status."
    ),
    _: User = Depends(require_inviter),
    service: AccountRequestService = Depends(get_account_request_service),
) -> AccountRequestListResponse:
    """Paginated account requests, newest first — admin and DevOps."""
    account_requests, total = await service.list_requests(
        limit=limit, offset=offset, status=request_status
    )
    return AccountRequestListResponse(
        items=[AccountRequestRead.model_validate(r) for r in account_requests],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/{request_id}/approve",
    response_model=AccountRequestDecided,
    summary="Approve an account request",
    responses={
        **NOT_FOUND,
        403: {
            "description": "Requires admin/DevOps, and the caller may not replace "
            "the invitation this address already has"
        },
        409: {
            "description": "Already approved or rejected, or the address has an "
            "account by now"
        },
    },
)
async def approve_account_request(
    request_id: int = Path(ge=1),
    actor: User = Depends(require_inviter),
    service: AccountRequestService = Depends(get_account_request_service),
) -> AccountRequestDecided:
    """Invite the address to join as a `Viewer` and email it the link, which
    works like any invitation's: once, for `INVITATION_EXPIRE_DAYS`. Change the
    role afterwards with `PATCH /users/{user_id}/role`.

    The invitation is saved even if the email cannot be sent; `email_sent` says
    which happened, and the invitation can be re-sent from the invitation list.
    """
    try:
        account_request, invitation, token = await service.approve(actor, request_id)
    except AccountRequestNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except RoleNotGrantableError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except (
        AccountRequestClosedError,
        UserAlreadyExistsError,
        InvitationConflictError,
    ) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return _decided(account_request, await send_approval(invitation, token))


@router.patch(
    "/{request_id}/reject",
    response_model=AccountRequestDecided,
    summary="Reject an account request",
    responses={**FORBIDDEN, **NOT_FOUND, **CLOSED},
)
async def reject_account_request(
    request_id: int = Path(ge=1),
    actor: User = Depends(require_inviter),
    service: AccountRequestService = Depends(get_account_request_service),
) -> AccountRequestDecided:
    """Turn the request down and email the address to say so. It cannot ask
    again until an admin or DevOps user unblocks it (`PATCH
    /account-requests/{id}/unblock`) or invites it directly. `email_sent` is
    false when that email could not be delivered."""
    try:
        account_request = await service.reject(actor, request_id)
    except AccountRequestNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AccountRequestClosedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return _decided(account_request, await send_rejection(account_request))


@router.patch(
    "/{request_id}/unblock",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Let a rejected address ask again",
    responses={
        **FORBIDDEN,
        **NOT_FOUND,
        409: {"description": "The request is not rejected"},
    },
)
async def unblock_account_request(
    request_id: int = Path(ge=1),
    _: User = Depends(require_inviter),
    service: AccountRequestService = Depends(get_account_request_service),
) -> None:
    """Undo a rejection, so the address may ask for an account again. The
    rejected request is deleted; nobody is emailed, and nothing is created: the
    person still has to ask, and be approved, like anyone else. To give them
    access straight away, invite them instead (`POST /invitations`)."""
    try:
        await service.unblock(request_id)
    except AccountRequestNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AccountRequestNotRejectedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
