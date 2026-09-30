"""Made-up events of every kind, for previewing a template before saving it.

Also what keeps the catalog honest: each sample's `context()` must offer exactly
the placeholders the catalog documents, and the preview endpoint refuses to
run if it does not.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.monitoring.alerts.base import Notification, NotificationKind, WebsiteSnapshot
from app.monitoring.alerts.events import (
    DnsChangeEvent,
    DomainExpiryEvent,
    NameserverChangeEvent,
    OutageEvent,
    PacketLossEvent,
    ReportEvent,
    SiteStats,
    SlowResponseEvent,
    SslExpiryEvent,
)
from app.monitoring.websites.checker import CheckResult, Timings
from app.monitoring.websites.dns_probe import DnsResult, ResolverAnswer
from app.monitoring.websites.models import CheckType, DnsRecordType
from app.monitoring.websites.pinger import PingStats

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
        timings=Timings(dns_ms=21, connect_ms=38, tls_ms=88, first_byte_ms=1650),
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
        timings=Timings(dns_ms=9, connect_ms=31, tls_ms=64, first_byte_ms=96),
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
        case NotificationKind.DOMAIN_EXPIRING:
            return DomainExpiryEvent(
                website=site,
                result=up,
                domain="acme.example",
                expires_at=now + timedelta(days=13, hours=5),
                registrar="Example Registrar, Inc.",
                bucket=14,
                recipients=recipients,
            )
        case NotificationKind.NAMESERVERS_CHANGED:
            return NameserverChangeEvent(
                website=site,
                result=up,
                domain="acme.example",
                previous=["ns1.old-dns.example", "ns2.old-dns.example"],
                current=["ns1.new-dns.example", "ns2.new-dns.example"],
                registrar="Example Registrar, Inc.",
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
                timings=Timings(dns_ms=18, connect_ms=42, tls_ms=96, first_byte_ms=3920),
            )
            return SlowResponseEvent(
                website=site,
                result=slow,
                threshold_ms=3000,
                slow_checks=3,
                recipients=recipients,
            )
        case NotificationKind.PACKET_LOSS:
            host = replace(
                site,
                name="Core router",
                url="gw.acme.example",
                timeout_seconds=2,
                check_interval_seconds=60,
                check_type=CheckType.PING,
            )
            lossy = CheckResult(
                is_up=True,
                checked_at=now,
                response_time_ms=24,
                timings=Timings(dns_ms=4),
                ping=PingStats(
                    address="203.0.113.1",
                    sent=5,
                    received=3,
                    min_ms=18.204,
                    avg_ms=23.871,
                    max_ms=31.56,
                    jitter_ms=6.702,
                ),
            )
            return PacketLossEvent(
                website=host,
                result=lossy,
                threshold_percent=20,
                lossy_checks=3,
                recipients=recipients,
            )
        case NotificationKind.DNS_CHANGED:
            record = replace(
                site,
                name="Acme apex A record",
                url="acme.example",
                timeout_seconds=5,
                check_type=CheckType.DNS,
            )
            now_serving = ["198.51.100.7"]
            answers = [
                ResolverAnswer(
                    resolver=name,
                    address=address,
                    records=now_serving,
                    ttl=300,
                    time_ms=ms,
                )
                for name, address, ms in (
                    ("Cloudflare", "1.1.1.1", 9),
                    ("Google", "8.8.8.8", 31),
                    ("Quad9", "9.9.9.9", 14),
                    ("OpenDNS", "208.67.222.222", 42),
                )
            ]
            changed = CheckResult(
                is_up=True,
                checked_at=now,
                response_time_ms=24,
                dns=DnsResult(name=record.url, record_type=DnsRecordType.A, answers=answers),
            )
            return DnsChangeEvent(
                website=record,
                result=changed,
                previous=["203.0.113.10"],
                current=now_serving,
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
