"""Made-up events of every kind, for previewing a template before saving it.

Also what keeps the catalog honest: each sample's `context()` must offer exactly
the placeholders the catalog documents, and the preview endpoint refuses to
run if it does not.
"""

from datetime import UTC, datetime, timedelta

from app.monitoring.alerts.base import Notification, NotificationKind, WebsiteSnapshot
from app.monitoring.alerts.events import (
    OutageEvent,
    ReportEvent,
    SiteStats,
    SlowResponseEvent,
    SslExpiryEvent,
)
from app.monitoring.websites.checker import CheckResult

_PROJECT = "Acme Corp"


def _site(now: datetime) -> WebsiteSnapshot:
    return WebsiteSnapshot(
        id=1,
        project_id=1,
        name="Marketing site",
        url="https://www.acme.example/",
        project_name=_PROJECT,
        expected_status=200,
        timeout_seconds=10,
        check_interval_seconds=300,
        down_since=now - timedelta(minutes=12),
        consecutive_failures=3,
    )


def sample_event(kind: NotificationKind) -> Notification:
    now = datetime.now(UTC)
    site = _site(now)
    down = CheckResult(
        is_up=False,
        checked_at=now,
        status_code=503,
        reason="Service Unavailable",
        response_time_ms=1840,
        error="Expected HTTP 200, got 503.",
        error_type="unexpected_status",
        final_url=site.url,
        content_length=1520,
        headers={"server": "nginx", "retry-after": "120"},
    )
    up = CheckResult(
        is_up=True,
        checked_at=now,
        status_code=200,
        reason="OK",
        response_time_ms=212,
        final_url=site.url,
        content_length=48213,
        headers={"server": "nginx"},
    )
    recipients = ("you@example.com",)

    match kind:
        case NotificationKind.DOWN:
            return OutageEvent(
                kind=kind,
                website=site,
                result=down,
                attempt=1,
                max_attempts=4,
                recipients=recipients,
            )
        case NotificationKind.STILL_DOWN:
            return OutageEvent(
                kind=kind,
                website=site,
                result=down,
                downtime_seconds=735,
                attempt=2,
                max_attempts=4,
                recipients=recipients,
            )
        case NotificationKind.RECOVERED:
            return OutageEvent(
                kind=kind,
                website=site,
                result=up,
                downtime_seconds=1260,
                attempt=3,
                max_attempts=4,
                recipients=recipients,
            )
        case NotificationKind.SSL_EXPIRING:
            return SslExpiryEvent(
                website=site,
                result=up,
                expires_at=now + timedelta(days=6, hours=11),
                issuer="Let's Encrypt",
                bucket=7,
                recipients=recipients,
            )
        case NotificationKind.SLOW_RESPONSE:
            slow = CheckResult(
                is_up=True,
                checked_at=now,
                status_code=200,
                reason="OK",
                response_time_ms=4210,
                final_url=site.url,
            )
            return SlowResponseEvent(
                website=site,
                result=slow,
                threshold_ms=3000,
                slow_checks=3,
                recipients=recipients,
            )
        case NotificationKind.MONTHLY_REPORT:
            first = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)).replace(day=1)
            end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return ReportEvent(
                project_id=1,
                project_name=_PROJECT,
                period_start=first,
                period_end=end,
                sites=[
                    SiteStats(1, "Marketing site", "https://www.acme.example/", 8640, 8628, 3600, 2, 2700, 212, 480),
                    SiteStats(2, "Customer portal", "https://portal.acme.example/", 8640, 8640, 0, 0, 0, 180, 390),
                    SiteStats(3, "Public API", "https://api.acme.example/health", 8640, 8583, 17100, 3, 9000, 95, 210),
                ],
                sites_without_data=1,
                recipients=recipients,
            )
    raise ValueError(f"No sample for {kind}")
