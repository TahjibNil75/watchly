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
    Website,
    WebsiteCheck,
    WebsiteEvent,
    WebsiteStatus,
    recipient_website_ids,
)
from app.monitoring.websites.schemas import (
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
            f"set up Slack or Telegram on this site or on project {website.project.name!r}."
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


class WebsiteContentRuleError(WebsiteError):
    """A content rule on a request that comes back with no body to search."""


class WebsiteTargetError(WebsiteError):
    """A `url` that does not suit the site's check type, or DNS expected values
    that do not suit its record type."""


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


class WebsiteService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit_if_alertable(
        self,
        website: Website,
        *,
        check_slack: bool = False,
        check_telegram: bool = False,
    ) -> None:
        """Commit a change unless it leaves the site unable to alert anyone,
        with content rules on a bodiless method, or — with `check_slack` and
        `check_telegram` — with Slack or Telegram settings that post nowhere."""
        # Build the error before rolling back, which expires `website`.
        error = slack_problem(website) if check_slack else None
        if error is None and check_telegram:
            error = telegram_problem(website)
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
    ) -> tuple[list[Website], int]:
        """One page of the sites `actor` is allowed to see."""
        filters = self._visible_filters(actor, project_id, q, check_type)
        if status is not None:
            filters.append(Website.status == status)
        if is_enabled is not None:
            filters.append(Website.is_enabled.is_(is_enabled))

        if sort is WebsiteSort.ID:
            order = [Website.id]
        else:
            order = [func.lower(Website.name), Website.id]
            if sort is WebsiteSort.STATUS:
                is_down = and_(Website.is_enabled, Website.status == WebsiteStatus.DOWN)
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
        """Counts by state of the sites `actor` may see, in one query."""

        def enabled_with(status: WebsiteStatus):
            return func.count().filter(
                Website.is_enabled.is_(True), Website.status == status
            )

        row = (
            await self.session.execute(
                select(
                    func.count(),
                    enabled_with(WebsiteStatus.UP),
                    enabled_with(WebsiteStatus.DOWN),
                    enabled_with(WebsiteStatus.UNKNOWN),
                    func.count().filter(Website.is_enabled.is_(False)),
                )
                .select_from(Website)
                .where(*self._visible_filters(actor, project_id, q, check_type))
            )
        ).one()
        return WebsiteSummary(
            total=row[0], up=row[1], down=row[2], unknown=row[3], paused=row[4]
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
                }
            ),
            url=url,
            alert_emails=[str(email) for email in payload.alert_emails],
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
                website, check_slack=True, check_telegram=True
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

        if "url" in changes and changes["url"] != website.url:
            # The certificate state describes the old host. Clearing it makes
            # the next check read the new one and warn afresh.
            website.ssl_expires_at = None
            website.ssl_checked_at = None
            website.ssl_alert_bucket = None
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
                website, check_slack=slack_changed, check_telegram=telegram_changed
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
        """Enabled websites whose interval has elapsed since the last check.

        The interval is per-row, so the deadline is computed in SQL:
        `last_checked_at + interval '1 second' * check_interval_seconds <= now`.
        """
        now = now or datetime.now(UTC)
        next_due_at = Website.last_checked_at + (
            literal_column("interval '1 second'") * Website.check_interval_seconds
        )
        rows = await self.session.scalars(
            select(Website)
            .where(
                Website.is_enabled.is_(True),
                or_(
                    Website.last_checked_at.is_(None),  # never checked
                    next_due_at <= now,
                ),
            )
            .order_by(Website.last_checked_at.asc().nulls_first())
        )
        return list(rows)

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
