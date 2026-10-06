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
from app.monitoring.infra.aws.events import (
    CapacityEvent,
    CheckLine,
    DeployedResource,
    DeployEvent,
    InfraDegradedEvent,
    InfraOutageEvent,
    ScaledInstance,
    ScalingEvent,
    ResourceSnapshot,
    VpcEvent,
)
from app.monitoring.infra.aws.models import ResourceKind
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
    return _infra_sample(kind, now, recipients)


def _infra_sample(kind: NotificationKind, now: datetime, recipients: tuple[str, ...]) -> Notification:
    server = ResourceSnapshot(
        id=15,
        project_id=1,
        project_name=_PROJECT,
        name="report-runner",
        kind=ResourceKind.SERVER,
        aws_id="i-0b2e5d6c7b8a9f0e1",
        address="10.20.21.55",
        vpc_name="prod-vpc",
        aws_state="running",
        down_since=now - timedelta(minutes=12),
    )
    load_balancer = replace(
        server,
        id=21,
        name="orders-api-alb",
        kind=ResourceKind.LOAD_BALANCER,
        aws_id="arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:loadbalancer/app/orders-api-alb/50dc6c495c0c9188",
        address="internal-orders-api-alb-1234567890.ap-southeast-1.elb.amazonaws.com",
        aws_state="active",
        down_since=None,
    )
    failing = (
        CheckLine("ping", "ping", "No reply to 3 pings within 2s.", "no_reply"),
        CheckLine(
            "tcp 22",
            "tcp",
            "TCP 22: No answer: the packets were dropped (security group, NACL or route).",
            "connect_timeout",
        ),
    )
    match kind:
        case NotificationKind.INFRA_DOWN:
            return InfraOutageEvent(
                kind=kind, resource=server, checked_at=now, failing=failing,
                attempt=1, max_attempts=4, recipients=recipients,
            )
        case NotificationKind.INFRA_STILL_DOWN:
            return InfraOutageEvent(
                kind=kind, resource=server, checked_at=now, failing=failing,
                downtime_seconds=735, attempt=2, max_attempts=4, recipients=recipients,
            )
        case NotificationKind.INFRA_RECOVERED:
            return InfraOutageEvent(
                kind=kind, resource=server, checked_at=now,
                downtime_seconds=1260, attempt=3, max_attempts=4, recipients=recipients,
            )
        case NotificationKind.INFRA_DEGRADED:
            return InfraDegradedEvent(
                resource=load_balancer,
                checked_at=now,
                problem="targets_unhealthy",
                problem_detail=(
                    "orders-api-tg: 2 of 3 targets healthy; i-0c4d5e6f7a8b9c0d1 failing with "
                    "Target.Timeout (Request timed out)"
                ),
                check=CheckLine("targets of orders-api-tg", "target_health", "orders-api-tg: 2 of 3 targets healthy"),
                recipients=recipients,
            )
        case NotificationKind.ASG_SCALED_OUT | NotificationKind.ASG_SCALED_IN:
            group = replace(
                server,
                id=30,
                name="orders-api-asg",
                kind=ResourceKind.AUTO_SCALING_GROUP,
                aws_id="orders-api-asg",
                address=None,
                aws_state="4 of 4 in service",
                down_since=None,
            )
            out = kind is NotificationKind.ASG_SCALED_OUT
            return ScalingEvent(
                kind=kind,
                resource=group,
                checked_at=now,
                instances=(
                    ScaledInstance(
                        "i-0a1b2c3d4e5f60718", "ap-southeast-1a", "10.20.21.71",
                        True if out else None, "200 in 41 ms" if out else None,
                    ),
                    ScaledInstance(
                        "i-0f9e8d7c6b5a43210", "ap-southeast-1b", "10.20.22.64",
                        False if out else None, "no answer: connection refused" if out else None,
                    ),
                ),
                in_service=4,
                desired=4,
                health_endpoint="HTTP port 80, GET /health" if out else None,
                recipients=recipients,
            )
        case NotificationKind.VPC_UNREACHABLE | NotificationKind.VPC_RECOVERED:
            return VpcEvent(
                kind=kind,
                vpc_id=1,
                vpc_name="prod-vpc",
                cidrs=("10.20.0.0/16",),
                project_id=1,
                project_name=_PROJECT,
                occurred_at=now,
                failed_checks=11,
                total_checks=14,
                downtime_seconds=720,
                failing=failing,
                recipients=recipients,
            )
        case NotificationKind.DEPLOY_STARTED | NotificationKind.DEPLOY_FINISHED:
            finished = kind is NotificationKind.DEPLOY_FINISHED
            return DeployEvent(
                kind=kind,
                project_id=1,
                project_name=_PROJECT,
                account_name="Production",
                aws_account_id="123456789012",
                region="ap-southeast-1",
                deployment_id="d-7Q2X9ABCD",
                application="orders-api",
                group="orders-api-prod",
                environment="production",
                started_at=now - timedelta(minutes=4) if finished else now,
                creator="user",
                description="Release 2.4.1",
                revision="github acme/orders-api@4f1c2d9",
                resources=(
                    DeployedResource(30, "orders-api-asg", ResourceKind.AUTO_SCALING_GROUP),
                    DeployedResource(21, "orders-api-alb", ResourceKind.LOAD_BALANCER),
                ),
                status="Succeeded" if finished else None,
                finished_at=now if finished else None,
                recipients=recipients,
            )
        case NotificationKind.ACCOUNT_CAPACITY:
            return CapacityEvent(
                project_id=1,
                project_name=_PROJECT,
                account_name="Production",
                aws_account_id="123456789012",
                region="ap-southeast-1",
                problem="eip_unattached",
                problem_detail=(
                    "2 Elastic IPs attached to nothing: 203.0.113.10 (old-bastion), 203.0.113.11; "
                    "about US$7 a month"
                ),
                occurred_at=now,
                addresses=("203.0.113.10 (old-bastion, eipalloc-0a1b2c3d)", "203.0.113.11 (eipalloc-0e4f5a6b)"),
                recipients=recipients,
            )
    raise ValueError(f"No sample for {kind}")
