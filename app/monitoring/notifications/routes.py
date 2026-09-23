"""Notification settings, previews, and on-demand monthly reports.

Global settings are the admin's defaults for every project; a project may
override them for itself. See `service.py` for how the levels combine.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_roles
from app.core.permissions import PROJECT_CREATORS, ROLE_MANAGERS
from app.db.models.user import User
from app.db.session import get_db
from app.monitoring.alerts.base import NotificationKind
from app.monitoring.alerts.email import render_html, render_text
from app.monitoring.alerts.slack import build_payload
from app.monitoring.notifications.catalog import CATALOG
from app.monitoring.notifications.reports import (
    NoReportDataError,
    PeriodNotOverError,
    ReportService,
    month_period,
    previous_month,
)
from app.monitoring.notifications.samples import sample_event
from app.monitoring.notifications.schemas import (
    NotificationPreviewRequest,
    NotificationPreviewResponse,
    NotificationSettingRead,
    NotificationSettingsResponse,
    NotificationSettingUpdate,
    ReportSendRequest,
    ReportSendResponse,
)
from app.monitoring.notifications.service import (
    NotificationSettingsError,
    NotificationSettingsService,
)
from app.monitoring.notifications.templating import compose_message
from app.monitoring.projects.service import (
    ProjectForbiddenError,
    ProjectNotFoundError,
    ProjectService,
)

router = APIRouter(prefix="/monitoring", tags=["monitoring: notifications"])

#: Admin and DevOps set the defaults every project inherits.
require_admin_or_devops = require_roles(*ROLE_MANAGERS)
require_project_creator = require_roles(*PROJECT_CREATORS)

BAD_SETTING = {
    422: {
        "description": "Unknown placeholder, text too long, or it would silence "
        "the “Site down” alert on every channel"
    }
}
PROJECT_ERRORS = {
    403: {"description": "Requires admin/DevOps, or ownership of this project"},
    404: {"description": "No such project"},
}


def get_settings_service(session: AsyncSession = Depends(get_db)) -> NotificationSettingsService:
    return NotificationSettingsService(session)


def get_project_service(session: AsyncSession = Depends(get_db)) -> ProjectService:
    return ProjectService(session)


def get_report_service(session: AsyncSession = Depends(get_db)) -> ReportService:
    return ReportService(session)


def _project_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProjectNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ProjectForbiddenError):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    raise exc


def _bad_setting(exc: NotificationSettingsError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


# -- global level ---------------------------------------------------------


@router.get(
    "/notifications",
    response_model=NotificationSettingsResponse,
    summary="Global notification settings",
)
async def read_global_settings(
    _: User = Depends(get_current_user),
    service: NotificationSettingsService = Depends(get_settings_service),
) -> NotificationSettingsResponse:
    """Every notification kind with the wording and switches all projects
    inherit, plus each kind's placeholders. Any signed-in user may read it."""
    return NotificationSettingsResponse(
        items=[NotificationSettingRead.of(s) for s in await service.effective_all()]
    )


@router.put(
    "/notifications/{kind}",
    response_model=NotificationSettingRead,
    summary="Set the global default for one notification kind",
    responses={403: {"description": "Requires admin or DevOps"}, **BAD_SETTING},
)
async def update_global_setting(
    payload: NotificationSettingUpdate,
    kind: NotificationKind = Path(),
    actor: User = Depends(require_admin_or_devops),
    service: NotificationSettingsService = Depends(get_settings_service),
) -> NotificationSettingRead:
    try:
        setting = await service.save(kind, project_id=None, actor_id=actor.id, **payload.model_dump())
    except NotificationSettingsError as exc:
        raise _bad_setting(exc) from exc
    return NotificationSettingRead.of(setting)


@router.delete(
    "/notifications/{kind}",
    response_model=NotificationSettingRead,
    summary="Reset the global default for one kind to the built-in wording",
    responses={403: {"description": "Requires admin or DevOps"}},
)
async def reset_global_setting(
    kind: NotificationKind = Path(),
    _: User = Depends(require_admin_or_devops),
    service: NotificationSettingsService = Depends(get_settings_service),
) -> NotificationSettingRead:
    return NotificationSettingRead.of(await service.reset(kind, project_id=None))


