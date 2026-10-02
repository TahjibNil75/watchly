from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.countries import countries_of
from app.auth.dependencies import (
    get_authenticated_user,
    get_current_user,
    require_role_manager,
    require_user_manager,
)
from app.db.models.user import User, UserRole
from app.db.session import get_db
from app.auth.cookies import set_refresh_cookie
from app.auth.service import AuthService, UserAlreadyExistsError
from app.core.geo import country_of
from app.core.password_policy import WeakPasswordError
from app.schemas.user import UserRead
from app.user.mail import send_email_confirmation
from app.user.schemas import (
    EmailChangeRequest,
    EmailChangeRequested,
    PasswordChangeRequest,
    ProfileRead,
    ProfileUpdate,
    RoleUpdateRequest,
    SignInCountries,
    UserListResponse,
)
from app.user.service import (
    CannotChangeOwnRoleError,
    CannotSuspendSelfError,
    EmailUnchangedError,
    IncorrectPasswordError,
    InsufficientRankError,
    SamePasswordError,
    UserNotFoundError,
    UserService,
)

router = APIRouter(prefix="/users", tags=["users"])

FORBIDDEN = {403: {"description": "Requires a user-management role"}}
NOT_FOUND = {404: {"description": "No such user"}}
# 400 rather than 401: the session is fine, only the password typed was wrong,
# and a 401 would sign the web UI out.
WRONG_PASSWORD = {400: {"description": "The current password is incorrect"}}


def get_user_service(session: AsyncSession = Depends(get_db)) -> UserService:
    return UserService(session)


@router.get(
    "/me",
    response_model=ProfileRead,
    summary="Get the authenticated user",
)
async def read_me(current_user: User = Depends(get_authenticated_user)) -> ProfileRead:
    """Any signed-in user can read their own profile, whatever their role —
    including one who still has to replace a temporary password."""
    return ProfileRead.model_validate(current_user)


@router.get(
    "/me/countries",
    response_model=SignInCountries,
    summary="Countries you have signed in from",
)
async def read_my_countries(
    request: Request,
    current_user: User = Depends(get_authenticated_user),
    session: AsyncSession = Depends(get_db),
) -> SignInCountries:
    """The countries the proxy in front of Watchly reported for your sign-ins,
    most recent first, and the country of this request. Both are empty or null
    unless `COUNTRY_HEADER` is set. A country is where the connection appears
    to come from, so a VPN shows up as its own."""
    return SignInCountries(
        current=country_of(request),
        countries=await countries_of(session, current_user),
    )


