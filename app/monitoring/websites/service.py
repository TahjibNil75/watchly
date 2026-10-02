"""CRUD for monitored websites. No HTTP concerns, no probing."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, delete, func, literal_column, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.core.permissions import can_view_all_projects
from app.db.models.user import User
from app.monitoring.projects.models import Project, visible_project_ids
from app.monitoring.projects.service import resolve_users
from app.monitoring.websites.models import (
    CheckType,
    DnsRecordType,
    MaintenanceWindow,
    Website,
    WebsiteCheck,
    WebsiteEvent,
    WebsiteStatus,
    maintenance_in_effect,
    recipient_website_ids,
)
from app.monitoring.websites.schemas import (
    MAX_MAINTENANCE_MINUTES,
    MaintenanceCreate,
    WebsiteCreate,
    WebsiteSort,
    WebsiteSummary,
    WebsiteUpdate,
    dns_values,
    monitor_target,
)


class WebsiteError(Exception):
    """Base class for website-management failures."""


class WebsiteNotFoundError(WebsiteError):
    def __init__(self, website_id: int) -> None:
        self.website_id = website_id
        super().__init__(f"No monitored website with id {website_id}.")


class DuplicateWebsiteError(WebsiteError):
    def __init__(self, url: str, record_type: DnsRecordType | None = None) -> None:
        self.url = url
        what = f"The {record_type.value} record of {url}" if record_type else url
        super().__init__(f"{what} is already being monitored.")


class WebsiteNotAlertableError(WebsiteError):
    """The site would have no way to raise an alert."""

    def __init__(self, website: Website) -> None:
        super().__init__(
            f"{website.name!r} would have no alert channel. Add recipient_ids or "
            "alert_emails, set inherit_project_recipients back to true, or "
            "set up Slack, Telegram or WhatsApp on this site or on project "
            f"{website.project.name!r}."
        )


class WebsiteSlackError(WebsiteError):
    """The site's own Slack settings could not deliver anything."""


def slack_problem(website: Website) -> WebsiteSlackError | None:
    """Why the site's own Slack settings would post nowhere, if they would."""
    if website.slack_bot_token and not website.slack_channel_id:
        return WebsiteSlackError(
            "slack_bot_token needs slack_channel_id: a site's own token posts to "
            "the site's own channel."
        )
    if website.slack_channel_id and not (
        website.slack_bot_token or website.project.slack_bot_token
    ):
        return WebsiteSlackError(
            "slack_channel_id needs a bot token: add slack_bot_token to this "
            f"site, or configure Slack on project {website.project.name!r}."
        )
    return None


class WebsiteTelegramError(WebsiteError):
    """The site's own Telegram settings could not deliver anything."""


def telegram_problem(website: Website) -> WebsiteTelegramError | None:
    """Why the site's own Telegram settings would send nowhere, if they would."""
    if website.telegram_bot_token and not website.telegram_chat_id:
        return WebsiteTelegramError(
            "telegram_bot_token needs telegram_chat_id: a site's own bot sends "
            "to the site's own chat."
        )
    if website.telegram_chat_id and not (
        website.telegram_bot_token or website.project.telegram_bot_token
    ):
        return WebsiteTelegramError(
            "telegram_chat_id needs a bot token: add telegram_bot_token to this "
            f"site, or configure Telegram on project {website.project.name!r}."
        )
    return None


class WebsiteWhatsAppError(WebsiteError):
    """The site's own WhatsApp numbers have nothing to send them from."""


def whatsapp_problem(website: Website) -> WebsiteWhatsAppError | None:
    """Why the site's own WhatsApp numbers would get nothing, if they would."""
    project = website.project
    if website.whatsapp_recipients and not (
        project.whatsapp_access_token and project.whatsapp_phone_number_id
    ):
        return WebsiteWhatsAppError(
            "whatsapp_recipients are sent from the project's business number: "
            "configure whatsapp_access_token and whatsapp_phone_number_id on "
            f"project {project.name!r} first."
        )
    return None


