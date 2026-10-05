"""Infrastructure checks, turned into outages and alerts.

The policy is the websites' (see `app/monitoring/service.py`), with the
resource, not the check, as what goes down:

    UP   -> DOWN   the first of its checks fails      -> infra_down, listing
                                                         every failing check
    DOWN -> DOWN   a check fails again               -> infra_still_down while
                                                         alerts are left
    DOWN -> UP     every check passes again          -> infra_recovered
    UP             a problem lasts INFRA_PROBLEM_CHECKS -> infra_degraded, then
                   checks (unhealthy targets, slowness,   quiet for the cooldown
                   packet loss)

When most checks into a VPC fail to *connect* at once, the VPC is marked
unreachable instead: one vpc_unreachable alert per project, and each
resource's own down alert is held. When the VPC answers again, vpc_recovered
goes out, and the resources still down send their held alerts then. Checks
that go over the internet (an internet-facing load balancer, a server's public
IP) say nothing about reaching into the VPC, and are left out of that count.

In Slack an outage is one thread and in Telegram one reply chain, as for a
website. A maintenance window silences all of it.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.monitoring import egress
from app.monitoring.alerts.base import Alerter, Notification, NotificationKind, SlackTarget, TelegramTarget
from app.monitoring.infra.aws import client, probes
from app.monitoring.infra.aws.events import (
    CheckLine,
    InfraDegradedEvent,
    InfraOutageEvent,
    ResourceSnapshot,
    ScaledInstance,
    ScalingEvent,
    VpcEvent,
)
from app.monitoring.infra.aws.models import (
    NETWORK_CHECKS,
    AwsCheck,
    AwsCheckResult,
    AwsEvent,
    AwsResource,
    AwsVpc,
    CheckHealth,
    InfraCheckType,
    ResourceKind,
    ResourceStatus,
    is_public_path,
)
from app.monitoring.infra.aws.probes.base import NOT_ABOUT_REACH, ProbeResult, ProbeTarget
from app.monitoring.infra.aws.service import InfraService, ScalingChange
from app.monitoring.notifications.dispatcher import Notifier
from app.monitoring.notifications.recipients import (
    dedupe_emails,
    project_recipients,
    slack_target,
    telegram_target,
    whatsapp_target,
)
from app.monitoring.projects.models import Project
from app.monitoring.websites.pinger import IcmpUnavailableError

logger = logging.getLogger(__name__)

#: Probes run at once, at most.
MAX_CONCURRENT_PROBES = 25
#: What the feed records; a still-down reminder only repeats the first alert.
FEED_KINDS = frozenset(
    {
        NotificationKind.INFRA_DOWN,
        NotificationKind.INFRA_RECOVERED,
        NotificationKind.INFRA_DEGRADED,
        NotificationKind.VPC_UNREACHABLE,
        NotificationKind.VPC_RECOVERED,
        NotificationKind.ASG_SCALED_OUT,
        NotificationKind.ASG_SCALED_IN,
    }
)


@dataclass(slots=True)
class ResourceOutcome:
    resource: AwsResource
    results: list[AwsCheckResult]
    #: The first notification the run raised, if any.
    alert: NotificationKind | None = None
    delivered_by: tuple[str, ...] = ()


def through_vpc(check: AwsCheck, resource: AwsResource) -> bool:
    """Whether this check connects into the VPC, and so tells whether
    Watchly can reach it."""
    return check.check_type in NETWORK_CHECKS and not is_public_path(
        resource.kind, resource.aws_detail, check.settings
    )


def build_target(check: AwsCheck, resource: AwsResource, vpc: AwsVpc) -> ProbeTarget:
    """What the probe needs: where to connect, and where it may. A public path
    may also reach public addresses; every other, the VPC's ranges only."""
    detail = resource.aws_detail or {}
    address = resource.address
    if resource.kind is ResourceKind.SERVER and check.settings.get("use_public_ip"):
        address = detail.get("public_ip")
    return ProbeTarget(
        check_type=check.check_type,
        settings=check.settings,
        timeout=float(check.timeout_seconds),
        kind=resource.kind,
        resource_name=resource.name,
        aws_id=resource.aws_id,
        address=address,
        aws_detail=detail,
        region=client.Region.of(vpc),
        scope=egress.vpc_scope(
            vpc.name, vpc.cidrs, allow_public=is_public_path(resource.kind, detail, check.settings)
        ),
    )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class InfraMonitor:
    """Runs infrastructure checks and turns their results into alerts."""

    def __init__(self, session: AsyncSession, alerters: list[Alerter] | None = None) -> None:
        self.session = session
        self.service = InfraService(session)
        self.notifier = Notifier(session, alerters)

    # -- recipients ----------------------------------------------------------

    @staticmethod
    def recipients_for(resource: AwsResource) -> tuple[str, ...]:
        return dedupe_emails(resource.alert_recipients, settings.ALERT_DEFAULT_EMAILS)

    @staticmethod
    def _slack(resource: AwsResource, kind: NotificationKind | None = None) -> SlackTarget | None:
        """The project's channel, replying in the outage's thread when there
        is one there; the recovery also shows in the channel."""
        target = slack_target(resource.project)
        if (
            kind is None
            or target is None
            or resource.slack_thread_ts is None
            or resource.slack_thread_channel != target.channel_id
        ):
            return target
        return replace(
            target,
            thread_ts=resource.slack_thread_ts,
            broadcast=kind is NotificationKind.INFRA_RECOVERED,
        )

    @staticmethod
    def _telegram(resource: AwsResource, threaded: bool = False) -> TelegramTarget | None:
        target = telegram_target(resource.project)
        if (
            not threaded
            or target is None
            or resource.telegram_thread_message_id is None
            or resource.telegram_thread_chat != target.chat_id
        ):
            return target
        return replace(target, reply_to=resource.telegram_thread_message_id)

    @staticmethod
    def _forget_threads(resource: AwsResource) -> None:
        resource.slack_thread_ts = None
        resource.slack_thread_channel = None
        resource.telegram_thread_message_id = None
        resource.telegram_thread_chat = None

    # -- events --------------------------------------------------------------

    def _failing_lines(self, resource: AwsResource) -> tuple[CheckLine, ...]:
        return tuple(
            CheckLine(
                name=check.name,
                check_type=check.check_type.value,
                summary=(check.last_result or {}).get("summary") or "failed",
                error_type=(check.last_result or {}).get("error_type"),
            )
            for check in resource.checks
            if check.is_enabled and check.health is CheckHealth.DOWN
        )

    def _outage(
        self,
        kind: NotificationKind,
        resource: AwsResource,
        checked_at: datetime,
        *,
        attempt: int,
        downtime_seconds: float = 0.0,
    ) -> InfraOutageEvent:
        return InfraOutageEvent(
            kind=kind,
            resource=ResourceSnapshot.of(resource),
            checked_at=checked_at,
            failing=self._failing_lines(resource),
            downtime_seconds=downtime_seconds,
            attempt=attempt,
            max_attempts=resource.max_down_alerts,
            recipients=self.recipients_for(resource),
            slack=self._slack(resource, kind),
            telegram=self._telegram(resource, threaded=True),
            whatsapp=whatsapp_target(resource.project),
        )

    @staticmethod
    def _feed(event: Notification, vpc_id: int, summary: str) -> AwsEvent:
        resource = getattr(event, "resource", None)
        return AwsEvent(
            project_id=event.project_id,
            vpc_id=vpc_id,
            resource_id=resource.id if resource is not None else None,
            kind=event.kind.value,
            occurred_at=getattr(event, "checked_at", None) or getattr(event, "occurred_at"),
            summary=summary[:2000],
            downtime_seconds=(
                round(event.downtime_seconds)
                if getattr(event, "downtime_seconds", 0) and event.kind
                in {NotificationKind.INFRA_RECOVERED, NotificationKind.VPC_RECOVERED}
                else None
            ),
        )

    # -- the state machine ---------------------------------------------------

    def _track_problems(
        self,
        resource: AwsResource,
        check: AwsCheck,
        result: ProbeResult,
        events: list[Notification],
    ) -> None:
        """Count each problem's streak; open it, and alert, once it lasts
        INFRA_PROBLEM_CHECKS checks. A failed check is an outage, not a
        problem, and resets every streak."""
        problems = {kind: dict(entry) for kind, entry in (check.problems or {}).items()}
        current = result.problems if result.ok else {}
        for kind, detail in current.items():
            entry = problems.get(kind, {})
            entry["streak"] = int(entry.get("streak", 0)) + 1
            entry["detail"] = detail
            if entry["streak"] >= settings.INFRA_PROBLEM_CHECKS and not entry.get("since"):
                entry["since"] = result.checked_at.isoformat()
                last = _parse_time(entry.get("last_alert_at"))
                cooldown = settings.INFRA_PROBLEM_ALERT_COOLDOWN_SECONDS
                if last is None or (result.checked_at - last).total_seconds() >= cooldown:
                    entry["last_alert_at"] = result.checked_at.isoformat()
                    events.append(
                        InfraDegradedEvent(
                            resource=ResourceSnapshot.of(resource),
                            checked_at=result.checked_at,
                            problem=kind,
                            problem_detail=detail,
                            check=CheckLine(check.name, check.check_type.value, result.summary),
                            cooldown_seconds=cooldown,
                            recipients=self.recipients_for(resource),
                            slack=self._slack(resource),
                            telegram=self._telegram(resource),
                            whatsapp=whatsapp_target(resource.project),
                        )
                    )
            problems[kind] = entry
        for kind, entry in problems.items():
            if kind not in current:
                entry.update(streak=0, since=None, detail=None)
        # A new dict, so the JSONB column is seen to change.
        check.problems = problems
        if not result.ok:
            check.health = CheckHealth.DOWN
        elif any(entry.get("since") for entry in problems.values()):
            check.health = CheckHealth.DEGRADED
        else:
            check.health = CheckHealth.HEALTHY

    async def record(
        self,
        resource: AwsResource,
        pairs: list[tuple[AwsCheck, ProbeResult]],
        *,
        hold: bool = False,
    ) -> ResourceOutcome:
        """Persist the results, advance the resource's state, and alert. With
        `hold`, its VPC is unreachable: a new outage is recorded but its down
        alert waits for the VPC."""
        rows = []
        for check, result in pairs:
            row = AwsCheckResult(
                check_id=check.id,
                resource_id=resource.id,
                checked_at=result.checked_at,
                ok=result.ok,
                response_time_ms=result.response_time_ms,
                dns_ms=result.dns_ms,
                connect_ms=result.connect_ms,
                tls_ms=result.tls_ms,
                summary=result.summary[:2000],
                error=result.error,
                error_type=result.error_type,
                address=(result.address or "")[:64] or None,
                metric=result.metric,
                detail=result.detail,
            )
            self.session.add(row)
            rows.append(row)

        if await self.service.in_maintenance(resource.id):
            # As for websites: kept for the history, moves nothing along.
            await self.session.commit()
            for row in rows:
                await self.session.refresh(row)
            return ResourceOutcome(resource=resource, results=rows)

        events: list[Notification] = []
        now = max(result.checked_at for _, result in pairs)
        for check, result in pairs:
            check.last_checked_at = result.checked_at
            check.last_result = result.as_last_result()
            if result.snapshot is not None:
                check.snapshot = result.snapshot
            check.consecutive_failures = 0 if result.ok else check.consecutive_failures + 1
            self._track_problems(resource, check, result, events)
        resource.last_checked_at = now

        enabled = [c for c in resource.checks if c.is_enabled]
        any_down = any(c.health is CheckHealth.DOWN for c in enabled)
        any_known = any(c.health is not CheckHealth.UNKNOWN for c in enabled)
        failed_now = any(not result.ok for _, result in pairs)
        was_down = resource.status is ResourceStatus.DOWN

        if any_down:
            # Problems of a resource that is down are not news.
            events[:] = [e for e in events if not isinstance(e, InfraDegradedEvent)]
            resource.degraded_since = None
            if not was_down:
                resource.status = ResourceStatus.DOWN
                resource.down_since = now
                self._forget_threads(resource)
                if hold:
                    resource.down_alerts_sent = 0
                    resource.down_alert_held = True
                else:
                    resource.down_alerts_sent = 1
                    resource.down_alert_held = False
                    events.append(self._outage(NotificationKind.INFRA_DOWN, resource, now, attempt=1))
            elif resource.down_alert_held:
                if not hold:
                    resource.down_alerts_sent = 1
                    resource.down_alert_held = False
                    events.append(self._outage(NotificationKind.INFRA_DOWN, resource, now, attempt=1))
            elif failed_now and not hold and resource.down_alerts_sent < resource.max_down_alerts:
                resource.down_alerts_sent += 1
                events.append(
                    self._outage(
                        NotificationKind.INFRA_STILL_DOWN,
                        resource,
                        now,
                        attempt=resource.down_alerts_sent,
                        downtime_seconds=self._downtime(resource, now),
                    )
                )
        elif any_known:
            if was_down:
                if resource.down_alerts_sent > 0:
                    events.append(
                        self._outage(
                            NotificationKind.INFRA_RECOVERED,
                            resource,
                            now,
                            attempt=resource.down_alerts_sent,
                            downtime_seconds=self._downtime(resource, now),
                        )
                    )
                resource.down_since = None
                resource.down_alerts_sent = 0
                resource.down_alert_held = False
            resource.status = ResourceStatus.UP
            opened = [
                _parse_time(entry.get("since"))
                for c in enabled
                for entry in (c.problems or {}).values()
                if entry.get("since")
            ]
            opened = [moment for moment in opened if moment is not None]
            resource.degraded_since = min(opened) if opened else None

        for event in events:
            if event.kind in FEED_KINDS:
                summary = (
                    event.problem_detail
                    if isinstance(event, InfraDegradedEvent)
                    else "; ".join(line.text() for line in event.failing)
                    if isinstance(event, InfraOutageEvent) and not event.is_recovery
                    else f"Back up after {event.downtime}"
                    if isinstance(event, InfraOutageEvent)
                    else event.describe()
                )
                self.session.add(self._feed(event, resource.vpc_id, summary))

        # Commit before sending, so a slow channel can never make the same
        # alert go out twice.
        await self.session.commit()
        await self.service.reload(resource)
        for row in rows:
            await self.session.refresh(row)

        delivered: list[str] = []
        for event in events:
            for channel in await self.notifier.dispatch(event):
                if channel not in delivered:
                    delivered.append(channel)
        await self._keep_threads(resource, events)
        if any(e.kind is NotificationKind.INFRA_RECOVERED for e in events):
            self._forget_threads(resource)
            await self.session.commit()
        return ResourceOutcome(
            resource=resource,
            results=rows,
            alert=events[0].kind if events else None,
            delivered_by=tuple(delivered),
        )

    async def _keep_threads(self, resource: AwsResource, events: list[Notification]) -> None:
        """Remember the outage's first Slack and Telegram messages, so the rest
        of the outage replies under them."""
        changed = False
        for event in events:
            if not isinstance(event, InfraOutageEvent) or event.is_recovery:
                continue
            if event.slack is not None and event.slack.thread_ts is None and event.slack_ts and not resource.slack_thread_ts:
                resource.slack_thread_ts = event.slack_ts
                resource.slack_thread_channel = event.slack.channel_id
                changed = True
            if (
                event.telegram is not None
                and event.telegram.reply_to is None
                and event.telegram_message_id
                and not resource.telegram_thread_message_id
            ):
                resource.telegram_thread_message_id = event.telegram_message_id
                resource.telegram_thread_chat = event.telegram.chat_id
                changed = True
        if changed:
            await self.session.commit()

    @staticmethod
    def _downtime(resource: AwsResource, now: datetime) -> float:
        if resource.down_since is None:
            return 0.0
        started = resource.down_since
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return max((now - started).total_seconds(), 0.0)

    # -- whole VPCs ----------------------------------------------------------

    async def _vpc_projects(self, vpc_id: int) -> list[Project]:
        rows = await self.session.scalars(
            select(Project)
            .where(
                Project.id.in_(
                    select(AwsResource.project_id).where(
                        AwsResource.vpc_id == vpc_id, AwsResource.is_enabled.is_(True)
                    )
                )
            )
            .order_by(Project.id)
        )
        return list(rows)

    async def _vpc_events(self, vpc: AwsVpc, kind: NotificationKind, **fields) -> list[VpcEvent]:
        events = []
        for project in await self._vpc_projects(vpc.id):
            event = VpcEvent(
                kind=kind,
                vpc_id=vpc.id,
                vpc_name=vpc.name,
                cidrs=tuple(vpc.cidrs),
                project_id=project.id,
                project_name=project.name,
                recipients=project_recipients(project),
                slack=slack_target(project),
                telegram=telegram_target(project),
                whatsapp=whatsapp_target(project),
                **fields,
            )
            events.append(event)
            summary = (
                f"{fields.get('failed_checks')} of {fields.get('total_checks')} checks failed to connect"
                if kind is NotificationKind.VPC_UNREACHABLE
                else "The VPC answers again"
            )
            self.session.add(self._feed(event, vpc.id, summary))
        return events

    async def _evaluate_vpcs(
        self, by_resource: dict[int, tuple[AwsResource, list[tuple[AwsCheck, ProbeResult]]]]
    ) -> tuple[list[VpcEvent], set[int]]:
        """Mark VPCs unreachable, or reachable again, from this tick's results
        and the latest known result of every other network check in them.
        Returns the events to send and the VPCs that came back."""
        now = datetime.now(UTC)
        tick: dict[int, list[ProbeResult]] = {}
        ran: set[int] = set()
        for resource, pairs in by_resource.values():
            for check, result in pairs:
                if through_vpc(check, resource):
                    ran.add(check.id)
                    if result.tells_reach:
                        tick.setdefault(resource.vpc_id, []).append(result)

        events: list[VpcEvent] = []
        recovered: set[int] = set()
        for vpc_id, results in tick.items():
            vpc = await self.session.get(AwsVpc, vpc_id)
            if vpc is None:
                continue
            tick_failures = sum(1 for r in results if r.connect_failure)
            if vpc.unreachable_since is None:
                if not tick_failures:
                    continue
                known = [r.connect_failure for r in results]
                others = await self.session.execute(
                    select(AwsCheck, AwsResource.kind, AwsResource.aws_detail)
                    .join(AwsResource, AwsResource.id == AwsCheck.resource_id)
                    .where(
                        AwsResource.vpc_id == vpc_id,
                        AwsResource.is_enabled.is_(True),
                        AwsResource.missing_since.is_(None),
                        AwsCheck.is_enabled.is_(True),
                        AwsCheck.check_type.in_(list(NETWORK_CHECKS)),
                        AwsCheck.last_checked_at.is_not(None),
                    )
                )
                for check, kind, detail in others:
                    if check.id in ran or not check.last_result or is_public_path(kind, detail, check.settings):
                        continue
                    last = check.last_result
                    if not last.get("ok") and last.get("error_type") in NOT_ABOUT_REACH:
                        continue
                    age = (now - check.last_checked_at).total_seconds()
                    if age <= 2 * check.check_interval_seconds + settings.MONITOR_TICK_SECONDS:
                        known.append(bool(check.last_result.get("connect_failure")))
                failures = sum(known)
                if (
                    len(known) >= settings.VPC_UNREACHABLE_MIN_CHECKS
                    and failures * 100 >= settings.VPC_UNREACHABLE_PERCENT * len(known)
                ):
                    vpc.unreachable_since = now
                    logger.warning(
                        "VPC %s unreachable: %d of %d checks failed to connect.",
                        vpc.name, failures, len(known),
                    )
                    failing = tuple(
                        CheckLine(r.address or "?", "connect", r.summary, r.error_type)
                        for r in results
                        if r.connect_failure
                    )
                    events.extend(
                        await self._vpc_events(
                            vpc,
                            NotificationKind.VPC_UNREACHABLE,
                            occurred_at=now,
                            failed_checks=failures,
                            total_checks=len(known),
                            failing=failing,
                        )
                    )
            elif tick_failures * 100 < settings.VPC_UNREACHABLE_PERCENT * len(results):
                downtime = (now - vpc.unreachable_since).total_seconds()
                vpc.unreachable_since = None
                recovered.add(vpc_id)
                logger.info("VPC %s reachable again after %.0f s.", vpc.name, downtime)
                events.extend(
                    await self._vpc_events(
                        vpc, NotificationKind.VPC_RECOVERED, occurred_at=now, downtime_seconds=downtime
                    )
                )
        if events or recovered:
            await self.session.commit()
        return events, recovered

    async def _forget_unwatched_vpcs(self) -> None:
        """Clear `unreachable` from VPCs nothing probes into any more: their
        resources paused, deleted or gone from AWS, or every check left sent
        over the internet. No result would ever clear it otherwise. Quietly:
        nothing answered, so there is no vpc_recovered to send."""
        vpcs = list(await self.session.scalars(select(AwsVpc).where(AwsVpc.unreachable_since.is_not(None))))
        if not vpcs:
            return
        rows = await self.session.execute(
            select(AwsResource.vpc_id, AwsResource.kind, AwsResource.aws_detail, AwsCheck.settings)
            .join(AwsCheck, AwsCheck.resource_id == AwsResource.id)
            .where(
                AwsResource.vpc_id.in_([vpc.id for vpc in vpcs]),
                AwsResource.is_enabled.is_(True),
                AwsResource.missing_since.is_(None),
                AwsCheck.is_enabled.is_(True),
                AwsCheck.check_type.in_(list(NETWORK_CHECKS)),
            )
        )
        watched = {
            vpc_id for vpc_id, kind, detail, check_settings in rows if not is_public_path(kind, detail, check_settings)
        }
        forgotten = {vpc.id for vpc in vpcs if vpc.id not in watched}
        if not forgotten:
            return
        for vpc in vpcs:
            if vpc.id in forgotten:
                vpc.unreachable_since = None
                logger.info("VPC %s no longer marked unreachable: no check probes into it.", vpc.name)
        await self.session.commit()
        await self._release_held(forgotten)

    async def _release_held(self, vpc_ids: set[int]) -> None:
        """Resources still down when their VPC came back send the down alert
        they held; ones that came back with it say nothing."""
        held = await self.session.scalars(
            select(AwsResource).where(
                AwsResource.vpc_id.in_(vpc_ids),
                AwsResource.down_alert_held.is_(True),
            )
        )
        for resource in held:
            if resource.status is not ResourceStatus.DOWN:
                resource.down_alert_held = False
                continue
            now = datetime.now(UTC)
            resource.down_alert_held = False
            resource.down_alerts_sent = 1
            event = self._outage(NotificationKind.INFRA_DOWN, resource, now, attempt=1)
            self.session.add(
                self._feed(event, resource.vpc_id, "; ".join(line.text() for line in event.failing))
            )
            await self.session.commit()
            await self.notifier.dispatch(event)
            await self._keep_threads(resource, [event])
        await self.session.commit()

    # -- entry points --------------------------------------------------------

    async def check_resource(self, resource: AwsResource) -> ResourceOutcome:
        """Run every enabled check of one resource now, as a scheduled run
        would. Raises IcmpUnavailableError when a ping cannot be sent here."""
        checks = [c for c in resource.checks if c.is_enabled]
        targets = [build_target(check, resource, resource.vpc) for check in checks]
        results = await asyncio.gather(
            *(probes.run(target, check.retries_on_failure) for check, target in zip(checks, targets))
        )
        hold = resource.vpc.unreachable_since is not None
        return await self.record(resource, list(zip(checks, results)), hold=hold)

    async def run_due_checks(self) -> list[ResourceOutcome]:
        """Probe every check whose interval has elapsed. Called by the scheduler."""
        await self._forget_unwatched_vpcs()
        due = await self.service.due_resources()
        if not due:
            return []
        logger.info("Checking %d due infrastructure check(s).", sum(len(c) for _, c in due))
        jobs = [
            (resource, check, build_target(check, resource, resource.vpc))
            for resource, checks in due
            for check in checks
        ]

        gate = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

        async def probe(target: ProbeTarget, retries: int) -> ProbeResult:
            async with gate:
                return await probes.run(target, retries)

        results = await asyncio.gather(
            *(probe(target, check.retries_on_failure) for _, check, target in jobs),
            return_exceptions=True,
        )
        by_resource: dict[int, tuple[AwsResource, list[tuple[AwsCheck, ProbeResult]]]] = {}
        for (resource, check, _), result in zip(jobs, results, strict=True):
            if isinstance(result, IcmpUnavailableError):
                logger.error("Could not ping for %s: %s", resource.name, result)
                continue
            if isinstance(result, BaseException):
                logger.error("Unexpected error checking %s / %s", resource.name, check.name, exc_info=result)
                continue
            by_resource.setdefault(resource.id, (resource, []))[1].append((check, result))

        vpc_events, recovered = await self._evaluate_vpcs(by_resource)
        outcomes = []
        for resource, pairs in by_resource.values():
            # Deleted, paused or edited while its probes ran: record only what
            # still applies.
            try:
                await self.service.reload(resource)
            except Exception:  # noqa: BLE001 - the row is gone
                logger.info("%s was deleted during its checks; results dropped.", resource.name)
                continue
            if not resource.is_enabled:
                continue
            live = {c.id: c for c in resource.checks if c.is_enabled}
            pairs = [(live[c.id], r) for c, r in pairs if c.id in live]
            if not pairs:
                continue
            outcomes.append(
                await self.record(resource, pairs, hold=resource.vpc.unreachable_since is not None)
            )
        if recovered:
            await self._release_held(recovered)
        for event in vpc_events:
            await self.notifier.dispatch(event)
        return outcomes

    async def sync_due_vpcs(self) -> int:
        """Read resources from AWS again for every VPC whose sync is due, and
        tell of the instances that joined or left an Auto Scaling group whose
        user asked to hear of it."""
        vpcs = await self.service.vpcs_due_for_sync()
        for vpc in vpcs:
            try:
                changes = await self.service.sync_vpc(vpc)
            except Exception:
                logger.exception("Sync of VPC %s failed; continuing.", vpc.name)
                await self.session.rollback()
                continue
            for change in changes:
                try:
                    await self.notify_scaling(change)
                except Exception:
                    logger.exception("Scaling notification for %s failed; continuing.", change.resource.name)
                    await self.session.rollback()
        return len(vpcs)

    # -- scaling -------------------------------------------------------------

    async def _probe_new_instance(self, resource: AwsResource, member: dict) -> ScaledInstance:
        """The instance, and, if the group has a health endpoint, what it said."""
        config = resource.scale_health_check
        use_public = bool((config or {}).get("use_public_ip"))
        address = member.get("public_ip") if use_public else member.get("private_ip")
        instance = ScaledInstance(id=member["id"], az=member.get("az"), address=address)
        if not config:
            return instance
        target = ProbeTarget(
            check_type=InfraCheckType.HTTP,
            settings=config,
            timeout=10.0,
            kind=ResourceKind.SERVER,
            resource_name=member["id"],
            aws_id=member["id"],
            address=address,
            aws_detail={},
            region=client.Region.of(resource.vpc),
            scope=egress.vpc_scope(resource.vpc.name, resource.vpc.cidrs, allow_public=use_public),
        )
        try:
            result = await probes.run(target, retries=2)
        except Exception as exc:  # noqa: BLE001 - a probe never blocks the notification
            return replace(instance, health_ok=False, health=f"could not be probed: {exc}"[:200])
        return replace(instance, health_ok=result.ok, health=result.summary[:200])

    async def notify_scaling(self, change: ScalingChange) -> list[Notification]:
        """One notification per direction that the group's user turned on. A
        group in maintenance, or paused, says nothing."""
        resource = change.resource
        if not resource.is_enabled or await self.service.in_maintenance(resource.id):
            return []
        detail = resource.aws_detail or {}
        now = datetime.now(UTC)
        common = {
            "resource": ResourceSnapshot.of(resource),
            "checked_at": now,
            "in_service": len(self._in_service(resource)),
            "desired": detail.get("desired_capacity") or 0,
            "recipients": self.recipients_for(resource),
            "slack": self._slack(resource),
            "telegram": self._telegram(resource),
            "whatsapp": whatsapp_target(resource.project),
        }
        events: list[Notification] = []
        if change.added and resource.notify_scale_out:
            config = resource.scale_health_check
            endpoint = None
            if config:
                scheme = config.get("scheme", "http")
                port = config.get("port") or (443 if scheme == "https" else 80)
                endpoint = f"{scheme.upper()} port {port}, GET {config.get('path', '/health')}"
            instances = await asyncio.gather(*(self._probe_new_instance(resource, m) for m in change.added))
            events.append(
                ScalingEvent(
                    kind=NotificationKind.ASG_SCALED_OUT,
                    instances=tuple(instances),
                    health_endpoint=endpoint,
                    **common,
                )
            )
        if change.removed and resource.notify_scale_in:
            events.append(
                ScalingEvent(
                    kind=NotificationKind.ASG_SCALED_IN,
                    instances=tuple(ScaledInstance(id=i) for i in change.removed),
                    **common,
                )
            )
        for event in events:
            self.session.add(
                self._feed(
                    event,
                    resource.vpc_id,
                    f"{event.count} {'added' if event.is_out else 'removed'}: "
                    + ", ".join(i.id for i in event.instances[:5])
                    + (" …" if len(event.instances) > 5 else "")
                    + (" (health endpoint failed on some)" if event.health_failed else ""),
                )
            )
        if events:
            await self.session.commit()
            for event in events:
                await self.notifier.dispatch(event)
        return events

    @staticmethod
    def _in_service(resource: AwsResource) -> list[dict]:
        return [m for m in (resource.aws_detail or {}).get("instances", []) if m.get("lifecycle_state") == "InService"]