@router.patch(
    "/me",
    response_model=ProfileRead,
    summary="Update your own profile",
)
async def update_me(
    payload: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ProfileRead:
    """Change your full name. Any other field is refused with `422`: the email
    has its own endpoint, and the role and username are not yours to change."""
    if "full_name" in payload.model_fields_set:
        current_user = await service.update_profile(current_user, payload.full_name)
    return ProfileRead.model_validate(current_user)


@router.post(
    "/me/password",
    response_model=ProfileRead,
    summary="Change your own password",
    responses={
        400: {
            "description": "The current password is incorrect, or the new one is "
            "the same, too weak, or holds the username, email or name"
        }
    },
)
async def change_my_password(
    payload: PasswordChangeRequest,
    response: Response,
    current_user: User = Depends(get_authenticated_user),
    service: UserService = Depends(get_user_service),
) -> ProfileRead:
    """Set a new password, given the current one. Every session the account has
    is signed out — other devices included — except the one making the change,
    which gets a new refresh cookie in this response. Access tokens already
    issued are refused from the next request (401): the caller's own client
    then refreshes with its new cookie, and every other device is signed out.

    After signing in with a temporary password this is the only thing the
    account may do, with the temporary password as `current_password`.
    Changing it lifts `must_change_password`."""
    try:
        user = await service.change_password(
            current_user, payload.current_password, payload.new_password
        )
    except (IncorrectPasswordError, SamePasswordError, WeakPasswordError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    set_refresh_cookie(response, await AuthService(service.session).start_session(user))
    return ProfileRead.model_validate(user)


@router.post(
    "/me/email",
    response_model=EmailChangeRequested,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask to change your email address",
    responses={
        400: {"description": "The current password is incorrect, or the address is already yours"},
        409: {"description": "Another account uses that address"},
    },
)
async def request_email_change(
    payload: EmailChangeRequest,
    current_user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> EmailChangeRequested:
    """Email a confirmation link to the new address. The account keeps its
    current address until the link is opened (`POST /auth/confirm-email`); until
    then the new one shows as `pending_email`.

    Asking again replaces the pending change and its link. The link expires
    after `EMAIL_CHANGE_EXPIRE_HOURS`. The change is saved even if the email
    cannot be sent; `email_sent` says which happened.
    """
    try:
        token = await service.request_email_change(
            current_user, payload.new_email, payload.current_password
        )
    except (IncorrectPasswordError, EmailUnchangedError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except UserAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    email_sent = await send_email_confirmation(current_user, token)
    return EmailChangeRequested(
        **ProfileRead.model_validate(current_user).model_dump(), email_sent=email_sent
    )


@router.delete(
    "/me/email",
    response_model=ProfileRead,
    summary="Cancel a pending email change",
)
async def cancel_email_change(
    current_user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ProfileRead:
    """Drop the pending change; its link stops working. Nothing happens if
    there is none."""
    return ProfileRead.model_validate(await service.cancel_email_change(current_user))


@router.get(
    "",
    response_model=UserListResponse,
    summary="List users",
    responses=FORBIDDEN,
)
async def list_users(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    role: UserRole | None = Query(None, description="Filter by role."),
    is_active: bool | None = Query(None, description="Filter by active state."),
    _: User = Depends(require_user_manager),
    service: UserService = Depends(get_user_service),
) -> UserListResponse:
    """Paginated directory of users — admin, DevOps and project managers."""
    users, total = await service.list_users(
        limit=limit, offset=offset, role=role, is_active=is_active
    )
    return UserListResponse(
        items=[UserRead.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{user_id}",
    response_model=UserRead,
    summary="Get a user by id",
    responses={**FORBIDDEN, **NOT_FOUND},
)
async def read_user(
    user_id: int = Path(ge=1),
    _: User = Depends(require_user_manager),
    service: UserService = Depends(get_user_service),
) -> UserRead:
    try:
        user = await service.get_by_id(user_id)
    except UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return UserRead.model_validate(user)


@router.patch(
    "/{user_id}/role",
    response_model=UserRead,
    summary="Change a user's role",
    responses={
        **NOT_FOUND,
        403: {"description": "Requires admin/DevOps, or the target is yourself"},
    },
)
async def change_user_role(
    payload: RoleUpdateRequest,
    user_id: int = Path(ge=1),
    actor: User = Depends(require_role_manager),
    service: UserService = Depends(get_user_service),
) -> UserRead:
    """Assign a role to a user. Admin and DevOps have equal authority here.

    Takes effect on the target's next request: authorization reads the role from
    the database, not from their existing access token.
    """
    try:
        user = await service.change_role(actor, user_id, payload.role)
    except UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (CannotChangeOwnRoleError, InsufficientRankError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return UserRead.model_validate(user)


@router.patch(
    "/{user_id}/suspend",
    response_model=UserRead,
    summary="Suspend a user",
    responses={
        **NOT_FOUND,
        403: {
            "description": "The caller's role may not suspend this target's role "
            "(see SUSPENDABLE_BY), or the target is the caller"
        },
    },
)
async def suspend_user(
    user_id: int = Path(ge=1),
    actor: User = Depends(require_user_manager),
    service: UserService = Depends(get_user_service),
) -> UserRead:
    """Block a user from signing in, and invalidate their current session.

    Sets `is_active = False`. Login then returns `403` for them, and any access
    token they still hold is refused on its next request.
    """
    return await _set_suspended(service, actor, user_id, suspended=True)


@router.patch(
    "/{user_id}/reactivate",
    response_model=UserRead,
    summary="Reinstate a suspended user",
    responses={
        **NOT_FOUND,
        403: {
            "description": "The caller's role may not reinstate this target's role "
            "(see SUSPENDABLE_BY)"
        },
    },
)
async def reactivate_user(
    user_id: int = Path(ge=1),
    actor: User = Depends(require_user_manager),
    service: UserService = Depends(get_user_service),
) -> UserRead:
    """Lift a suspension, restoring the user's ability to log in."""
    return await _set_suspended(service, actor, user_id, suspended=False)


async def _set_suspended(
    service: UserService, actor: User, user_id: int, suspended: bool
) -> UserRead:
    try:
        user = await service.set_suspended(actor, user_id, suspended)
    except UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (CannotSuspendSelfError, InsufficientRankError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return UserRead.model_validate(user)
