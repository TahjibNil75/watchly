"""The monthly uptime report: computing it from stored checks, and sending it.

Numbers come from `website_checks`, so the purge of old checks keeps whatever
last month's report may still read: see `report_data_floor`.

How the figures are defined, since a report is only worth sending if people can
trust it:

- **Uptime** is the share of checks that succeeded. Checks are evenly spaced,
  so this tracks time-weighted uptime, and unlike a time-weighted figure it
  does not change if a site was paused for a few days.
- **An outage** is a run of failed checks. Its downtime runs from the first
  failed check to the next successful one — the same instant the recovery alert
  fires. An outage that began before the month counts from the 1st; one still
  open at the end counts to the month's end, but no further than one interval
  past the last check actually made, so a site paused mid-outage is not billed
  for time nobody was watching.
- **Incidents** are the outages that overlapped the month.
"""

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.monitoring.alerts.base import Alerter
from app.monitoring.alerts.events import ReportEvent, SiteStats
from app.monitoring.notifications.dispatcher import Notifier
from app.monitoring.notifications.models import ReportDelivery
from app.monitoring.notifications.recipients import project_recipients, slack_target
from app.monitoring.projects.models import Project
from app.monitoring.websites.models import Website, WebsiteCheck

logger = logging.getLogger(__name__)

#: How long after the scheduled moment a missed report is still sent. Covers a
#: restart or an outage of Watchly itself around the 1st, without mailing last
#: month's report to someone who has just deployed the feature on the 20th.
CATCH_UP = timedelta(days=3)

#: How far before a month the report looks for an outage carried into it: two
#: intervals (see `_site_stats`) of the slowest check a site may have, a day.
CARRY_OVER_LOOKBACK = timedelta(days=2)


@dataclass(frozen=True, slots=True)
class Period:
    """A calendar month in UTC: `start` inclusive, `end` exclusive."""

    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        return self.start.strftime("%Y-%m")


class NoReportDataError(Exception):
    def __init__(self, project: Project, period: Period) -> None:
        super().__init__(
            f"{project.name!r} has no checks in {period.start:%B %Y}, so there is "
            "nothing to report."
        )


class PeriodNotRetainedError(Exception):
    def __init__(self, period: Period) -> None:
        super().__init__(
            f"Checks from {period.start:%B %Y} are older than the retained check "
            "history, so its figures would be incomplete."
        )


class PeriodNotOverError(Exception):
    def __init__(self, period: Period) -> None:
        super().__init__(f"{period.start:%B %Y} has not ended yet.")


def month_period(year: int, month: int) -> Period:
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
    return Period(start, end)


def previous_month(now: datetime) -> Period:
    first_of_this = datetime(now.year, now.month, 1, tzinfo=UTC)
    last_of_previous = first_of_this - timedelta(days=1)
    return month_period(last_of_previous.year, last_of_previous.month)


def report_data_floor(now: datetime) -> datetime:
    """The oldest check last month's report can still need at `now`.

    That report goes out on MONTHLY_REPORT_DAY, may catch up for `CATCH_UP`
    after, and can be sent by hand (`send_now`) any time this month — so the
    whole of last month, and the lookback before it, stays until this month
    ends. Up to about 64 days with the report on the 28th.
    """
    return previous_month(now.astimezone(UTC)).start - CARRY_OVER_LOOKBACK


def retained_checks_since(now: datetime) -> datetime:
    """Raw checks newer than this are kept: CHECK_RETENTION_DAYS, or what last
    month's report may still read, whichever reaches further back."""
    return min(now - timedelta(days=settings.CHECK_RETENTION_DAYS), report_data_floor(now))


def scheduled_period(now: datetime) -> Period | None:
    """The month whose report is due to go out at `now`, or None.

    Due from MONTHLY_REPORT_DAY at MONTHLY_REPORT_HOUR_UTC, for `CATCH_UP`.
    """
    now = now.astimezone(UTC)
    due_at = datetime(
        now.year,
        now.month,
        settings.MONTHLY_REPORT_DAY,
        settings.MONTHLY_REPORT_HOUR_UTC,
        tzinfo=UTC,
    )
    if not due_at <= now < due_at + CATCH_UP:
        return None
    return previous_month(now)


def compute_site_stats(
    *,
    website_id: int,
    name: str,
    url: str,
    interval_seconds: int,
    checks: list[tuple[datetime, bool, int | None]],
    carried_in_outage: bool,
    period: Period,
) -> SiteStats | None:
    """One site's month from its checks (`(checked_at, is_up, ms)`, oldest first).

    Pure, so the definitions in the module docstring can be tested without a
    database. Returns None when there were no checks.
    """
    if not checks:
        return None

    downtime = 0.0
    longest = 0.0
    incidents = 0
    run_start: datetime | None = None
    if carried_in_outage:
        run_start, incidents = period.start, 1

    for checked_at, is_up, _ in checks:
        if not is_up:
            if run_start is None:
                run_start = checked_at
                incidents += 1
        elif run_start is not None:
            length = (checked_at - run_start).total_seconds()
            downtime += length
            longest = max(longest, length)
            run_start = None

    if run_start is not None:
        end = min(period.end, checks[-1][0] + timedelta(seconds=interval_seconds))
        length = max((end - run_start).total_seconds(), 0.0)
        downtime += length
        longest = max(longest, length)

    # Response times of successful checks only: a failed check's "time" is how
    # long it took to give up, which would drag the average toward the timeout.
    times = sorted(ms for _, is_up, ms in checks if is_up and ms is not None)
    return SiteStats(
        website_id=website_id,
        name=name,
        url=url,
        checks=len(checks),
        up_checks=sum(1 for _, is_up, _ in checks if is_up),
        downtime_seconds=downtime,
        incidents=incidents,
        longest_outage_seconds=longest,
        avg_response_ms=round(sum(times) / len(times)) if times else None,
        p95_response_ms=times[max(math.ceil(len(times) * 0.95) - 1, 0)] if times else None,
    )