class MaintenanceError(WebsiteError):
    """A maintenance window that could not be scheduled as asked."""


class MaintenanceOverlapError(MaintenanceError):
    def __init__(self, window: MaintenanceWindow) -> None:
        self.window = window
        super().__init__(
            "The site already has maintenance from "
            f"{_utc(window.starts_at)} to {_utc(window.ends_at)}: end or cancel "
            "that one first."
        )


class MaintenanceNotFoundError(WebsiteError):
    def __init__(self, window_id: int) -> None:
        super().__init__(f"No upcoming maintenance window with id {window_id} on this site.")


class MaintenanceStartedError(MaintenanceError):
    def __init__(self) -> None:
        super().__init__("That maintenance has already started: end it instead.")


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


class WebsiteContentRuleError(WebsiteError):
    """A content rule on a request that comes back with no body to search."""


class WebsiteTargetError(WebsiteError):
    """A `url` that does not suit the site's check type, or DNS expected values
    that do not suit its record type."""


class WebsiteRequestHeaderError(WebsiteError):
    """A request header sent to keep its stored value, when none is stored."""


def _sealed_header(name: str, value: str) -> dict[str, str]:
    """A request header as `Website.request_headers` stores it."""
    return {"name": name, "value": encrypt_secret(value)}


def content_rule_problem(website: Website) -> WebsiteContentRuleError | None:
    """Why the site's content rules could never be checked, if they could not."""
    if not (website.must_contain or website.must_not_contain):
        return None
    if website.check_type is not CheckType.HTTP:
        what = "A ping" if website.check_type is CheckType.PING else "A DNS check"
        return WebsiteContentRuleError(
            f"{what} has no response body to search: remove must_contain / "
            "must_not_contain."
        )
    if website.method in {"HEAD", "OPTIONS"}:
        return WebsiteContentRuleError(
            f"{website.method} responses have no body to search: use GET or POST, "
            "or remove must_contain / must_not_contain."
        )
    return None


#: What a site learned about its host (certificate, domain, CDN, server),
#: forgotten when its `url` changes.
_HOST_STATE = (
    "ssl_expires_at",
    "ssl_checked_at",
    "ssl_alert_bucket",
    "ssl_subject",
    "ssl_issuer",
    "ssl_sans",
    "ssl_valid_from",
    "ssl_tls_version",
    "ssl_cipher",
    "ssl_alpn",
    "ssl_chain",
    "domain_name",
    "domain_expires_at",
    "domain_registrar",
    "domain_checked_at",
    "domain_error",
    "domain_alert_bucket",
    "domain_nameservers",
    "domain_nameservers_changed_at",
    "security_headers",
    "security_checked_at",
    "cdn",
    "cdn_checked_at",
    "server",
    "server_checked_at",
)


