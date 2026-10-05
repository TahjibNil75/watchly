from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_roles
from app.core.permissions import PROJECT_CREATORS, ROLE_MANAGERS
from app.db.models.user import User
from app.db.session import get_db
from app.monitoring.projects.models import Project, ProjectMonitors
from app.monitoring.projects.service import (
    ProjectForbiddenError,
    ProjectNotFoundError,
    ProjectService,
    UnknownMembersError,
)
from app.monitoring.exports import csv_response, stats_csv
from app.monitoring.service import MonitoringService
from app.monitoring.websites.history import HistoryService
from app.monitoring.websites.models import CheckType, WebsiteStatus
from app.monitoring.websites.pinger import IcmpUnavailableError
from app.monitoring.websites.schemas import (
    BlockedWebsites,
    CheckNowResponse,
    MaintenanceCreate,
    StatsFormat,
    StatsRange,
    WebsiteCheckRead,
    WebsiteCreate,
    WebsiteEventList,
    WebsiteEventRead,
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
    MaintenanceError,
    MaintenanceNotFoundError,
    MaintenanceOverlapError,
    MaintenanceStartedError,
    WebsiteContentRuleError,
    WebsiteNotAlertableError,
    WebsiteNotFoundError,
    WebsiteRequestHeaderError,
    WebsiteService,
    WebsiteSlackError,
    WebsiteTargetError,
    WebsiteTelegramError,
    WebsiteWhatsAppError,
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
        WebsiteSort.ID,
        description=(
            "`status` puts down sites first (not those in maintenance), then sorts by name."
        ),
    ),
    check_type: CheckType | None = Query(
        None, description="Only HTTP sites, pinged hosts, DNS checks or database checks."
    ),
    in_maintenance: bool | None = Query(
        None,
        description="`true`: only sites in a maintenance window now; `false`: only the rest.",
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
        check_type=check_type,
        in_maintenance=in_maintenance,
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
    check_type: CheckType | None = Query(
        None, description="Only HTTP sites, pinged hosts, DNS checks or database checks."
    ),
    actor: User = Depends(get_current_user),
    service: WebsiteService = Depends(get_website_service),
) -> WebsiteSummary:
    """Over the same sites as the list with the same `project_id`, `q` and
    `check_type`, so a dashboard can show every count while paging through one
    state."""
    return await service.summary(actor, project_id=project_id, q=q, check_type=check_type)


# Declared before "/{website_id}", which would otherwise claim the path.
@router.get(
    "/blocked",
    response_model=BlockedWebsites,
    summary="Website checks the egress policy refuses",
    responses={403: {"description": "Requires admin or DevOps"}},
)
async def blocked_websites(
    actor: User = Depends(require_roles(*ROLE_MANAGERS)),
    service: WebsiteService = Depends(get_website_service),
) -> BlockedWebsites:
    """Resolves the host of every HTTP, ping and database check now, as its
    next check would, and lists the ones the policy refuses: loopback,
    link-local and this server's own addresses always; private addresses
    while WEBSITE_PRIVATE_TARGETS is `block`. Use it to find what turning the
    policy on stops. DNS checks connect to nothing and are not listed."""
    return await service.blocked_by_egress()