class ReportService:
    def __init__(self, session: AsyncSession, alerters: list[Alerter] | None = None) -> None:
        self.session = session
        self.notifier = Notifier(session, alerters)

    # -- building ----------------------------------------------------------

    async def _site_stats(self, website: Website, period: Period) -> SiteStats | None:
        rows = (
            await self.session.execute(
                select(WebsiteCheck.checked_at, WebsiteCheck.is_up, WebsiteCheck.response_time_ms)
                .where(
                    WebsiteCheck.website_id == website.id,
                    WebsiteCheck.checked_at >= period.start,
                    WebsiteCheck.checked_at < period.end,
                )
                .order_by(WebsiteCheck.checked_at)
            )
        ).all()
        if not rows:
            return None

        before = (
            await self.session.execute(
                select(WebsiteCheck.checked_at, WebsiteCheck.is_up)
                .where(
                    WebsiteCheck.website_id == website.id,
                    WebsiteCheck.checked_at < period.start,
                )
                .order_by(desc(WebsiteCheck.checked_at))
                .limit(1)
            )
        ).first()
        # Only a *recent* failure means the outage carried over the boundary;
        # after a long gap in checking we cannot say what happened in between.
        carried = bool(
            before is not None
            and not before.is_up
            and period.start - before.checked_at
            <= timedelta(seconds=2 * website.check_interval_seconds)
        )
        return compute_site_stats(
            website_id=website.id,
            name=website.name,
            url=website.url,
            interval_seconds=website.check_interval_seconds,
            checks=[(r.checked_at, r.is_up, r.response_time_ms) for r in rows],
            carried_in_outage=carried,
            period=period,
        )

    async def build(self, project: Project, period: Period) -> ReportEvent | None:
        """The project's report, or None if none of its sites has any checks."""
        websites = list(
            await self.session.scalars(
                select(Website).where(Website.project_id == project.id).order_by(Website.name)
            )
        )
        stats: list[SiteStats] = []
        for website in websites:
            site = await self._site_stats(website, period)
            if site is not None:
                stats.append(site)
        if not stats:
            return None
        return ReportEvent(
            project_id=project.id,
            project_name=project.name,
            period_start=period.start,
            period_end=period.end,
            sites=stats,
            sites_without_data=len(websites) - len(stats),
            recipients=project_recipients(project),
            slack=slack_target(project),
        )

    # -- sending -----------------------------------------------------------

    async def send_now(
        self, project: Project, period: Period, now: datetime | None = None
    ) -> tuple[ReportEvent, tuple[str, ...]]:
        """Build and send one project's report right away.

        Honours the project's notification switches, like the scheduled one.
        Not recorded as a delivery, so it does not stop the scheduled report.
        """
        if period.end > (now or datetime.now(UTC)):
            raise PeriodNotOverError(period)
        event = await self.build(project, period)
        if event is None:
            raise NoReportDataError(project, period)
        return event, await self.notifier.dispatch(event)

    async def export(self, project: Project, period: Period, now: datetime | None = None) -> ReportEvent:
        """The project's report for a month that has ended and whose checks are
        all still stored, for download. Sends nothing."""
        now = now or datetime.now(UTC)
        if period.end > now:
            raise PeriodNotOverError(period)
        if period.start < retained_checks_since(now):
            raise PeriodNotRetainedError(period)
        event = await self.build(project, period)
        if event is None:
            raise NoReportDataError(project, period)
        return event

    async def _claim(self, project_id: int, period: Period) -> ReportDelivery | None:
        """Record that this report is being handled, before sending it.

        A unique row per (project, month) is what stops a slow or crashing
        mail server, or a second worker, from sending the same report twice.
        """
        delivery = ReportDelivery(project_id=project_id, period_start=period.start.date())
        self.session.add(delivery)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            return None
        return delivery

    async def send_due_reports(self, now: datetime | None = None) -> int:
        """Send last month's report for every active project that has not had it.

        Called on every scheduler tick; does nothing outside the send window.
        Returns how many reports were sent.
        """
        if not settings.MONTHLY_REPORTS_ENABLED:
            return 0
        period = scheduled_period(now or datetime.now(UTC))
        if period is None:
            return 0

        pending_ids = list(
            await self.session.scalars(
                select(Project.id)
                .where(
                    Project.is_active.is_(True),
                    ~exists().where(
                        ReportDelivery.project_id == Project.id,
                        ReportDelivery.period_start == period.start.date(),
                    ),
                )
                .order_by(Project.id)
            )
        )
        sent = 0
        # Ids, re-fetching each project: a rollback (a lost claim, a failed
        # send) expires every loaded object, and touching an expired one from
        # async code raises MissingGreenlet.
        for project_id in pending_ids:
            try:
                project = await self.session.get(Project, project_id)
                if project is None:
                    continue
                delivery = await self._claim(project_id, period)
                if delivery is None:
                    continue  # another worker got there first
                event = await self.build(project, period)
                if event is None:
                    logger.info(
                        "No checks for project %r in %s; no report.", project.name, period.label
                    )
                    continue
                delivered = await self.notifier.dispatch(event)
                delivery.channels = list(delivered)
                await self.session.commit()
                sent += 1
            except Exception:
                # One project's failure must not keep the rest from their report.
                logger.exception("Monthly report failed for project id %s", project_id)
                await self.session.rollback()
        if sent:
            logger.info("Sent %d monthly report(s) for %s.", sent, period.label)
        return sent