class WebsiteService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit_if_alertable(
        self,
        website: Website,
        *,
        check_slack: bool = False,
        check_telegram: bool = False,
        check_whatsapp: bool = False,
    ) -> None:
        """Commit a change unless it leaves the site unable to alert anyone,
        with content rules on a bodiless method, or — with `check_slack`,
        `check_telegram` and `check_whatsapp` — with Slack, Telegram or
        WhatsApp settings that send nowhere."""
        # Build the error before rolling back, which expires `website`.
        error = slack_problem(website) if check_slack else None
        if error is None and check_telegram:
            error = telegram_problem(website)
        if error is None and check_whatsapp:
            error = whatsapp_problem(website)
        if error is None:
            error = content_rule_problem(website)
        if error is None and not website.alert_channels:
            error = WebsiteNotAlertableError(website)
        if error is not None:
            # Discard the half-applied change so the session is not left dirty.
            await self.session.rollback()
            raise error
        await self.session.commit()

    async def get(self, website_id: int) -> Website:
        website = await self.session.get(Website, website_id)
        if website is None:
            raise WebsiteNotFoundError(website_id)
        return website

    async def get_visible(self, website_id: int, actor: User) -> Website:
        """Fetch a site the actor is allowed to *see*.

        Anyone but admin and DevOps who can neither see the site's project nor
        is one of its recipients gets `WebsiteNotFoundError`, so a site they
        cannot access is indistinguishable from one that does not exist.
        """
        website = await self.get(website_id)
        if can_view_all_projects(actor.role) or website.project.includes_user(actor.id):
            return website
        if any(recipient.id == actor.id for recipient in website.recipients):
            return website
        raise WebsiteNotFoundError(website_id)

    @staticmethod
    def _visible_filters(
        actor: User,
        project_id: int | None = None,
        q: str | None = None,
        check_type: CheckType | None = None,
    ) -> list:
        """What narrows both a list and a summary: what the actor may see, and
        the project, search and check type the caller picked."""
        filters = []
        if not can_view_all_projects(actor.role):
            # Scoped to the sites of projects they own or belong to, plus any
            # site they are a recipient of; empty for everyone else.
            filters.append(
                or_(
                    Website.project_id.in_(visible_project_ids(actor.id)),
                    Website.id.in_(recipient_website_ids(actor.id)),
                )
            )
        if project_id is not None:
            filters.append(Website.project_id == project_id)
        if check_type is not None:
            filters.append(Website.check_type == check_type)
        if q and q.strip():
            term = q.strip()
            filters.append(
                or_(
                    Website.name.icontains(term, autoescape=True),
                    Website.url.icontains(term, autoescape=True),
                )
            )
        return filters

    async def list(
        self,
        actor: User,
        limit: int = 50,
        offset: int = 0,
        status: WebsiteStatus | None = None,
        is_enabled: bool | None = None,
        project_id: int | None = None,
        q: str | None = None,
        sort: WebsiteSort = WebsiteSort.ID,
        check_type: CheckType | None = None,
        in_maintenance: bool | None = None,
    ) -> tuple[list[Website], int]:
        """One page of the sites `actor` is allowed to see."""
        filters = self._visible_filters(actor, project_id, q, check_type)
        if status is not None:
            filters.append(Website.status == status)
        if is_enabled is not None:
            filters.append(Website.is_enabled.is_(is_enabled))
        if in_maintenance is not None:
            under = maintenance_in_effect(func.now())
            filters.append(under if in_maintenance else ~under)

        if sort is WebsiteSort.ID:
            order = [Website.id]
        else:
            order = [func.lower(Website.name), Website.id]
            if sort is WebsiteSort.STATUS:
                # A site down for its own maintenance is not news.
                is_down = and_(
                    Website.is_enabled,
                    Website.status == WebsiteStatus.DOWN,
                    ~maintenance_in_effect(func.now()),
                )
                order.insert(0, case((is_down, 0), else_=1))

        total = await self.session.scalar(
            select(func.count()).select_from(Website).where(*filters)
        )
        rows = await self.session.scalars(
            select(Website).where(*filters).order_by(*order).limit(limit).offset(offset)
        )
        return list(rows), total or 0

    async def summary(
        self,
        actor: User,
        project_id: int | None = None,
        q: str | None = None,
        check_type: CheckType | None = None,
    ) -> WebsiteSummary:
        """Counts by state of the sites `actor` may see, in one query. A site
        in maintenance counts there whatever its status; a paused one is
        paused whatever else."""
        under = maintenance_in_effect(func.now())

        def enabled_with(status: WebsiteStatus):
            return func.count().filter(
                Website.is_enabled.is_(True), Website.status == status, ~under
            )

        row = (
            await self.session.execute(
                select(
                    func.count(),
                    enabled_with(WebsiteStatus.UP),
                    enabled_with(WebsiteStatus.DOWN),
                    enabled_with(WebsiteStatus.UNKNOWN),
                    func.count().filter(Website.is_enabled.is_(True), under),
                    func.count().filter(Website.is_enabled.is_(False)),
                )
                .select_from(Website)
                .where(*self._visible_filters(actor, project_id, q, check_type))
            )
        ).one()
        return WebsiteSummary(
            total=row[0],
            up=row[1],
            down=row[2],
            unknown=row[3],
            maintenance=row[4],
            paused=row[5],
        )

    async def create(
        self, payload: WebsiteCreate, project: Project, created_by_id: int | None
    ) -> Website:
        """Register a site under `project`, which the caller has already been
        checked against."""
        url = str(payload.url)
        record_type = payload.dns_record_type
        existing = await self.session.scalar(
            select(Website).where(
                Website.url == url,
                Website.check_type == payload.check_type,
                Website.dns_record_type.is_not_distinct_from(record_type),
            )
        )
        if existing is not None:
            raise DuplicateWebsiteError(url, record_type)

        recipients = await resolve_users(self.session, payload.recipient_ids)
        website = Website(
            **payload.model_dump(
                exclude={
                    "url",
                    "alert_emails",
                    "recipient_ids",
                    "project_id",
                    "slack_bot_token",
                    "telegram_bot_token",
                    "request_headers",
                }
            ),
            url=url,
            alert_emails=[str(email) for email in payload.alert_emails],
            request_headers=[
                _sealed_header(header.name, header.value)
                for header in payload.request_headers
            ],
            slack_bot_token=(
                encrypt_secret(payload.slack_bot_token)
                if payload.slack_bot_token
                else None
            ),
            telegram_bot_token=(
                encrypt_secret(payload.telegram_bot_token)
                if payload.telegram_bot_token
                else None
            ),
            project=project,
            created_by_id=created_by_id,
        )
        website.recipients = recipients
        self.session.add(website)
        try:
            await self._commit_if_alertable(
                website, check_slack=True, check_telegram=True, check_whatsapp=True
            )
        except IntegrityError as exc:
            await self.session.rollback()
            raise DuplicateWebsiteError(url, record_type) from exc
        await self.session.refresh(website)
        return website

    async def update(self, website_id: int, payload: WebsiteUpdate) -> Website:
        website = await self.get(website_id)
        changes = payload.model_dump(exclude_unset=True)

        if changes.get("url") is not None:
            # What a valid target looks like depends on the site's check type,
            # which the payload cannot change.
            try:
                changes["url"] = monitor_target(website.check_type, changes["url"])
            except ValueError as exc:
                raise WebsiteTargetError(f"url: {exc}") from exc
        if website.check_type is CheckType.DNS:
            self._dns_changes(website, changes)
        else:
            changes.pop("dns_record_type", None)
            changes.pop("dns_expected_values", None)
        if website.check_type is CheckType.HTTP:
            self._header_changes(website, changes)
        else:
            changes.pop("request_headers", None)
        if "alert_emails" in changes and changes["alert_emails"] is not None:
            changes["alert_emails"] = [str(e) for e in changes["alert_emails"]]
        for not_null in (
            "inherit_project_recipients",
            "retries_on_failure",
            "ping_count",
            "dns_record_type",
            "dns_expected_values",
        ):
            if changes.get(not_null) is None:
                # Optional in the payload but NOT NULL in the table.
                changes.pop(not_null, None)
        if "slack_channel_id" in changes and not changes["slack_channel_id"]:
            changes["slack_channel_id"] = None
            # The site's own token only ever posts to the site's own channel,
            # so removing the channel removes the token with it.
            if not changes.get("slack_bot_token"):
                changes["slack_bot_token"] = None
        if changes.get("slack_bot_token"):
            changes["slack_bot_token"] = encrypt_secret(changes["slack_bot_token"])
        # Only re-validate Slack when it changes, so a site saved before the
        # check existed can still be edited.
        slack_changed = "slack_bot_token" in changes or (
            "slack_channel_id" in changes
            and changes["slack_channel_id"] != website.slack_channel_id
        )
        if "telegram_chat_id" in changes and not changes["telegram_chat_id"]:
            changes["telegram_chat_id"] = None
            # Like Slack: the site's own bot only ever sends to the site's own
            # chat, so removing the chat removes the token with it.
            if not changes.get("telegram_bot_token"):
                changes["telegram_bot_token"] = None
        if changes.get("telegram_bot_token"):
            changes["telegram_bot_token"] = encrypt_secret(changes["telegram_bot_token"])
        telegram_changed = "telegram_bot_token" in changes or (
            "telegram_chat_id" in changes
            and changes["telegram_chat_id"] != website.telegram_chat_id
        )
        if "whatsapp_recipients" in changes and changes["whatsapp_recipients"] is None:
            # NOT NULL in the table; empty means "the project's numbers".
            changes["whatsapp_recipients"] = []
        whatsapp_changed = (
            "whatsapp_recipients" in changes
            and changes["whatsapp_recipients"] != website.whatsapp_recipients
        )

        if "url" in changes and changes["url"] != website.url:
            # The certificate and domain state describe the old host.
            # Clearing it makes the next check read the new one and warn
            # afresh.
            for field in _HOST_STATE:
                setattr(website, field, None)
        if website.check_type is CheckType.DNS and (
            changes.get("url", website.url) != website.url
            or changes.get("dns_record_type", website.dns_record_type)
            != website.dns_record_type
        ):
            # The records learned were another record's: learn this one's
            # afresh rather than report the difference as a change.
            website.dns_records = None

        for field, value in changes.items():
            setattr(website, field, value)

        # Read before a rollback expires them.
        url, record_type = website.url, website.dns_record_type
        try:
            await self._commit_if_alertable(
                website,
                check_slack=slack_changed,
                check_telegram=telegram_changed,
                check_whatsapp=whatsapp_changed,
            )
        except IntegrityError as exc:
            await self.session.rollback()
            raise DuplicateWebsiteError(url, record_type) from exc
        await self.session.refresh(website)
        return website

    @staticmethod
    def _dns_changes(website: Website, changes: dict) -> None:
        """Validate a DNS check's record type and expected values together, in
        place: what an expected value may be depends on the record type."""
        record_type = changes.get("dns_record_type") or website.dns_record_type
        type_changed = record_type != website.dns_record_type
        values = changes.get("dns_expected_values")
        if values is None:
            if not type_changed:
                return
            # Values pinned for another record type say nothing about this one.
            values = []
        try:
            changes["dns_expected_values"] = dns_values(record_type, values)
        except ValueError as exc:
            raise WebsiteTargetError(f"dns_expected_values: {exc}") from exc

    @staticmethod
    def _header_changes(website: Website, changes: dict) -> None:
        """Encrypt the request headers sent, in place. One sent without a
        value keeps the value stored under its name, so a client that is never
        shown the values can still save the list back."""
        if "request_headers" not in changes:
            return
        stored = {h["name"].lower(): h["value"] for h in website.request_headers}
        sealed = []
        # Null is NOT NULL in the table; it clears them, like [].
        for header in changes["request_headers"] or []:
            if header["value"] is not None:
                sealed.append(_sealed_header(header["name"], header["value"]))
            elif (value := stored.get(header["name"].lower())) is not None:
                sealed.append({"name": header["name"], "value": value})
            else:
                raise WebsiteRequestHeaderError(
                    f"request_headers: {header['name']} has no stored value to "
                    "keep: give its value."
                )
        changes["request_headers"] = sealed

    async def add_recipients(self, website_id: int, user_ids: list[int]) -> Website:
        """Idempotent for anyone already a recipient."""
        website = await self.get(website_id)
        existing = {user.id for user in website.recipients}
        for user in await resolve_users(self.session, user_ids):
            if user.id not in existing:
                website.recipients.append(user)
        await self.session.commit()
        await self.session.refresh(website)
        return website

    async def remove_recipient(self, website_id: int, user_id: int) -> Website:
        """Removing a non-recipient is a no-op."""
        website = await self.get(website_id)
        website.recipients = [u for u in website.recipients if u.id != user_id]
        await self._commit_if_alertable(website)
        await self.session.refresh(website)
        return website

    async def delete(self, website_id: int) -> None:
        website = await self.get(website_id)
        await self.session.delete(website)
        await self.session.commit()

    async def due_for_check(self, now: datetime | None = None) -> list[Website]:
        """Enabled websites whose interval has elapsed since the last check,
        and that are not in maintenance.

        The interval is per-row, so the deadline is computed in SQL:
        `last_checked_at + interval '1 second' * check_interval_seconds <= now`.
        A site whose maintenance just ended is overdue, so it is checked on
        the next tick.
        """
        now = now or datetime.now(UTC)
        next_due_at = Website.last_checked_at + (
            literal_column("interval '1 second'") * Website.check_interval_seconds
        )
        rows = await self.session.scalars(
            select(Website)
            .where(
                Website.is_enabled.is_(True),
                ~maintenance_in_effect(now),
                or_(
                    Website.last_checked_at.is_(None),  # never checked
                    next_due_at <= now,
                ),
            )
            .order_by(Website.last_checked_at.asc().nulls_first())
        )
        return list(rows)

    # -- maintenance ---------------------------------------------------------

    async def in_maintenance(self, website_id: int, now: datetime | None = None) -> bool:
        """Whether a window covers `now`. Read afresh, not from the loaded
        site, whose windows may have changed since."""
        now = now or datetime.now(UTC)
        return bool(
            await self.session.scalar(
                select(Website.id).where(Website.id == website_id, maintenance_in_effect(now))
            )
        )

    async def schedule_maintenance(
        self,
        website_id: int,
        payload: MaintenanceCreate,
        created_by_id: int | None,
        now: datetime | None = None,
    ) -> Website:
        """Start maintenance now, or schedule it for later. A start already
        past is now: checks and alerts cannot be taken back."""
        website = await self.get(website_id)
        now = now or datetime.now(UTC)
        starts_at = max(payload.starts_at or now, now)
        ends_at = payload.ends_at or starts_at + timedelta(minutes=payload.duration_minutes)
        if ends_at <= starts_at:
            raise MaintenanceError("ends_at must be later than starts_at, and in the future.")
        if ends_at - starts_at > timedelta(minutes=MAX_MAINTENANCE_MINUTES):
            raise MaintenanceError(
                f"Maintenance lasts at most {MAX_MAINTENANCE_MINUTES // (24 * 60)} "
                "days: pause the site for longer."
            )
        clash = await self.session.scalar(
            select(MaintenanceWindow)
            .where(
                MaintenanceWindow.website_id == website_id,
                MaintenanceWindow.starts_at < ends_at,
                MaintenanceWindow.ends_at > starts_at,
            )
            .order_by(MaintenanceWindow.starts_at)
            .limit(1)
        )
        if clash is not None:
            raise MaintenanceOverlapError(clash)

        self.session.add(
            MaintenanceWindow(
                website_id=website_id,
                starts_at=starts_at,
                ends_at=ends_at,
                reason=payload.reason,
                created_by_id=created_by_id,
            )
        )
        await self.session.commit()
        await self.session.refresh(website)
        return website

    async def end_maintenance(self, website_id: int, now: datetime | None = None) -> Website:
        """End the window in effect now, keeping it on record as ended now.
        Does nothing when there is none: it may have just run out."""
        website = await self.get(website_id)
        now = now or datetime.now(UTC)
        window = await self.session.scalar(
            select(MaintenanceWindow).where(
                MaintenanceWindow.website_id == website_id,
                MaintenanceWindow.starts_at <= now,
                MaintenanceWindow.ends_at > now,
            )
        )
        if window is not None:
            if window.starts_at == now:
                # Nothing to keep, and `ends_at` must be after `starts_at`.
                await self.session.delete(window)
            else:
                window.ends_at = now
            await self.session.commit()
        await self.session.refresh(website)
        return website

    async def cancel_maintenance(
        self, website_id: int, window_id: int, now: datetime | None = None
    ) -> Website:
        """Delete a window that has not started yet."""
        website = await self.get(website_id)
        now = now or datetime.now(UTC)
        window = await self.session.get(MaintenanceWindow, window_id)
        if window is None or window.website_id != website_id or window.ends_at <= now:
            raise MaintenanceNotFoundError(window_id)
        if window.starts_at <= now:
            raise MaintenanceStartedError()
        await self.session.delete(window)
        await self.session.commit()
        await self.session.refresh(website)
        return website

    async def purge_old_maintenance(self, now: datetime | None = None) -> int:
        """Delete windows that ended more than CHECK_RETENTION_DAYS ago, like
        the feed. Called on every tick; returns how many were deleted."""
        before = (now or datetime.now(UTC)) - timedelta(days=settings.CHECK_RETENTION_DAYS)
        result = await self.session.execute(
            delete(MaintenanceWindow).where(MaintenanceWindow.ends_at < before)
        )
        await self.session.commit()
        return result.rowcount or 0

    async def recent_checks(
        self, website_id: int, actor: User, limit: int = 50
    ) -> list[WebsiteCheck]:
        # 404 for an unknown site, and for one the actor may not see.
        await self.get_visible(website_id, actor)
        rows = await self.session.scalars(
            select(WebsiteCheck)
            .where(WebsiteCheck.website_id == website_id)
            .order_by(WebsiteCheck.checked_at.desc())
            .limit(limit)
        )
        return list(rows)

    async def purge_old_checks(
        self, before: datetime, *, batch_size: int = 5_000, max_batches: int = 20
    ) -> int:
        """Delete checks older than `before`, one committed batch at a time.

        Stops after `max_batches`, so the first purge of a large backlog is
        spread over several scheduler ticks rather than holding one for
        minutes. Returns how many were deleted. `HistoryService.purge` decides
        `before`; call that rather than this.
        """
        deleted = 0
        for _ in range(max_batches):
            batch = (
                select(WebsiteCheck.id)
                .where(WebsiteCheck.checked_at < before)
                .limit(batch_size)
                .scalar_subquery()
            )
            result = await self.session.execute(
                delete(WebsiteCheck).where(WebsiteCheck.id.in_(batch))
            )
            await self.session.commit()
            deleted += result.rowcount or 0
            if (result.rowcount or 0) < batch_size:
                break
        return deleted

    async def events(
        self, actor: User, *, after_id: int | None = None, limit: int = 20
    ) -> tuple[list[WebsiteEvent], int]:
        """The alert feed over the sites `actor` may see, newest first, and how
        many events match. With `after_id`, only the events after that one."""
        filters = self._visible_filters(actor)
        if after_id is not None:
            filters.append(WebsiteEvent.id > after_id)
        total = await self.session.scalar(
            select(func.count(WebsiteEvent.id)).join(WebsiteEvent.website).where(*filters)
        )
        rows = await self.session.scalars(
            select(WebsiteEvent)
            .join(WebsiteEvent.website)
            .options(contains_eager(WebsiteEvent.website))
            .where(*filters)
            .order_by(WebsiteEvent.id.desc())
            .limit(limit)
        )
        return list(rows), total or 0

    async def purge_old_events(self, now: datetime | None = None) -> int:
        """Delete feed events older than CHECK_RETENTION_DAYS. Called on every
        tick; returns how many were deleted."""
        before = (now or datetime.now(UTC)) - timedelta(days=settings.CHECK_RETENTION_DAYS)
        result = await self.session.execute(
            delete(WebsiteEvent).where(WebsiteEvent.occurred_at < before)
        )
        await self.session.commit()
        return result.rowcount or 0
