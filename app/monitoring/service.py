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
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.monitoring.alerts.base import (
    Alerter,
    AlertEvent,
    AlertKind,
    SlackTarget,
    WebsiteSnapshot,
)
from app.monitoring.alerts.email import EmailAlerter
from app.monitoring.alerts.slack import SlackAlerter
from app.monitoring.alerts.webhook import WebhookAlerter
from app.monitoring.websites.checker import CheckResult, check_website
from app.monitoring.websites.models import Website, WebsiteCheck, WebsiteStatus
from app.monitoring.websites.service import WebsiteService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CheckOutcome:
    """What one probe did, for callers and tests to inspect."""

    website: Website
    check: WebsiteCheck
    result: CheckResult
    alert: AlertKind | None = None
    delivered_by: tuple[str, ...] = ()


def default_alerters() -> list[Alerter]:
    """Every channel; each one no-ops when it is not configured."""
    return [EmailAlerter(), SlackAlerter(), WebhookAlerter()]


class MonitoringService:
    """Runs checks and turns their results into alerts."""

    def __init__(
        self,
        session: AsyncSession,
        alerters: list[Alerter] | None = None,
    ) -> None:
        self.session = session
        self.websites = WebsiteService(session)
        self.alerters = default_alerters() if alerters is None else alerters

    # -- recipients --------------------------------------------------------

    def recipients_for(self, website: Website) -> tuple[str, ...]:
        """Who hears about this site, de-duplicated.

        In order: the project's members and extra_emails (unless the site has
        `inherit_project_recipients` off), the site's own recipient users and
        alert_emails, and the global ALERT_DEFAULT_EMAILS. Suspended users are
        excluded wherever they appear.
        """
        seen: dict[str, None] = {}
        for email in [*website.alert_recipients, *settings.ALERT_DEFAULT_EMAILS]:
            cleaned = email.strip()
            if cleaned:
                seen.setdefault(cleaned.lower(), None)
        return tuple(seen)

    def slack_target_for(self, website: Website) -> SlackTarget | None:
        """The channel this site's alerts go to, with the token decrypted.

        The bot token always comes from the project; the channel is the site's
        own when it has one, otherwise the project's. Returns None when the
        project has no Slack set up, or the stored token cannot be decrypted.
        """
        project = website.project
        if project is None or not project.slack_enabled:
            return None

        channel = website.slack_channel_id or project.slack_channel_id
        if not channel:
            return None

        token = decrypt_secret(project.slack_bot_token)
        if not token:
            return None
        return SlackTarget(bot_token=token, channel_id=channel)

    # -- alert dispatch ----------------------------------------------------

    async def dispatch(self, event: AlertEvent) -> tuple[str, ...]:
        """Send `event` on every configured channel. Never raises."""
        if not settings.ALERTS_ENABLED:
            logger.info("ALERTS_ENABLED is false; suppressing %s", event.subject)
            return ()

        delivered: list[str] = []
        for alerter in self.alerters:
            try:
                if not await alerter.is_configured(event):
                    continue
                if await alerter.send(event):
                    delivered.append(alerter.name)
            except Exception:
                # A channel blowing up must not stop the others, or the loop.
                logger.exception("Alerter %r failed for %s", alerter.name, event.subject)
        if not delivered:
            logger.warning("No channel delivered: %s", event.subject)
        return tuple(delivered)

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
        event: AlertEvent | None = None

        if result.is_up:
            downtime = self._downtime(website, result.checked_at)
            website.consecutive_failures = 0
            website.status = WebsiteStatus.UP
            if was_down:
                # Recovery: one alert, then silence until the next outage.
                event = AlertEvent(
                    kind=AlertKind.RECOVERED,
                    website=WebsiteSnapshot.of(website),
                    result=result,
                    downtime_seconds=downtime,
                    attempt=website.down_alerts_sent,
                    max_attempts=website.max_down_alerts,
                    recipients=self.recipients_for(website),
                    slack=self.slack_target_for(website),
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
                event = AlertEvent(
                    kind=AlertKind.DOWN,
                    website=WebsiteSnapshot.of(website),
                    result=result,
                    downtime_seconds=0.0,
                    attempt=1,
                    max_attempts=website.max_down_alerts,
                    recipients=self.recipients_for(website),
                    slack=self.slack_target_for(website),
                )
            elif website.down_alerts_sent < website.max_down_alerts:
                # Still down and we have alert budget left.
                website.down_alerts_sent += 1
                event = AlertEvent(
                    kind=AlertKind.STILL_DOWN,
                    website=WebsiteSnapshot.of(website),
                    result=result,
                    downtime_seconds=self._downtime(website, result.checked_at),
                    attempt=website.down_alerts_sent,
                    max_attempts=website.max_down_alerts,
                    recipients=self.recipients_for(website),
                    slack=self.slack_target_for(website),
                )
            else:
                # Budget spent: keep checking so we can still detect recovery,
                # but stop mailing people about an outage they know about.
                logger.info(
                    "%s still down; alert budget (%d) spent, staying quiet.",
                    website.url,
                    website.max_down_alerts,
                )

        # Commit the new state before sending, so a slow or failing mail server
        # can never cause the same alert to be re-sent on the next tick.
        await self.session.commit()
        await self.session.refresh(website)
        await self.session.refresh(check)

        delivered: tuple[str, ...] = ()
        if event is not None:
            delivered = await self.dispatch(event)

        return CheckOutcome(
            website=website,
            check=check,
            result=result,
            alert=event.kind if event else None,
            delivered_by=delivered,
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
