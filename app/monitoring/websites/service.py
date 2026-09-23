"""CRUD for monitored websites. No HTTP concerns, no probing."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import can_view_all_projects
from app.db.models.user import User
from app.monitoring.projects.models import Project, member_project_ids
from app.monitoring.projects.service import resolve_users
from app.monitoring.websites.models import (
    Website,
    WebsiteCheck,
    WebsiteStatus,
    recipient_website_ids,
)
from app.monitoring.websites.schemas import WebsiteCreate, WebsiteUpdate


class WebsiteError(Exception):
    """Base class for website-management failures."""


class WebsiteNotFoundError(WebsiteError):
    def __init__(self, website_id: int) -> None:
        self.website_id = website_id
        super().__init__(f"No monitored website with id {website_id}.")


class DuplicateWebsiteError(WebsiteError):
    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"{url} is already being monitored.")


class WebsiteNotAlertableError(WebsiteError):
    """The site would have no way to raise an alert."""

    def __init__(self, website: Website) -> None:
        super().__init__(
            f"{website.name!r} would have no alert channel. Add recipient_ids or "
            "alert_emails, set inherit_project_recipients back to true, or "
            f"configure Slack on project {website.project.name!r}."
        )


class WebsiteService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit_if_alertable(self, website: Website) -> None:
        """Commit a change unless it leaves the site unable to alert anyone."""
        if not website.alert_channels:
            # Discard the half-applied change so the session is not left dirty.
            error = WebsiteNotAlertableError(website)
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

        A viewer or developer who is neither in the site's project nor one of
        its recipients gets `WebsiteNotFoundError`, so a site they cannot
        access is indistinguishable from one that does not exist.
        """
        website = await self.get(website_id)
        if can_view_all_projects(actor.role):
            return website
        people = [*website.project.members, *website.recipients]
        if any(person.id == actor.id for person in people):
            return website
        raise WebsiteNotFoundError(website_id)

    async def list(
        self,
        actor: User,
        limit: int = 50,
        offset: int = 0,
        status: WebsiteStatus | None = None,
        is_enabled: bool | None = None,
        project_id: int | None = None,
    ) -> tuple[list[Website], int]:
        """One page of the sites `actor` is allowed to see."""
        filters = []
        if not can_view_all_projects(actor.role):
            # Scoped to their projects' sites plus any site they are a
            # recipient of; empty for everyone else.
            filters.append(
                or_(
                    Website.project_id.in_(member_project_ids(actor.id)),
                    Website.id.in_(recipient_website_ids(actor.id)),
                )
            )
        if status is not None:
            filters.append(Website.status == status)
        if is_enabled is not None:
            filters.append(Website.is_enabled.is_(is_enabled))
        if project_id is not None:
            filters.append(Website.project_id == project_id)

        total = await self.session.scalar(
            select(func.count()).select_from(Website).where(*filters)
        )
        rows = await self.session.scalars(
            select(Website)
            .where(*filters)
            .order_by(Website.id)
            .limit(limit)
            .offset(offset)
        )
        return list(rows), total or 0

    async def create(
        self, payload: WebsiteCreate, project: Project, created_by_id: int | None
    ) -> Website:
        """Register a site under `project`, which the caller has already been
        checked against."""
        url = str(payload.url)
        existing = await self.session.scalar(select(Website).where(Website.url == url))
        if existing is not None:
            raise DuplicateWebsiteError(url)

        recipients = await resolve_users(self.session, payload.recipient_ids)
        website = Website(
            **payload.model_dump(
                exclude={"url", "alert_emails", "recipient_ids", "project_id"}
            ),
            url=url,
            alert_emails=[str(email) for email in payload.alert_emails],
            project=project,
            created_by_id=created_by_id,
        )
        website.recipients = recipients
        self.session.add(website)
        try:
            await self._commit_if_alertable(website)
        except IntegrityError as exc:
            await self.session.rollback()
            raise DuplicateWebsiteError(url) from exc
        await self.session.refresh(website)
        return website

    async def update(self, website_id: int, payload: WebsiteUpdate) -> Website:
        website = await self.get(website_id)
        changes = payload.model_dump(exclude_unset=True)

        if "url" in changes and changes["url"] is not None:
            changes["url"] = str(changes["url"])
        if "alert_emails" in changes and changes["alert_emails"] is not None:
            changes["alert_emails"] = [str(e) for e in changes["alert_emails"]]
        if changes.get("inherit_project_recipients") is None:
            # Optional in the payload but NOT NULL in the table.
            changes.pop("inherit_project_recipients", None)

        for field, value in changes.items():
            setattr(website, field, value)

        try:
            await self._commit_if_alertable(website)
        except IntegrityError as exc:
            await self.session.rollback()
            raise DuplicateWebsiteError(str(changes.get("url", website.url))) from exc
        await self.session.refresh(website)
        return website

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

    async def purge_old_checks(self, older_than_days: int = 30) -> int:
        """Trim check history. Wire to a cron; nothing calls it automatically."""
        from sqlalchemy import delete

        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        result = await self.session.execute(
            delete(WebsiteCheck).where(WebsiteCheck.checked_at < cutoff)
        )
        await self.session.commit()
        return result.rowcount or 0
