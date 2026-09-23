from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import (
    get_current_user,
    require_role_manager,
    require_user_manager,
)
from app.db.models.user import User, UserRole
from app.db.session import get_db
from app.schemas.user import UserRead
from app.user.schemas import RoleUpdateRequest, UserListResponse
from app.user.service import (
    CannotChangeOwnRoleError,
    CannotSuspendSelfError,
    InsufficientRankError,
    UserNotFoundError,
    UserService,
)

router = APIRouter(prefix="/users", tags=["users"])

FORBIDDEN = {403: {"description": "Requires a user-management role"}}
NOT_FOUND = {404: {"description": "No such user"}}


def get_user_service(session: AsyncSession = Depends(get_db)) -> UserService:
    return UserService(session)


@router.get(
    "/me",
    response_model=UserRead,
    summary="Get the authenticated user",
)
async def read_me(current_user: User = Depends(get_current_user)) -> UserRead:
    """Any signed-in user can read their own profile, whatever their role."""
    return UserRead.model_validate(current_user)


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