@router.post(
    "/notifications/preview",
    response_model=NotificationPreviewResponse,
    summary="Preview a notification with sample data",
    responses=BAD_SETTING,
)
async def preview(
    payload: NotificationPreviewRequest,
    _: User = Depends(get_current_user),
) -> NotificationPreviewResponse:
    """Renders the email and Slack message exactly as they would be sent, using
    made-up data. Nothing is saved or delivered, so use it to try a template
    before saving it. Unknown placeholders are refused, as they would be on save."""
    info = CATALOG[payload.kind]
    try:
        subject = NotificationSettingsService.normalize_template(
            payload.kind, "subject", payload.subject
        )
        body = NotificationSettingsService.normalize_template(payload.kind, "body", payload.body)
    except NotificationSettingsError as exc:
        raise _bad_setting(exc) from exc

    message = compose_message(
        sample_event(payload.kind),
        subject or info.default_subject,
        body or info.default_body,
    )
    slack = build_payload(message)
    return NotificationPreviewResponse(
        subject=message.subject,
        email_html=render_html(message),
        email_text=render_text(message),
        slack_text=slack["text"],
        slack_blocks=slack["blocks"],
    )


# -- project level --------------------------------------------------------


@router.get(
    "/projects/{project_id}/notifications",
    response_model=NotificationSettingsResponse,
    summary="A project's notification settings",
    responses={404: {"description": "No such project"}},
)
async def read_project_settings(
    project_id: int = Path(ge=1),
    actor: User = Depends(get_current_user),
    projects: ProjectService = Depends(get_project_service),
    service: NotificationSettingsService = Depends(get_settings_service),
) -> NotificationSettingsResponse:
    """Each kind as it behaves for this project, after inheriting from the
    global level. `overrides` is what the project itself sets."""
    try:
        await projects.get_visible(project_id, actor)
    except ProjectNotFoundError as exc:
        raise _project_error(exc) from exc
    return NotificationSettingsResponse(
        items=[NotificationSettingRead.of(s) for s in await service.effective_all(project_id)]
    )


@router.put(
    "/projects/{project_id}/notifications/{kind}",
    response_model=NotificationSettingRead,
    summary="Override one notification kind for a project",
    responses={**PROJECT_ERRORS, **BAD_SETTING},
)
async def update_project_setting(
    payload: NotificationSettingUpdate,
    project_id: int = Path(ge=1),
    kind: NotificationKind = Path(),
    actor: User = Depends(require_project_creator),
    projects: ProjectService = Depends(get_project_service),
    service: NotificationSettingsService = Depends(get_settings_service),
) -> NotificationSettingRead:
    try:
        await projects.get_for_write(project_id, actor)
        setting = await service.save(
            kind, project_id=project_id, actor_id=actor.id, **payload.model_dump()
        )
    except (ProjectNotFoundError, ProjectForbiddenError) as exc:
        raise _project_error(exc) from exc
    except NotificationSettingsError as exc:
        raise _bad_setting(exc) from exc
    return NotificationSettingRead.of(setting)


@router.delete(
    "/projects/{project_id}/notifications/{kind}",
    response_model=NotificationSettingRead,
    summary="Remove a project's override so it inherits again",
    responses=PROJECT_ERRORS,
)
async def reset_project_setting(
    project_id: int = Path(ge=1),
    kind: NotificationKind = Path(),
    actor: User = Depends(require_project_creator),
    projects: ProjectService = Depends(get_project_service),
    service: NotificationSettingsService = Depends(get_settings_service),
) -> NotificationSettingRead:
    try:
        await projects.get_for_write(project_id, actor)
    except (ProjectNotFoundError, ProjectForbiddenError) as exc:
        raise _project_error(exc) from exc
    return NotificationSettingRead.of(await service.reset(kind, project_id=project_id))


# -- monthly report -------------------------------------------------------


@router.post(
    "/projects/{project_id}/report",
    response_model=ReportSendResponse,
    summary="Send a monthly uptime report now",
    responses={
        **PROJECT_ERRORS,
        409: {"description": "No checks in that month, or the month has not ended"},
    },
)
async def send_report(
    project_id: int = Path(ge=1),
    payload: ReportSendRequest | None = Body(default=None),
    actor: User = Depends(require_project_creator),
    projects: ProjectService = Depends(get_project_service),
    reports: ReportService = Depends(get_report_service),
) -> ReportSendResponse:
    """The same report the scheduler sends on the 1st, on demand — for last
    month unless `month` says otherwise. It follows the project's notification
    switches, and does not stop the scheduled report from going out."""
    try:
        project = await projects.get_for_write(project_id, actor)
    except (ProjectNotFoundError, ProjectForbiddenError) as exc:
        raise _project_error(exc) from exc

    now = datetime.now(UTC)
    month = payload.month if payload else None
    period = month_period(int(month[:4]), int(month[5:])) if month else previous_month(now)
    try:
        event, delivered = await reports.send_now(project, period, now)
    except (NoReportDataError, PeriodNotOverError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return ReportSendResponse(
        month=period.label,
        sites=len(event.sites),
        email_recipients=len(event.recipients),
        slack_configured=event.slack is not None,
        delivered_by=list(delivered),
    )
