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

In Slack an outage is one thread: the down alert starts it, the follow-ups
reply under it, and the recovery replies too while also showing in the channel.
In Telegram it is a reply chain the same way: the follow-ups and the recovery
reply to the down alert.

Softer signals ride on the same checks, none of which is an outage:

    slow   UP and slower than the threshold for SLOW_RESPONSE_CHECKS checks in
           a row -> one alert, then quiet for SLOW_ALERT_COOLDOWN_SECONDS
    loss   a pinged host UP but losing at least its threshold of pings for
           PACKET_LOSS_CHECKS checks in a row -> one alert, then quiet for
           PACKET_LOSS_ALERT_COOLDOWN_SECONDS
    ssl    the certificate crosses a SSL_EXPIRY_ALERT_DAYS threshold -> one
           alert per threshold, and one if it expires; renewing re-arms them
    dns    a DNS check with nothing pinned: the resolvers agree on records
           other than the ones they last agreed on -> one alert per change

A pinged host is UP while any of its pings is answered; one that answers none
is DOWN, and goes through the same outage alerts as a website. A DNS check is
DOWN when most resolvers cannot resolve its record, or any returns something
other than its pinned values; see `dns_probe`.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.monitoring.alerts.base import (
    Alerter,
    Notification,
    NotificationKind,
    SlackTarget,
    TelegramTarget,
    WebsiteSnapshot,
)
from app.monitoring.alerts.events import (
    DnsChangeEvent,
    OutageEvent,
    PacketLossEvent,
    SiteEvent,
    SlowResponseEvent,
    SslExpiryEvent,
)
from app.monitoring.notifications.dispatcher import Notifier, default_alerters
from app.monitoring.notifications.recipients import (
    dedupe_emails,
    slack_target,
    telegram_target,
)
from app.monitoring.websites.checker import CheckResult, check_website, new_client
from app.monitoring.websites.models import (
    Website,
    WebsiteCheck,
    WebsiteEvent,
    WebsiteStatus,
)
from app.monitoring.websites.pinger import IcmpUnavailableError
from app.monitoring.websites.service import WebsiteService

logger = logging.getLogger(__name__)

__all__ = [
    "FEED_KINDS",
    "CheckOutcome",
    "MonitoringService",
    "default_alerters",
    "feed_entry",
    "ssl_alert_bucket",
]

#: A certificate whose end date moved out by more than this has been replaced.
_RENEWAL_JUMP = timedelta(hours=12)

#: What the app's alert feed records. A still-down reminder only repeats the
#: first alert, so the feed keeps that one.
FEED_KINDS = frozenset(
    {
        NotificationKind.DOWN,
        NotificationKind.RECOVERED,
        NotificationKind.SLOW_RESPONSE,
        NotificationKind.PACKET_LOSS,
        NotificationKind.DNS_CHANGED,
        NotificationKind.SSL_EXPIRING,
    }
)


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


