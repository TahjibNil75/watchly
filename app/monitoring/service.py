"""Monitoring orchestration: probe due websites, track outages, fire alerts.

The alert policy, in one place:

    UP  -> DOWN   first failure          -> alert immediately (attempt 1)
    DOWN -> DOWN  still failing          -> alert while attempt <= max_down_alerts
    DOWN -> DOWN  past the alert budget  -> keep checking, stay silent
    DOWN -> UP    recovered              -> one recovery alert with total downtime
    UP  -> UP     healthy                -> silent

With the default interval of 5 minutes and `max_down_alerts = 4`, an outage
produces an immediate alert plus three follow-ups (at ~5, ~10 and ~15 minutes),
each stating the cumulative downtime, then silence until recovery.

Two softer signals ride on the same checks, neither of which is an outage:

    slow   UP and slower than the threshold for SLOW_RESPONSE_CHECKS checks in
           a row -> one alert, then quiet for SLOW_ALERT_COOLDOWN_SECONDS
    ssl    the certificate crosses a SSL_EXPIRY_ALERT_DAYS threshold -> one
           alert per threshold, and one if it expires; renewing re-arms them
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.monitoring.alerts.base import (
    Alerter,
    Notification,
    NotificationKind,
    SlackTarget,
    WebsiteSnapshot,
)
from app.monitoring.alerts.events import OutageEvent, SlowResponseEvent, SslExpiryEvent
from app.monitoring.notifications.dispatcher import Notifier, default_alerters
from app.monitoring.notifications.recipients import dedupe_emails, slack_target
from app.monitoring.websites.checker import CheckResult, check_website
from app.monitoring.websites.models import Website, WebsiteCheck, WebsiteStatus
from app.monitoring.websites.service import WebsiteService

logger = logging.getLogger(__name__)

__all__ = ["CheckOutcome", "MonitoringService", "default_alerters", "ssl_alert_bucket"]

#: A certificate whose end date moved out by more than this has been replaced.
_RENEWAL_JUMP = timedelta(hours=12)


@dataclass(slots=True)
class CheckOutcome:
    """What one probe did, for callers and tests to inspect."""

    website: Website
    check: WebsiteCheck
    result: CheckResult
    #: The first notification this probe raised, if any.
    alert: NotificationKind | None = None
    delivered_by: tuple[str, ...] = ()


def ssl_alert_bucket(expires_at: datetime, now: datetime) -> int | None:
    """Which SSL_EXPIRY_ALERT_DAYS threshold a certificate has crossed.

    The smallest threshold that the time remaining fits under, 0 once expired,
    None while it is still further out than every threshold.
    """
    remaining_days = (expires_at - now).total_seconds() / 86_400
    if remaining_days <= 0:
        return 0
    for threshold in sorted(settings.SSL_EXPIRY_ALERT_DAYS):
        if remaining_days <= threshold:
            return threshold
    return None


class MonitoringService:
    """Runs checks and turns their results into alerts."""

    def __init__(
        self,
        session: AsyncSession,
        alerters: list[Alerter] | None = None,
    ) -> None:
        self.session = session
        self.websites = WebsiteService(session)
        self.notifier = Notifier(session, alerters)
        self.alerters = self.notifier.alerters

    # -- recipients --------------------------------------------------------

    def recipients_for(self, website: Website) -> tuple[str, ...]:
        """Who hears about this site, de-duplicated.

        In order: the project's members and extra_emails (unless the site has
        `inherit_project_recipients` off), the site's own recipient users and
        alert_emails, and the global ALERT_DEFAULT_EMAILS. Suspended users are
        excluded wherever they appear.
        """
        return dedupe_emails(website.alert_recipients, settings.ALERT_DEFAULT_EMAILS)

    def slack_target_for(self, website: Website) -> SlackTarget | None:
        """The channel this site's alerts go to, with the token decrypted."""
        return slack_target(
            website.project, website.slack_channel_id, website.slack_bot_token
        )

    # -- alert dispatch ----------------------------------------------------

    async def dispatch(self, event: Notification) -> tuple[str, ...]:
        """Send `event` on every channel that is configured and switched on for
        its project. Never raises."""
        return await self.notifier.dispatch(event)

    # -- the state machine -------------------------------------------------

    async def record_result(
        self, website: Website, result: CheckResult
    ) -> CheckOutcome:
        """Persist a probe, advance the website's state, and alert if warranted."""
        check = WebsiteCheck(
            website_id=website.id,
            checked_at=result.checked_at,
            is_up=result.is_up,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            error=result.error,
            final_url=result.final_url,
        )
        self.session.add(check)

        was_down = website.status is WebsiteStatus.DOWN
        website.last_checked_at = result.checked_at
        events: list[Notification] = []

        if result.is_up:
            downtime = self._downtime(website, result.checked_at)
            website.consecutive_failures = 0
            website.status = WebsiteStatus.UP
            if was_down:
                # Recovery: one alert, then silence until the next outage.
                events.append(
                    self._outage_event(
                        NotificationKind.RECOVERED,
                        website,
                        result,
                        downtime_seconds=downtime,
                        attempt=website.down_alerts_sent,
                    )
                )
                website.down_since = None
                website.down_alerts_sent = 0
        else:
            website.consecutive_failures += 1
            if not was_down:
                # First failure of a new outage — alert immediately.
                website.status = WebsiteStatus.DOWN
                website.down_since = result.checked_at
                website.down_alerts_sent = 1
                events.append(
                    self._outage_event(NotificationKind.DOWN, website, result, attempt=1)
                )
            elif website.down_alerts_sent < website.max_down_alerts:
                # Still down and we have alert budget left.
                website.down_alerts_sent += 1
                events.append(
                    self._outage_event(
                        NotificationKind.STILL_DOWN,
                        website,
                        result,
                        downtime_seconds=self._downtime(website, result.checked_at),
                        attempt=website.down_alerts_sent,
                    )
                )
            else:
                # Budget spent: keep checking so we can still detect recovery,
                # but stop mailing people about an outage they know about.
                logger.info(
                    "%s still down; alert budget (%d) spent, staying quiet.",
                    website.url,
                    website.max_down_alerts,
                )

        self._track_slowness(website, result, events)
        self._track_certificate(website, result, events)

        # Commit the new state before sending, so a slow or failing mail server
        # can never cause the same alert to be re-sent on the next tick.
        await self.session.commit()
        await self.session.refresh(website)
        await self.session.refresh(check)

        delivered: list[str] = []
        for event in events:
            for channel in await self.dispatch(event):
                if channel not in delivered:
                    delivered.append(channel)

        return CheckOutcome(
            website=website,
            check=check,
            result=result,
            alert=events[0].kind if events else None,
            delivered_by=tuple(delivered),
        )

    def _outage_event(
        self,
        kind: NotificationKind,
        website: Website,
        result: CheckResult,
        *,
        downtime_seconds: float = 0.0,
        attempt: int,
    ) -> OutageEvent:
        return OutageEvent(
            kind=kind,
            website=WebsiteSnapshot.of(website),
            result=result,
            downtime_seconds=downtime_seconds,
            attempt=attempt,
            max_attempts=website.max_down_alerts,
            recipients=self.recipients_for(website),
            slack=self.slack_target_for(website),
        )

    def _track_slowness(
        self, website: Website, result: CheckResult, events: list[Notification]
    ) -> None:
        """Count consecutive slow-but-successful checks; alert once per episode.

        A failed check resets the streak — that is an outage, and has its own
        alerts. The cooldown keeps a site that hovers around its threshold from
        alerting every time the streak rebuilds.
        """
        threshold = website.slow_threshold_ms or settings.SLOW_RESPONSE_THRESHOLD_MS
        slow = (
            result.is_up
            and threshold > 0
            and result.response_time_ms is not None
            and result.response_time_ms > threshold
        )
        if not slow:
            website.slow_streak = 0
            return

        website.slow_streak += 1
        if website.slow_streak < settings.SLOW_RESPONSE_CHECKS:
            return
        last = website.last_slow_alert_at
        if last is not None and (
            result.checked_at - last
        ).total_seconds() < settings.SLOW_ALERT_COOLDOWN_SECONDS:
            return

        website.last_slow_alert_at = result.checked_at
        events.append(
            SlowResponseEvent(
                website=WebsiteSnapshot.of(website),
                result=result,
                threshold_ms=threshold,
                slow_checks=website.slow_streak,
                recipients=self.recipients_for(website),
                slack=self.slack_target_for(website),
            )
        )

    def _track_certificate(
        self, website: Website, result: CheckResult, events: list[Notification]
    ) -> None:
        """Record a certificate read, and warn when it crosses a threshold.

        Each threshold warns once per certificate. `ssl_alert_bucket` remembers
        the smallest one already warned about, so 14 -> 7 -> 3 -> 1 -> expired
        each fire in turn and nothing repeats.
        """
        cert = result.cert
        if cert is None:
            return
        # Stamped even when the read failed, so it retries on the interval.
        website.ssl_checked_at = result.checked_at
        if cert.expires_at is None:
            return

        previous = website.ssl_expires_at
        website.ssl_expires_at = cert.expires_at
        if previous is not None and cert.expires_at > previous + _RENEWAL_JUMP:
            # A renewed certificate starts its warnings afresh, even when it
            # is itself already inside the top threshold (short-lived certs).
            website.ssl_alert_bucket = None

        bucket = ssl_alert_bucket(cert.expires_at, result.checked_at)
        if bucket is None:
            website.ssl_alert_bucket = None
        elif website.ssl_alert_bucket is None or bucket < website.ssl_alert_bucket:
            website.ssl_alert_bucket = bucket
            events.append(
                SslExpiryEvent(
                    website=WebsiteSnapshot.of(website),
                    result=result,
                    expires_at=cert.expires_at,
                    issuer=cert.issuer,
                    bucket=bucket,
                    recipients=self.recipients_for(website),
                    slack=self.slack_target_for(website),
                )
            )

    @staticmethod
    def _downtime(website: Website, now: datetime) -> float:
        if website.down_since is None:
            return 0.0
        started = website.down_since
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return max((now - started).total_seconds(), 0.0)

    # -- entry points ------------------------------------------------------

    async def check_one(
        self, website: Website, client: httpx.AsyncClient | None = None
    ) -> CheckOutcome:
        result = await check_website(website, client=client)
        return await self.record_result(website, result)

    async def run_due_checks(self) -> list[CheckOutcome]:
        """Probe every website whose interval has elapsed. Called by the scheduler."""
        due = await self.websites.due_for_check()
        if not due:
            return []

        logger.info("Checking %d due website(s).", len(due))
        outcomes: list[CheckOutcome] = []
        async with httpx.AsyncClient(follow_redirects=True) as client:
            # Probe concurrently, then fold results in one at a time — the
            # session is not safe for concurrent use.
            results = await asyncio.gather(
                *(check_website(site, client=client) for site in due),
                return_exceptions=True,
            )
            for website, result in zip(due, results, strict=True):
                if isinstance(result, BaseException):
                    logger.exception(
                        "Unexpected error checking %s", website.url, exc_info=result
                    )
                    continue
                outcomes.append(await self.record_result(website, result))
        return outcomes
