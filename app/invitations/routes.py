from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_auth_service, require_inviter
from app.auth.routes import issue_tokens
from app.auth.service import AuthService, UserAlreadyExistsError
from app.core.rate_limit import RATE_LIMITED, rate_limit
from app.db.models.invitation import InvitationStatus
from app.db.models.user import User
from app.db.session import get_db
from app.invitations.mail import send_invitation
from app.invitations.schemas import (
    AcceptInvitationRequest,
    AcceptInvitationResponse,
    InvitationCreate,
    InvitationCreated,
    InvitationListResponse,
    InvitationPreview,
    InvitationRead,
    InvitationTokenRequest,
)
from app.invitations.service import (
    InvitationClosedError,
    InvitationConflictError,
    InvitationNotFoundError,
    InvitationService,
    InvitationUnusableError,
    RoleNotGrantableError,
)
from app.schemas.user import UserRead

router = APIRouter(prefix="/invitations", tags=["invitations"])

FORBIDDEN = {403: {"description": "Requires admin/DevOps"}}
NOT_FOUND = {404: {"description": "No such invitation"}}
UNUSABLE = {
    410: {
        "description": "Already used, withdrawn or expired, or its sender may no "
        "longer grant the role"
    }
}


def get_invitation_service(session: AsyncSession = Depends(get_db)) -> InvitationService:
    return InvitationService(session)


# --- Managing invitations: admin and DevOps ------------------------------------


@router.post(
    "",
    response_model=InvitationCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Invite a user by email",
    responses={
        403: {"description": "Requires admin/DevOps, and the caller may not grant that role"},
        409: {"description": "The address already has an account, or a concurrent invitation"},
    },
)
async def create_invitation(
    payload: InvitationCreate,
    actor: User = Depends(require_inviter),
    service: InvitationService = Depends(get_invitation_service),
) -> InvitationCreated:
    """Email someone a link that creates their account with the chosen role.

    Admin may grant any role; DevOps any role except Admin (see `INVITABLE_BY`).
    Inviting an address that already has a live invitation replaces it, which is
    also how to re-send one. The link works once and expires after
    `INVITATION_EXPIRE_DAYS`.

    The invitation is saved even if the email cannot be sent; `email_sent` says
    which happened.
    """
    try:
        invitation, token = await service.invite(actor, payload.email, payload.role)
    except RoleNotGrantableError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except (UserAlreadyExistsError, InvitationConflictError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    email_sent = await send_invitation(invitation, token)
    return InvitationCreated(
        **InvitationRead.model_validate(invitation).model_dump(), email_sent=email_sent
    )


@router.get(
    "",
    response_model=InvitationListResponse,
    summary="List invitations",
    responses=FORBIDDEN,
)
async def list_invitations(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    invitation_status: InvitationStatus | None = Query(
        None, alias="status", description="Filter by status."
    ),
    _: User = Depends(require_inviter),
    service: InvitationService = Depends(get_invitation_service),
) -> InvitationListResponse:
    """Paginated invitations, newest first — admin and DevOps."""
    invitations, total = await service.list_invitations(
        limit=limit, offset=offset, status=invitation_status
    )
    return InvitationListResponse(
        items=[InvitationRead.model_validate(i) for i in invitations],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/{invitation_id}/revoke",
    response_model=InvitationRead,
    summary="Withdraw an invitation",
    responses={
        **NOT_FOUND,
        403: {"description": "The caller may not grant this invitation's role"},
        409: {"description": "The invitation was already accepted"},
    },
)
async def revoke_invitation(
    invitation_id: int = Path(ge=1),
    actor: User = Depends(require_inviter),
    service: InvitationService = Depends(get_invitation_service),
) -> InvitationRead:
    """Kill an invitation's link. Only a role that could have sent it may
    withdraw it, so DevOps cannot cancel an invitation to Admin."""
    try:
        invitation = await service.revoke(actor, invitation_id)
    except InvitationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except RoleNotGrantableError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except InvitationClosedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return InvitationRead.model_validate(invitation)


# --- The invitee's side: public, authenticated by the emailed token ------------


@router.post(
    "/preview",
    response_model=InvitationPreview,
    summary="Look up an invitation from its emailed token",
    responses={
        404: {"description": "No invitation matches this token"},
        **UNUSABLE,
        **RATE_LIMITED,
    },
    dependencies=[rate_limit("email_links")],
)
async def preview_invitation(
    payload: InvitationTokenRequest,
    service: InvitationService = Depends(get_invitation_service),
) -> InvitationPreview:
    """Show the invitee which address and role they are accepting, without using
    the invitation up. No sign-in. A POST so the token stays out of access logs.
    Each client may try `RATE_LIMIT_EMAIL_LINKS` tokens, shared with accepting
    and with confirming an email address (`429` past that)."""
    try:
        invitation = await service.preview(payload.token)
    except InvitationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InvitationUnusableError as exc:
        raise HTTPException(status.HTTP_410_GONE, str(exc)) from exc
    return InvitationPreview(
        email=invitation.email,
        role=invitation.role,
        invited_by_name=invitation.invited_by_name,
        expires_at=invitation.expires_at,
    )


@router.post(
    "/accept",
    response_model=AcceptInvitationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Accept an invitation and create the account",
    responses={
        404: {"description": "No invitation matches this token"},
        409: {"description": "Username taken, or the address registered meanwhile"},
        **UNUSABLE,
        **RATE_LIMITED,
    },
    dependencies=[rate_limit("email_links")],
)
async def accept_invitation(
    payload: AcceptInvitationRequest,
    response: Response,
    service: InvitationService = Depends(get_invitation_service),
    auth: AuthService = Depends(get_auth_service),
) -> AcceptInvitationResponse:
    """Create the user with the email and role the invitation fixed, and sign
    them in. No sign-in needed; the link works once. Rate-limited with
    `POST /invitations/preview`."""
    try:
        user = await service.accept(payload)
    except InvitationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InvitationUnusableError as exc:
        raise HTTPException(status.HTTP_410_GONE, str(exc)) from exc
    except UserAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return AcceptInvitationResponse(
        user=UserRead.model_validate(user),
        tokens=await issue_tokens(user, auth, response),
    )
