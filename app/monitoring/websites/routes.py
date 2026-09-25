from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_roles
from app.core.permissions import PROJECT_CREATORS
from app.db.models.user import User
from app.db.session import get_db
from app.monitoring.projects.models import Project
from app.monitoring.projects.service import (
    ProjectForbiddenError,
    ProjectNotFoundError,
    ProjectService,
    UnknownMembersError,
)
from app.monitoring.exports import csv_response, stats_csv
from app.monitoring.service import MonitoringService
from app.monitoring.websites.history import HistoryService
from app.monitoring.websites.models import WebsiteStatus
from app.monitoring.websites.schemas import (
    CheckNowResponse,
    StatsFormat,
    StatsRange,
    WebsiteCheckRead,
    WebsiteCreate,
    WebsiteListResponse,
    WebsiteRead,
    WebsiteRecipientsUpdate,
    WebsiteSort,
    WebsiteStats,
    WebsiteSummary,
    WebsiteUpdate,
)
from app.monitoring.websites.models import Website
from app.monitoring.websites.service import (
    DuplicateWebsiteError,
    WebsiteContentRuleError,
    WebsiteNotAlertableError,
    WebsiteNotFoundError,
    WebsiteService,
    WebsiteSlackError,
)

router = APIRouter(prefix="/monitoring/websites", tags=["monitoring: websites"])

NOT_FOUND = {404: {"description": "No such monitored website"}}
NEEDS_MANAGER = {
    403: {"description": "Requires admin/DevOps, or ownership of the project"}
}

#: Admin, DevOps and project managers; per-project ownership is then checked
#: against the site's project.
require_project_creator = require_roles(*PROJECT_CREATORS)


def get_website_service(session: AsyncSession = Depends(get_db)) -> WebsiteService:
    return WebsiteService(session)


def get_monitoring_service(session: AsyncSession = Depends(get_db)) -> MonitoringService:
    return MonitoringService(session)


def get_project_service(session: AsyncSession = Depends(get_db)) -> ProjectService:
    return ProjectService(session)


async def _assert_can_manage(
    projects: ProjectService, project_id: int, actor: User
) -> Project:
    """Registering or changing a site requires rights over its project."""
    try:
        return await projects.get_for_write(project_id, actor)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ProjectForbiddenError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


def _not_found(exc: WebsiteNotFoundError) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


def _unprocessable(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


async def _get_for_write(
    service: WebsiteService, projects: ProjectService, website_id: int, actor: User
) -> Website:
    """Fetch a site the actor may change — which means rights over its project.

    A site they cannot see is not found, exactly as for a read. One they can
    see but whose project they do not manage is forbidden — including a site
    recipient's, whose project itself would read as not found.
    """
    try:
        website = await service.get_visible(website_id, actor)
    except WebsiteNotFoundError as exc:
        raise _not_found(exc) from exc
    try:
        await projects.get_for_write(website.project_id, actor)
    except (ProjectNotFoundError, ProjectForbiddenError) as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, str(ProjectForbiddenError())
        ) from exc
    return website


@router.get("", response_model=WebsiteListResponse, summary="List monitored websites")
async def list_websites(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status_filter: WebsiteStatus | None = Query(None, alias="status"),
    is_enabled: bool | None = Query(None),
    project_id: int | None = Query(None, ge=1, description="Only this project's sites."),
    q: str | None = Query(
        None, max_length=200, description="Only sites whose name or URL contains this."
    ),
    sort: WebsiteSort = Query(
        WebsiteSort.ID, description="`status` puts down sites first, then sorts by name."
    ),
    actor: User = Depends(get_current_user),
    service: WebsiteService = Depends(get_website_service),
) -> WebsiteListResponse:
    """Admin and DevOps see every monitored site.

    Everyone else — project managers included — sees only the sites of
    projects they own or are a member of, plus any site they are a recipient
    of: an empty list until someone adds them to one.
    """
    sites, total = await service.list(
        actor,
        limit=limit,
        offset=offset,
        status=status_filter,
        is_enabled=is_enabled,
        project_id=project_id,
        q=q,
        sort=sort,
    )
    return WebsiteListResponse(
        items=[WebsiteRead.model_validate(s) for s in sites],
        total=total,
        limit=limit,
        offset=offset,
    )