# Declared before "/{website_id}", which would otherwise claim the path.
@router.get(
    "/events",
    response_model=WebsiteEventList,
    summary=(
        "Recent outages, recoveries, slow spells, packet loss, DNS changes and "
        "expiring certificates"
    ),
)
async def list_events(
    after_id: int | None = Query(
        None, ge=0, description="Only events after this one: poll with the newest id you have."
    ),
    limit: int = Query(20, ge=1, le=100),
    actor: User = Depends(get_current_user),
    service: WebsiteService = Depends(get_website_service),
) -> WebsiteEventList:
    """Newest first, over the same sites as the list. Recorded whether or not
    any channel is switched on for the kind. An outage appears once, when it
    starts: still-down reminders are not recorded."""
    events, total = await service.events(actor, after_id=after_id, limit=limit)
    return WebsiteEventList(
        items=[WebsiteEventRead.model_validate(e) for e in events], total=total, limit=limit
    )


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
                "Unknown recipient id, no alert channel, Slack, Telegram or WhatsApp "
                "settings that send nowhere, a URL that does not suit the check "
                "type, DNS expected values that do not suit the record type, or "
                "content rules on a HEAD/OPTIONS request or a check other than "
                "HTTP, or a database check without db_engine"
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
    """With `check_type: "ping"`, `url` is a host name or IP address to send
    ICMP echo requests to, `ping_count` of them per check. With
    `check_type: "dns"`, `url` is a domain name whose `dns_record_type` record
    is looked up at every resolver in DNS_RESOLVERS; pin `dns_expected_values`
    to treat any other answer as an outage. With `check_type: "database"`,
    `url` is a database endpoint, `host:port`, and `db_engine` the protocol it
    speaks; each check opens a session as far as the server's first answer,
    and never logs in, so it needs no user or password.

    The site emails its project's members and extra_emails, plus its own
    `recipient_ids` (users) and `alert_emails` (addresses). Set
    `inherit_project_recipients: false` to email only the site's own list.
    Slack comes from the project, optionally on the site's own
    `slack_channel_id`; add `slack_bot_token` too for Slack of the site's own,
    which works even when the project has none. Telegram works the same way,
    with `telegram_chat_id` and `telegram_bot_token`. `whatsapp_recipients`
    sends the site's WhatsApp alerts to its own numbers, from the project's
    business number."""
    project = await _assert_can_manage(projects, payload.project_id, actor)
    if project.monitors is ProjectMonitors.INFRASTRUCTURE:
        raise _unprocessable(
            ValueError(f"{project.name} monitors infrastructure: add websites to a websites project.")
        )
    try:
        website = await service.create(payload, project, created_by_id=actor.id)
    except DuplicateWebsiteError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (
        UnknownMembersError,
        WebsiteContentRuleError,
        WebsiteNotAlertableError,
        WebsiteSlackError,
        WebsiteTelegramError,
        WebsiteWhatsAppError,
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
                "The change would leave no alert channel, Slack, Telegram or WhatsApp "
                "settings that send nowhere, a URL that does not suit the check "
                "type, DNS expected values that do not suit the record type, "
                "content rules on a HEAD/OPTIONS request or a check other than "
                "HTTP, or a request header left to keep a value that is not stored"
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
    token included; `slack_bot_token: null` goes back to the project's token.
    `telegram_chat_id` and `telegram_bot_token` work the same way.
    `whatsapp_recipients: []` goes back to the project's WhatsApp numbers.
    The check type cannot change: `url` must suit the one the site has. A DNS
    check's new `dns_record_type` clears its expected values unless new ones
    come with it. `request_headers` replaces the whole list; a header sent
    with `value: null` keeps the value stored under its name."""
    await _get_for_write(service, projects, website_id, actor)
    try:
        return WebsiteRead.model_validate(await service.update(website_id, payload))
    except DuplicateWebsiteError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (
        WebsiteContentRuleError,
        WebsiteNotAlertableError,
        WebsiteRequestHeaderError,
        WebsiteSlackError,
        WebsiteTargetError,
        WebsiteTelegramError,
        WebsiteWhatsAppError,
    ) as exc:
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
    project's and no Slack, Telegram or WhatsApp is set up."""
    await _get_for_write(service, projects, website_id, actor)
    try:
        website = await service.remove_recipient(website_id, user_id)
    except WebsiteNotAlertableError as exc:
        raise _unprocessable(exc) from exc
    return WebsiteRead.model_validate(website)


@router.post(
    "/{website_id}/maintenance",
    response_model=WebsiteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start maintenance now, or schedule it",
    responses={
        **NOT_FOUND,
        **NEEDS_MANAGER,
        409: {"description": "Overlaps maintenance the site already has"},
        422: {"description": "Ends before it starts, or in the past, or lasts over 7 days"},
    },
)
async def schedule_maintenance(
    payload: MaintenanceCreate,
    website_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: WebsiteService = Depends(get_website_service),
    projects: ProjectService = Depends(get_project_service),
) -> WebsiteRead:
    """For a deployment, say. While the window is in effect the site is not
    checked, raises no alerts and loses no uptime; the first check after it
    alerts as usual if the site is still down, and announces the recovery if
    it was down before and is up now.

    `{"duration_minutes": 30}` starts 30 minutes of it now. Send `starts_at`
    and `ends_at` to schedule it ahead. Returns the site, with the window in
    `maintenance` or `upcoming_maintenance`."""
    await _get_for_write(service, projects, website_id, actor)
    try:
        website = await service.schedule_maintenance(
            website_id, payload, created_by_id=actor.id
        )
    except MaintenanceOverlapError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except MaintenanceError as exc:
        raise _unprocessable(exc) from exc
    return WebsiteRead.model_validate(website)


@router.post(
    "/{website_id}/maintenance/end",
    response_model=WebsiteRead,
    summary="End maintenance now",
    responses={**NOT_FOUND, **NEEDS_MANAGER},
)
async def end_maintenance(
    website_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: WebsiteService = Depends(get_website_service),
    projects: ProjectService = Depends(get_project_service),
) -> WebsiteRead:
    """Ends the window in effect now, and the site is checked again on the
    next tick. Does nothing when no window is in effect, so a click that
    lands just after it ran out is harmless."""
    await _get_for_write(service, projects, website_id, actor)
    return WebsiteRead.model_validate(await service.end_maintenance(website_id))


@router.delete(
    "/{website_id}/maintenance/{window_id}",
    response_model=WebsiteRead,
    summary="Cancel scheduled maintenance",
    responses={
        **NOT_FOUND,
        **NEEDS_MANAGER,
        409: {"description": "It has already started: end it instead"},
    },
)
async def cancel_maintenance(
    website_id: int = Path(ge=1),
    window_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: WebsiteService = Depends(get_website_service),
    projects: ProjectService = Depends(get_project_service),
) -> WebsiteRead:
    """Only for a window that has not started; end one that has with
    `POST /maintenance/end`."""
    await _get_for_write(service, projects, website_id, actor)
    try:
        website = await service.cancel_maintenance(website_id, window_id)
    except MaintenanceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except MaintenanceStartedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
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
    response times count successful checks only, as in the monthly report. For a
    ping check, response time is the average round trip, and
    `packet_loss_percent` the share of pings lost; for a DNS check, it is the
    resolvers' average answer time; for a database check, the time to the
    server's answer."""
    try:
        website = await service.get_visible(website_id, actor)
    except WebsiteNotFoundError as exc:
        raise _not_found(exc) from exc
    stats = await HistoryService(service.session).stats(website_id, range_)
    if format_ is StatsFormat.CSV:
        return csv_response(
            stats_csv(
                stats,
                packet_loss=website.check_type is CheckType.PING,
                steps=website.check_type in (CheckType.HTTP, CheckType.DATABASE),
            ),
            f"watchly-site-{website_id}-{range_.value}.csv",
        )
    return stats


@router.post(
    "/{website_id}/check",
    response_model=CheckNowResponse,
    summary="Check a website right now",
    responses={
        **NOT_FOUND,
        **NEEDS_MANAGER,
        503: {"description": "A ping check, and this server is not allowed to send pings"},
    },
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
    During maintenance the check is recorded but raises and clears nothing.
    """
    website = await _get_for_write(service.websites, projects, website_id, actor)

    try:
        outcome = await service.check_one(website)
    except IcmpUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return CheckNowResponse(
        website=WebsiteRead.model_validate(outcome.website),
        check=WebsiteCheckRead.model_validate(outcome.check),
        alert_sent=outcome.alert.value if outcome.alert else None,
    )