def feed_entry(event: SiteEvent) -> WebsiteEvent:
    """The row `event` leaves in the app's alert feed."""
    entry = WebsiteEvent(
        website_id=event.website.id,
        kind=event.kind.value,
        occurred_at=event.result.checked_at,
        # A change is about the records before and after, not the latest check.
        summary=(
            event.change_summary
            if isinstance(event, DnsChangeEvent)
            else event.result.summary
        ),
        response_time_ms=event.result.response_time_ms,
    )
    if isinstance(event, OutageEvent) and event.is_recovery:
        entry.downtime_seconds = round(event.downtime_seconds)
    elif isinstance(event, SlowResponseEvent):
        entry.threshold_ms = event.threshold_ms
    elif isinstance(event, SslExpiryEvent):
        entry.ssl_expires_at = event.expires_at
    return entry


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

    def telegram_target_for(self, website: Website) -> TelegramTarget | None:
        """The chat this site's alerts go to, with the token decrypted."""
        return telegram_target(
            website.project, website.telegram_chat_id, website.telegram_bot_token
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
        ping = result.ping
        check = WebsiteCheck(
            website_id=website.id,
            checked_at=result.checked_at,
            is_up=result.is_up,
            status_code=result.status_code,
            reason=result.reason,
            response_time_ms=result.response_time_ms,
            error=result.error,
            error_type=result.error_type,
            final_url=result.final_url,
            headers=result.headers or None,
            dns_ms=result.timings.dns_ms,
            connect_ms=result.timings.connect_ms,
            tls_ms=result.timings.tls_ms,
            first_byte_ms=result.timings.first_byte_ms,
            packets_sent=ping.sent if ping else None,
            packets_received=ping.received if ping else None,
            rtt_min_ms=ping.min_ms if ping else None,
            rtt_avg_ms=ping.avg_ms if ping else None,
            rtt_max_ms=ping.max_ms if ping else None,
            jitter_ms=ping.jitter_ms if ping else None,
            ip_address=ping.address if ping else None,
            dns=result.dns.as_dict() if result.dns else None,
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
                self._forget_threads(website)
        else:
            website.consecutive_failures += 1
            if not was_down:
                # First failure of a new outage — alert immediately, as the
                # start of a new Slack thread and Telegram reply chain.
                website.status = WebsiteStatus.DOWN
                website.down_since = result.checked_at
                website.down_alerts_sent = 1
                self._forget_threads(website)
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
        self._track_packet_loss(website, result, events)
        self._track_dns_records(website, result, events)
        self._track_certificate(website, result, events)
        # The app's own feed, in the same commit as the state it describes.
        for event in events:
            if isinstance(event, SiteEvent) and event.kind in FEED_KINDS:
                self.session.add(feed_entry(event))

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
        await self._keep_threads(website, events)

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
            slack=self._outage_slack_target(website, kind),
            telegram=self._outage_telegram_target(website),
        )

    def _outage_slack_target(
        self, website: Website, kind: NotificationKind
    ) -> SlackTarget | None:
        """The site's Slack target, replying in the outage's thread when there
        is one in that channel. The recovery also shows in the channel."""
        target = self.slack_target_for(website)
        if (
            target is None
            or website.slack_thread_ts is None
            or website.slack_thread_channel != target.channel_id
        ):
            return target
        return replace(
            target,
            thread_ts=website.slack_thread_ts,
            broadcast=kind is NotificationKind.RECOVERED,
        )

    def _outage_telegram_target(self, website: Website) -> TelegramTarget | None:
        """The site's Telegram target, replying to the outage's first message
        when there is one in that chat."""
        target = self.telegram_target_for(website)
        if (
            target is None
            or website.telegram_thread_message_id is None
            or website.telegram_thread_chat != target.chat_id
        ):
            return target
        return replace(target, reply_to=website.telegram_thread_message_id)

    @staticmethod
    def _forget_threads(website: Website) -> None:
        """An outage ended or a new one began: its replies start afresh."""
        website.slack_thread_ts = None
        website.slack_thread_channel = None
        website.telegram_thread_message_id = None
        website.telegram_thread_chat = None

    async def _keep_threads(
        self, website: Website, events: list[Notification]
    ) -> None:
        """Remember an outage alert that went to Slack or Telegram as a new
        message, so the rest of the outage replies under it.

        Usually that is the down alert. If it never reached a channel, or the
        site's channel or chat changed mid-outage, the next alert that posts
        there starts the thread.
        """
        slack_kept = telegram_kept = False
        for event in events:
            if not isinstance(event, OutageEvent) or event.is_recovery:
                continue
            if (
                not slack_kept
                and event.slack is not None
                and event.slack.thread_ts is None
                and event.slack_ts
            ):
                website.slack_thread_ts = event.slack_ts
                website.slack_thread_channel = event.slack.channel_id
                slack_kept = True
            if (
                not telegram_kept
                and event.telegram is not None
                and event.telegram.reply_to is None
                and event.telegram_message_id
            ):
                website.telegram_thread_message_id = event.telegram_message_id
                website.telegram_thread_chat = event.telegram.chat_id
                telegram_kept = True
        if slack_kept or telegram_kept:
            await self.session.commit()

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
                telegram=self.telegram_target_for(website),
            )
        )

    def _track_packet_loss(
        self, website: Website, result: CheckResult, events: list[Notification]
    ) -> None:
        """Count consecutive pings that lost packets over the threshold while
        the host still answered; alert once per episode, like slowness.

        A host that answers nothing is down, not lossy, and resets the streak.
        """
        threshold = (
            website.packet_loss_threshold_percent or settings.PACKET_LOSS_THRESHOLD_PERCENT
        )
        lossy = (
            result.is_up
            and result.ping is not None
            and threshold > 0
            and result.ping.loss_percent >= threshold
        )
        if not lossy:
            website.loss_streak = 0
            return

        website.loss_streak += 1
        if website.loss_streak < settings.PACKET_LOSS_CHECKS:
            return
        last = website.last_loss_alert_at
        if last is not None and (
            result.checked_at - last
        ).total_seconds() < settings.PACKET_LOSS_ALERT_COOLDOWN_SECONDS:
            return

        website.last_loss_alert_at = result.checked_at
        events.append(
            PacketLossEvent(
                website=WebsiteSnapshot.of(website),
                result=result,
                threshold_percent=threshold,
                lossy_checks=website.loss_streak,
                recipients=self.recipients_for(website),
                slack=self.slack_target_for(website),
                telegram=self.telegram_target_for(website),
            )
        )

    def _track_dns_records(
        self, website: Website, result: CheckResult, events: list[Notification]
    ) -> None:
        """Remember the records the resolvers agree on, and alert when they
        agree on different ones.

        Waiting for them to agree keeps one change to one alert: while it
        propagates, resolvers still serving the old records from cache
        disagree with the rest, which is not yet a change. With values pinned,
        a different answer is an outage instead, so this stays quiet; the
        records are still remembered, for the page and for unpinning later.
        """
        if result.dns is None:
            return
        current = result.dns.records
        if current is None:
            return
        previous = website.dns_records
        website.dns_records = current
        # The first agreement is what later ones are measured against.
        if previous is None or previous == current or website.dns_expected_values:
            return
        events.append(
            DnsChangeEvent(
                website=WebsiteSnapshot.of(website),
                result=result,
                previous=previous,
                current=current,
                recipients=self.recipients_for(website),
                slack=self.slack_target_for(website),
                telegram=self.telegram_target_for(website),
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
                    telegram=self.telegram_target_for(website),
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
        """Raises `IcmpUnavailableError` for a ping check when this server
        cannot ping at all; nothing is recorded then."""
        result = await check_website(website, client=client)
        return await self.record_result(website, result)

    async def run_due_checks(self) -> list[CheckOutcome]:
        """Probe every website whose interval has elapsed. Called by the scheduler."""
        due = await self.websites.due_for_check()
        if not due:
            return []

        logger.info("Checking %d due website(s).", len(due))
        outcomes: list[CheckOutcome] = []
        async with new_client() as client:
            # Probe concurrently, then fold results in one at a time — the
            # session is not safe for concurrent use.
            results = await asyncio.gather(
                *(check_website(site, client=client) for site in due),
                return_exceptions=True,
            )
            for website, result in zip(due, results, strict=True):
                if isinstance(result, IcmpUnavailableError):
                    # A problem with this server, not the host: recording the
                    # host as down would alert its people about the wrong thing.
                    logger.error("Could not ping %s: %s", website.url, result)
                    continue
                if isinstance(result, BaseException):
                    logger.exception(
                        "Unexpected error checking %s", website.url, exc_info=result
                    )
                    continue
                if await self._changed_since_loaded(website):
                    # Edited, checked by hand or deleted while this probe ran:
                    # the result is of settings the site no longer has (a DNS
                    # check's old pins or record type, say), and recording it
                    # would alert about them. The next check uses the new ones.
                    logger.info("%s changed during its check; result dropped.", website.url)
                    continue
                outcomes.append(await self.record_result(website, result))
        return outcomes

    async def _changed_since_loaded(self, website: Website) -> bool:
        """Whether the row was updated or deleted since `website` was read.
        Every update stamps `updated_at`."""
        current = await self.session.scalar(
            select(Website.updated_at).where(Website.id == website.id)
        )
        return current != website.updated_at