# Declared before "/{website_id}", which would otherwise claim the path.
@router.get(
    "/summary",
    response_model=WebsiteSummary,
    summary="Count monitored websites by state",
)
async def summarize_websites(
    project_id: int | None = Query(None, ge=1, description="Only this project's sites."),
    q: str | None = Query(
        None, max_length=200, description="Only sites whose name or URL contains this."
    ),
    actor: User = Depends(get_current_user),
    service: WebsiteService = Depends(get_website_service),
) -> WebsiteSummary:
    """Over the same sites as the list with the same `project_id` and `q`, so a
    dashboard can show every count while paging through one state."""
    return await service.summary(actor, project_id=project_id, q=q)


@router.post(
    "",
    response_model=WebsiteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start monitoring a website under a project",
    responses={
        **NEEDS_MANAGER,
        404: {"description": "No such project"},
        409: {"description": "URL already monitored"},
        422: {
            "description": (
                "Unknown recipient id, no alert channel, Slack settings that "
                "post nowhere, or content rules on a HEAD/OPTIONS request"
            )
        },
    },
)
async def create_website(
    payload: WebsiteCreate,
    actor: User = Depends(require_project_creator),
    service: WebsiteService = Depends(get_website_service),
    projects: ProjectService = Depends(get_project_service),
) -> WebsiteRead:
    """The site emails its project's members and extra_emails, plus its own
    `recipient_ids` (users) and `alert_emails` (addresses). Set
    `inherit_project_recipients: false` to email only the site's own list.
    Slack comes from the project, optionally on the site's own
    `slack_channel_id`; add `slack_bot_token` too for Slack of the site's own,
    which works even when the project has none."""
    project = await _assert_can_manage(projects, payload.project_id, actor)
    try:
        website = await service.create(payload, project, created_by_id=actor.id)
    except DuplicateWebsiteError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (
        UnknownMembersError,
        WebsiteContentRuleError,
        WebsiteNotAlertableError,
        WebsiteSlackError,
    ) as exc:
        raise _unprocessable(exc) from exc
    return WebsiteRead.model_validate(website)


@router.get(
    "/{website_id}",
    response_model=WebsiteRead,
    summary="Get one monitored website",
    responses=NOT_FOUND,
)
async def read_website(
    website_id: int = Path(ge=1),
    actor: User = Depends(get_current_user),
    service: WebsiteService = Depends(get_website_service),
) -> WebsiteRead:
    """A site the caller cannot see reads as 404, not 403."""
    try:
        website = await service.get_visible(website_id, actor)
    except WebsiteNotFoundError as exc:
        raise _not_found(exc) from exc
    return WebsiteRead.model_validate(website)


@router.patch(
    "/{website_id}",
    response_model=WebsiteRead,
    summary="Update a monitored website",
    responses={
        **NOT_FOUND,
        **NEEDS_MANAGER,
        409: {"description": "URL already monitored"},
        422: {
            "description": (
                "The change would leave no alert channel, Slack settings that "
                "post nowhere, or content rules on a HEAD/OPTIONS request"
            )
        },
    },
)
async def update_website(
    payload: WebsiteUpdate,
    website_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: WebsiteService = Depends(get_website_service),
    projects: ProjectService = Depends(get_project_service),
) -> WebsiteRead:
    """`alert_emails` replaces the whole list; site users are managed through
    `/recipients`. `slack_channel_id: null` removes the site's own Slack,
    token included; `slack_bot_token: null` goes back to the project's token."""
    await _get_for_write(service, projects, website_id, actor)
    try:
        return WebsiteRead.model_validate(await service.update(website_id, payload))
    except DuplicateWebsiteError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (WebsiteContentRuleError, WebsiteNotAlertableError, WebsiteSlackError) as exc:
        raise _unprocessable(exc) from exc


@router.delete(
    "/{website_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop monitoring a website",
    responses={**NOT_FOUND, **NEEDS_MANAGER},
)
async def delete_website(
    website_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: WebsiteService = Depends(get_website_service),
    projects: ProjectService = Depends(get_project_service),
) -> None:
    await _get_for_write(service, projects, website_id, actor)
    await service.delete(website_id)


