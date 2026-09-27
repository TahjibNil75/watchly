from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_roles
from app.core.permissions import PROJECT_CREATORS
from app.db.models.user import User
from app.db.session import get_db
from app.monitoring.projects.schemas import (
    ProjectCreate,
    ProjectListResponse,
    ProjectMembersUpdate,
    ProjectRead,
    ProjectUpdate,
)
from app.monitoring.projects.service import (
    DuplicateProjectError,
    ProjectForbiddenError,
    ProjectNotFoundError,
    NoAlertChannelError,
    ProjectService,
    UnknownMembersError,
)

router = APIRouter(prefix="/monitoring/projects", tags=["monitoring: projects"])

#: Admin, DevOps and project managers may create projects.
require_project_creator = require_roles(*PROJECT_CREATORS)

NOT_FOUND = {404: {"description": "No such project"}}
FORBIDDEN = {
    403: {"description": "Requires admin/DevOps, or ownership of this project"}
}


def get_project_service(session: AsyncSession = Depends(get_db)) -> ProjectService:
    return ProjectService(session)


def _translate(exc: Exception) -> HTTPException:
    match exc:
        case ProjectNotFoundError():
            return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
        case ProjectForbiddenError():
            return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
        case DuplicateProjectError():
            return HTTPException(status.HTTP_409_CONFLICT, str(exc))
        case UnknownMembersError() | NoAlertChannelError():
            return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
        case _:
            raise exc


@router.get("", response_model=ProjectListResponse, summary="List projects")
async def list_projects(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    is_active: bool | None = Query(None),
    owner_id: int | None = Query(None, ge=1),
    actor: User = Depends(get_current_user),
    service: ProjectService = Depends(get_project_service),
) -> ProjectListResponse:
    """Admin and DevOps see every project.

    Everyone else — project managers included — sees only the projects they
    own or are a member of: an empty list until someone adds them to one.
    """
    projects, total = await service.list(
        actor, limit=limit, offset=offset, is_active=is_active, owner_id=owner_id
    )
    return ProjectListResponse(
        items=[ProjectRead.model_validate(p) for p in projects],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
    responses={
        403: {"description": "Requires admin, DevOps or project manager"},
        409: {"description": "Project name already taken"},
        422: {"description": "One of the member ids does not exist"},
    },
)
async def create_project(
    payload: ProjectCreate,
    actor: User = Depends(require_project_creator),
    service: ProjectService = Depends(get_project_service),
) -> ProjectRead:
    """The caller becomes the project's owner and can manage it thereafter."""
    try:
        project = await service.create(payload, owner=actor)
    except (DuplicateProjectError, UnknownMembersError) as exc:
        raise _translate(exc) from exc
    return ProjectRead.model_validate(project)


@router.get(
    "/{project_id}",
    response_model=ProjectRead,
    summary="Get a project",
    responses=NOT_FOUND,
)
async def read_project(
    project_id: int = Path(ge=1),
    actor: User = Depends(get_current_user),
    service: ProjectService = Depends(get_project_service),
) -> ProjectRead:
    """A project the caller cannot see reads as 404, not 403."""
    try:
        return ProjectRead.model_validate(await service.get_visible(project_id, actor))
    except ProjectNotFoundError as exc:
        raise _translate(exc) from exc


@router.patch(
    "/{project_id}",
    response_model=ProjectRead,
    summary="Update a project",
    responses={
        **NOT_FOUND,
        **FORBIDDEN,
        409: {"description": "Name already taken"},
        422: {"description": "The change would leave no alert channel"},
    },
)
async def update_project(
    payload: ProjectUpdate,
    project_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: ProjectService = Depends(get_project_service),
) -> ProjectRead:
    try:
        return ProjectRead.model_validate(
            await service.update(project_id, payload, actor)
        )
    except (
        ProjectNotFoundError,
        ProjectForbiddenError,
        DuplicateProjectError,
        NoAlertChannelError,
    ) as exc:
        raise _translate(exc) from exc


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project and everything it monitors",
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def delete_project(
    project_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: ProjectService = Depends(get_project_service),
) -> None:
    try:
        await service.delete(project_id, actor)
    except (ProjectNotFoundError, ProjectForbiddenError) as exc:
        raise _translate(exc) from exc


@router.post(
    "/{project_id}/members",
    response_model=ProjectRead,
    summary="Add responsible members",
    responses={**NOT_FOUND, **FORBIDDEN, 422: {"description": "Unknown member id"}},
)
async def add_members(
    payload: ProjectMembersUpdate,
    project_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: ProjectService = Depends(get_project_service),
) -> ProjectRead:
    """Members receive every alert for this project's sites. Adding is idempotent."""
    try:
        project = await service.add_members(project_id, payload.member_ids, actor)
    except (
        ProjectNotFoundError,
        ProjectForbiddenError,
        UnknownMembersError,
    ) as exc:
        raise _translate(exc) from exc
    return ProjectRead.model_validate(project)


@router.delete(
    "/{project_id}/members/{user_id}",
    response_model=ProjectRead,
    summary="Remove a member",
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def remove_member(
    project_id: int = Path(ge=1),
    user_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: ProjectService = Depends(get_project_service),
) -> ProjectRead:
    """They stop receiving this project's alerts. Removing a non-member is a no-op.

    Refused with `422` if they are the last member and neither Slack nor
    Telegram is configured —
    a project must always keep at least one way to raise an alert.
    """
    try:
        project = await service.remove_member(project_id, user_id, actor)
    except (ProjectNotFoundError, ProjectForbiddenError, NoAlertChannelError) as exc:
        raise _translate(exc) from exc
    return ProjectRead.model_validate(project)