@router.post(
    "/{website_id}/recipients",
    response_model=WebsiteRead,
    summary="Add users alerted about this site",
    responses={**NOT_FOUND, **NEEDS_MANAGER, 422: {"description": "Unknown user id"}},
)
async def add_recipients(
    payload: WebsiteRecipientsUpdate,
    website_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: WebsiteService = Depends(get_website_service),
    projects: ProjectService = Depends(get_project_service),
) -> WebsiteRead:
    """Several at once; idempotent for anyone already a recipient. They need not
    be project members, and can see this site once added."""
    await _get_for_write(service, projects, website_id, actor)
    try:
        website = await service.add_recipients(website_id, payload.recipient_ids)
    except UnknownMembersError as exc:
        raise _unprocessable(exc) from exc
    return WebsiteRead.model_validate(website)


@router.delete(
    "/{website_id}/recipients/{user_id}",
    response_model=WebsiteRead,
    summary="Stop alerting a user about this site",
    responses={
        **NOT_FOUND,
        **NEEDS_MANAGER,
        422: {"description": "The change would leave no alert channel"},
    },
)
async def remove_recipient(
    website_id: int = Path(ge=1),
    user_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: WebsiteService = Depends(get_website_service),
    projects: ProjectService = Depends(get_project_service),
) -> WebsiteRead:
    """Removing a non-recipient is a no-op. Refused if they are the site's last
    way to alert — e.g. its last recipient while it does not inherit the
    project's and Slack is not set up."""
    await _get_for_write(service, projects, website_id, actor)
    try:
        website = await service.remove_recipient(website_id, user_id)
    except WebsiteNotAlertableError as exc:
        raise _unprocessable(exc) from exc
    return WebsiteRead.model_validate(website)


@router.get(
    "/{website_id}/checks",
    response_model=list[WebsiteCheckRead],
    summary="Recent check history",
    responses=NOT_FOUND,
)
async def list_checks(
    website_id: int = Path(ge=1),
    limit: int = Query(50, ge=1, le=500),
    actor: User = Depends(get_current_user),
    service: WebsiteService = Depends(get_website_service),
) -> list[WebsiteCheckRead]:
    """Newest first — the evidence behind any alert."""
    try:
        checks = await service.recent_checks(website_id, actor, limit=limit)
    except WebsiteNotFoundError as exc:
        raise _not_found(exc) from exc
    return [WebsiteCheckRead.model_validate(c) for c in checks]


@router.get(
    "/{website_id}/stats",
    response_model=WebsiteStats,
    summary="Uptime and response time over a range",
    responses={**NOT_FOUND, 200: {"content": {"text/csv": {}}}},
)
async def website_stats(
    website_id: int = Path(ge=1),
    range_: StatsRange = Query(StatsRange.DAY, alias="range"),
    format_: StatsFormat = Query(
        StatsFormat.JSON, alias="format", description="`csv` downloads the series as a file."
    ),
    actor: User = Depends(get_current_user),
    service: WebsiteService = Depends(get_website_service),
) -> WebsiteStats | Response:
    """Read from hourly rollups, so it reaches back past the retention of raw
    checks. Buckets are UTC hours for 24h and 7d, UTC days for 30d and 90d; the
    last one is still filling. Uptime is the share of checks that succeeded, and
    response times count successful checks only, as in the monthly report."""
    try:
        await service.get_visible(website_id, actor)
    except WebsiteNotFoundError as exc:
        raise _not_found(exc) from exc
    stats = await HistoryService(service.session).stats(website_id, range_)
    if format_ is StatsFormat.CSV:
        return csv_response(stats_csv(stats), f"watchly-site-{website_id}-{range_.value}.csv")
    return stats


@router.post(
    "/{website_id}/check",
    response_model=CheckNowResponse,
    summary="Check a website right now",
    responses={**NOT_FOUND, **NEEDS_MANAGER},
)
async def check_now(
    website_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: MonitoringService = Depends(get_monitoring_service),
    projects: ProjectService = Depends(get_project_service),
) -> CheckNowResponse:
    """Probe immediately instead of waiting for the next tick.

    Goes through the same state machine as a scheduled check, so it can raise
    and clear alerts. Useful for verifying a new site or your SMTP setup.
    """
    website = await _get_for_write(service.websites, projects, website_id, actor)

    outcome = await service.check_one(website)
    return CheckNowResponse(
        website=WebsiteRead.model_validate(outcome.website),
        check=WebsiteCheckRead.model_validate(outcome.check),
        alert_sent=outcome.alert.value if outcome.alert else None,
    )
