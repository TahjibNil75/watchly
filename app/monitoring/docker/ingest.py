"""Turning an agent's push into state, history and alerts.

Each push carries every container on the host. A container is matched to its
row by name, and decided as follows:

    down        it was seen running, stopped (exited, dead, or stuck
                restarting), and has stayed so DOCKER_DOWN_GRACE_SECONDS.
                A container first seen stopped never alerts: it may be a
                finished job. Running again sends one recovery.
    unhealthy   running, but its own healthcheck says unhealthy.
    oom         Docker reported an `oom` event, or it exited OOM-killed.
    restart_loop  DOCKER_RESTART_LOOP_COUNT restarts by its restart policy
                within DOCKER_RESTART_LOOP_WINDOW_SECONDS.
    high_cpu / high_memory  over DOCKER_CPU_ALERT_PERCENT (of the host's
                cores) or DOCKER_MEMORY_ALERT_PERCENT (of its limit) for
                DOCKER_PROBLEM_CHECKS heartbeats in a row.

The problems alert as they open, at most every
DOCKER_PROBLEM_ALERT_COOLDOWN_SECONDS per container and problem; a muted
container is tracked but never alerts. A container missing from a push was
removed: it is kept, marked removed, and alerts nothing.

When the agent cannot reach Docker the host is `docker_down`, and its
containers keep their last known state; see `monitor.py` for the agent going
quiet altogether.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_token
from app.monitoring.alerts.base import Notification, NotificationKind, format_duration
from app.monitoring.docker.events import (
    ContainerSnapshot,
    DockerContainerEvent,
    DockerHostEvent,
    HostSnapshot,
)
from app.monitoring.docker.models import (
    REMOVED,
    UP_STATES,
    DockerContainer,
    DockerEvent,
    DockerHost,
    DockerSample,
    EventSource,
    HostStatus,
)
from app.monitoring.docker.schemas import (
    AgentConfig,
    Ingest,
    IngestContainer,
    IngestEvent,
    IngestResponse,
)
from app.monitoring.notifications.dispatcher import Notifier
from app.monitoring.notifications.recipients import (
    project_recipients,
    slack_target,
    telegram_target,
    whatsapp_target,
)

logger = logging.getLogger(__name__)

AGENT_QUIET = "the agent stopped reporting"
#: Most restarts counted from one push, so a counter jump cannot bloat a row.
MAX_RESTARTS_PER_PUSH = 20


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def format_bytes(value: float | int | None) -> str:
    """`536870912` -> `"512 MiB"`."""
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit in ("B", "KiB", "MiB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def host_trouble_reason(host: DockerHost) -> str:
    if host.status == HostStatus.DOCKER_DOWN.value:
        return f"Docker is not answering the agent: {host.docker_error or 'no reason given'}"
    return AGENT_QUIET


@dataclass(slots=True)
class _Decision:
    """An alert to send once the rows have ids."""

    kind: NotificationKind
    container: DockerContainer | None
    detail: str
    event_kind: str
    downtime_seconds: int | None = None


def _open(problems: dict, key: str, detail: str, now: datetime) -> bool:
    """Open problem `key` (or refresh its detail). True when this opening
    should alert: it was closed, and the last alert is past the cooldown."""
    entry = dict(problems.get(key) or {})
    entry["detail"] = detail
    alert = False
    if not entry.get("since"):
        entry["since"] = now.isoformat()
        last = _parse(entry.get("last_alert_at"))
        if last is None or (now - last).total_seconds() >= settings.DOCKER_PROBLEM_ALERT_COOLDOWN_SECONDS:
            entry["last_alert_at"] = now.isoformat()
            alert = True
    problems[key] = entry
    return alert


def _close(problems: dict, key: str) -> None:
    """Close problem `key`, keeping when it last alerted so the cooldown holds
    if it comes straight back, and its counters."""
    entry = problems.get(key)
    if entry and entry.get("since"):
        problems[key] = {k: v for k, v in entry.items() if k not in ("since", "detail")}


class DockerIngestService:
    def __init__(self, session: AsyncSession, notifier: Notifier | None = None) -> None:
        self.session = session
        self.notifier = notifier or Notifier(session)

    async def authenticate(self, token: str) -> DockerHost | None:
        return await self.session.scalar(select(DockerHost).where(DockerHost.token_hash == hash_token(token)))

    async def ingest(self, host_id: int, push: Ingest, now: datetime | None = None) -> IngestResponse:
        now = now or datetime.now(UTC)
        # Locked, so the liveness job cannot mark it offline mid-push.
        host = await self.session.scalar(
            select(DockerHost)
            .where(DockerHost.id == host_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if host is None:  # deleted since it was authenticated
            raise LookupError(host_id)

        self._update_host(host, push, now)
        decisions: list[_Decision] = []
        rows: dict[str, DockerContainer] = {}
        if push.docker_error or push.containers is None:
            host.docker_error = (push.docker_error or "no container list")[:2000]
            decisions += self._host_trouble(host, HostStatus.DOCKER_DOWN, now)
        else:
            host.docker_error = None
            decisions += self._host_ok(host, now)
            rows = await self._apply_containers(host, push, now, decisions)

        # Ids for the new containers, which the events and alerts point to.
        await self.session.flush()
        await self._store_docker_events(host, push.events, rows)
        alerts = [self._alert(host, d, now) for d in decisions]
        for decision in decisions:
            self.session.add(self._feed_row(host, decision, now))
        # Commit before sending, so a slow channel can never make the same
        # alert go out twice.
        await self.session.commit()
        for alert in alerts:
            if alert is not None:
                await self.notifier.dispatch(alert)
        return IngestResponse(config=AgentConfig(interval_s=host.interval_seconds, ignore=list(host.ignore_patterns)))

    # --- host ---------------------------------------------------------------

    @staticmethod
    def _update_host(host: DockerHost, push: Ingest, now: datetime) -> None:
        host.last_seen_at = now
        if push.agent.version:
            host.agent_version = push.agent.version
        host.agent_mem_bytes = push.agent.mem_bytes
        host.dropped_events = (host.dropped_events or 0) + push.dropped_events
        if push.dropped_events:
            logger.warning("Docker agent of %s dropped %d events.", host.name, push.dropped_events)
        h = push.host
        if h is not None:
            host.hostname = h.hostname or host.hostname
            host.os = h.os or host.os
            host.kernel = h.kernel or host.kernel
            host.arch = h.arch or host.arch
            host.docker_version = h.docker_version or host.docker_version
            host.cpus = h.cpus or host.cpus
            host.mem_total_bytes = h.mem_total_bytes or host.mem_total_bytes
            host.containers_total = h.containers_total
            host.containers_running = h.containers_running

    @staticmethod
    def _host_trouble(host: DockerHost, status: HostStatus, now: datetime) -> list[_Decision]:
        """The host lost Docker (or its agent, from `monitor.py`). Alerts once,
        as it leaves `online`; a host never seen online stays quiet."""
        decisions = []
        if host.status == HostStatus.ONLINE.value:
            host.down_since = now
            host.down_alerted = True
            host.status = status.value
            decisions.append(
                _Decision(NotificationKind.DOCKER_HOST_OFFLINE, None, host_trouble_reason(host), "host_offline")
            )
        else:
            host.down_since = host.down_since or now
            host.status = status.value
        return decisions

    @staticmethod
    def _host_ok(host: DockerHost, now: datetime) -> list[_Decision]:
        decisions = []
        if host.status != HostStatus.ONLINE.value:
            if host.down_alerted and host.down_since:
                was = host_trouble_reason(host)
                downtime = int((now - host.down_since).total_seconds())
                decisions.append(
                    _Decision(NotificationKind.DOCKER_HOST_RECOVERED, None, was, "host_recovered", downtime)
                )
            elif host.status == HostStatus.PENDING.value:
                logger.info("Docker host %s reported for the first time.", host.name)
            host.status = HostStatus.ONLINE.value
            host.down_since = None
            host.down_alerted = False
        return decisions

    # --- containers -----------------------------------------------------------

    async def _apply_containers(
        self, host: DockerHost, push: Ingest, now: datetime, decisions: list[_Decision]
    ) -> dict[str, DockerContainer]:
        """Bring every container row in line with the push. Returns the rows by
        Docker id (current, and the old ids of removed ones)."""
        existing = {
            row.name: row
            for row in await self.session.scalars(select(DockerContainer).where(DockerContainer.host_id == host.id))
        }
        events_by_id: dict[str, list[IngestEvent]] = {}
        for event in push.events:
            events_by_id.setdefault(event.container_id, []).append(event)

        seen: set[str] = set()
        by_docker_id: dict[str, DockerContainer] = {}
        for reported in push.containers or []:
            if reported.name in seen:  # cannot happen in Docker; never trust it
                continue
            seen.add(reported.name)
            row = existing.get(reported.name)
            row = self._apply(host, row, reported, push, events_by_id.get(reported.id, []), now, decisions)
            by_docker_id[reported.id] = row

        for name, row in existing.items():
            if name not in seen:
                by_docker_id.setdefault(row.docker_id, row)
                if row.state != REMOVED:
                    row.state = REMOVED
                    row.removed_at = now
                    row.down_since = None
                    row.down_alerted = False
                    row.problems = {}
                    row.metrics = None
        return by_docker_id

    def _apply(
        self,
        host: DockerHost,
        row: DockerContainer | None,
        reported: IngestContainer,
        push: Ingest,
        events: list[IngestEvent],
        now: datetime,
        decisions: list[_Decision],
    ) -> DockerContainer:
        new = row is None
        if row is None:
            row = DockerContainer(
                host_id=host.id,
                project_id=host.project_id,
                name=reported.name,
                first_seen_at=now,
                problems={},
                down_alerted=False,
                muted=False,
            )
            self.session.add(row)
        prev_state = None if new else row.state
        prev_restarts = 0 if new else row.restart_count
        prev_finished = None if new else row.finished_at
        recreated = not new and row.docker_id != reported.id

        labels = reported.labels or {}
        row.docker_id = reported.id
        row.image = reported.image
        row.image_id = reported.image_id or None
        row.state = reported.state
        row.status_text = reported.status or None
        row.health = reported.health or "none"
        row.restart_count = reported.restart_count
        row.exit_code = reported.exit_code
        row.oom_killed = reported.oom_killed
        row.docker_created_at = reported.created_at
        row.started_at = reported.started_at
        row.finished_at = reported.finished_at
        row.compose_project = labels.get("com.docker.compose.project")
        row.compose_service = labels.get("com.docker.compose.service")
        row.last_seen_at = now
        row.removed_at = None

        problems = {key: dict(value) for key, value in (row.problems or {}).items()}
        heartbeat = push.kind == "heartbeat"
        if heartbeat and reported.metrics is not None and reported.state == "running":
            row.metrics = reported.metrics.model_dump()
            row.metrics_at = now
            self._sample(row, reported, now)
        elif reported.state not in UP_STATES:
            row.metrics = None

        for event in events:
            if event.action == "stop":
                problems["manual_stop"] = {"at": event.time.isoformat()}

        alert = not row.muted

        def decide(kind: NotificationKind, detail: str, event_kind: str, downtime: int | None = None) -> None:
            if alert:
                decisions.append(_Decision(kind, row, detail, event_kind, downtime))

        # Down and back.
        if row.state in UP_STATES:
            if row.down_since is not None:
                if row.down_alerted:
                    downtime = int((now - row.down_since).total_seconds())
                    decisions.append(
                        _Decision(NotificationKind.DOCKER_CONTAINER_RECOVERED, row, "running again", "recovered", downtime)
                    )
                row.down_since = None
                row.down_alerted = False
            problems.pop("manual_stop", None)
        else:
            if row.down_since is None and prev_state in UP_STATES:
                finished = reported.finished_at
                row.down_since = finished if finished and now - timedelta(days=1) <= finished <= now else now
            if (
                row.down_since is not None
                and not row.down_alerted
                and (now - row.down_since).total_seconds() >= settings.DOCKER_DOWN_GRACE_SECONDS
            ):
                if alert:
                    row.down_alerted = True
                downtime = int((now - row.down_since).total_seconds())
                decide(NotificationKind.DOCKER_CONTAINER_DOWN, self._down_detail(row, problems), "down", downtime)

        # Unhealthy.
        if row.state == "running" and row.health == "unhealthy":
            if _open(problems, "unhealthy", "its healthcheck reports unhealthy", now):
                decide(NotificationKind.DOCKER_CONTAINER_UNHEALTHY, "its healthcheck reports unhealthy", "unhealthy")
        else:
            _close(problems, "unhealthy")

        # Killed for memory: an oom event, or a new exit that was an OOM kill.
        oom = any(e.action == "oom" for e in events) or (
            not new and reported.oom_killed and reported.finished_at is not None and reported.finished_at != prev_finished
        )
        if oom:
            entry = dict(problems.get("oom") or {})
            entry["last_at"] = now.isoformat()
            entry["count"] = int(entry.get("count", 0)) + 1
            last = _parse(entry.get("last_alert_at"))
            if last is None or (now - last).total_seconds() >= settings.DOCKER_PROBLEM_ALERT_COOLDOWN_SECONDS:
                entry["last_alert_at"] = now.isoformat()
                limit = (row.metrics or {}).get("mem_limit_bytes") or (problems.get("_last_limit") or {}).get("bytes")
                detail = (
                    f"killed for using all of its {format_bytes(limit)} memory limit"
                    if limit
                    else "killed by the kernel for lack of memory"
                )
                decide(NotificationKind.DOCKER_CONTAINER_OOM, detail, "oom")
            problems["oom"] = entry
        if row.metrics and row.metrics.get("mem_limit_bytes"):
            problems["_last_limit"] = {"bytes": row.metrics["mem_limit_bytes"]}

        # Restart loop: restarts counted by Docker's restart policy.
        window = settings.DOCKER_RESTART_LOOP_WINDOW_SECONDS
        loop = dict(problems.get("restart_loop") or {})
        times = [t for t in loop.get("times", []) if (p := _parse(t)) and (now - p).total_seconds() < window]
        if not new and not recreated and reported.restart_count > prev_restarts:
            times += [now.isoformat()] * min(reported.restart_count - prev_restarts, MAX_RESTARTS_PER_PUSH)
        loop["times"] = times[-50:]
        problems["restart_loop"] = loop
        if len(times) >= settings.DOCKER_RESTART_LOOP_COUNT:
            detail = f"restarted {len(times)} times in the last {format_duration(window)}"
            if _open(problems, "restart_loop", detail, now):
                decide(NotificationKind.DOCKER_RESTART_LOOP, detail, "restart_loop")
        elif not times:
            _close(problems, "restart_loop")

        # Running hot, on heartbeats only (they carry the samples).
        if heartbeat:
            metrics = row.metrics if row.state == "running" else None
            self._threshold(
                problems,
                "high_cpu",
                self._cpu_over(metrics, host),
                lambda share, streak: f"CPU at {share:.0f}% of the host's {host.cpus} cores for {streak} heartbeats",
                now,
                decide,
            )
            self._threshold(
                problems,
                "high_memory",
                self._memory_over(metrics),
                lambda pct, streak: (
                    f"memory at {pct:.0f}% of its {format_bytes((metrics or {}).get('mem_limit_bytes'))} limit "
                    f"for {streak} heartbeats"
                ),
                now,
                decide,
            )

        row.problems = problems
        return row

    def _sample(self, row: DockerContainer, reported: IngestContainer, now: datetime) -> None:
        m = reported.metrics
        self.session.add(
            DockerSample(
                container=row,
                at=now,
                cpu_pct=m.cpu_pct,
                mem_used_bytes=m.mem_used_bytes,
                mem_limit_bytes=m.mem_limit_bytes,
                mem_pct=m.mem_pct,
                net_rx_bps=m.net_rx_bps,
                net_tx_bps=m.net_tx_bps,
                blk_read_bps=m.blk_read_bps,
                blk_write_bps=m.blk_write_bps,
                pids=m.pids,
            )
        )

    @staticmethod
    def _cpu_over(metrics: dict | None, host: DockerHost) -> float | None:
        """The container's share of the host's CPU when over the threshold."""
        threshold = settings.DOCKER_CPU_ALERT_PERCENT
        if not metrics or not threshold or metrics.get("cpu_pct") is None:
            return None
        share = metrics["cpu_pct"] / max(host.cpus or 1, 1)
        return share if share >= threshold else None

    @staticmethod
    def _memory_over(metrics: dict | None) -> float | None:
        threshold = settings.DOCKER_MEMORY_ALERT_PERCENT
        if not metrics or not threshold or metrics.get("mem_pct") is None:
            return None
        return metrics["mem_pct"] if metrics["mem_pct"] >= threshold else None

    @staticmethod
    def _threshold(problems, key, value, describe, now, decide) -> None:
        entry = dict(problems.get(key) or {})
        streak = int(entry.get("streak", 0)) + 1 if value is not None else 0
        entry["streak"] = streak
        problems[key] = entry
        if streak >= settings.DOCKER_PROBLEM_CHECKS:
            detail = describe(value, streak)
            if _open(problems, key, detail, now):
                decide(NotificationKind.DOCKER_RESOURCE_HIGH, detail, key)
        elif streak == 0:
            _close(problems, key)

    @staticmethod
    def _down_detail(row: DockerContainer, problems: dict) -> str:
        code = row.exit_code
        stopped = _parse((problems.get("manual_stop") or {}).get("at"))
        if row.state == "restarting":
            return f"keeps restarting; last exit code {code}"
        if row.state == "dead":
            return "is dead (Docker could not stop or remove it cleanly)"
        if row.oom_killed:
            return f"was killed for lack of memory (exit code {code})"
        if stopped and row.down_since and abs((row.down_since - stopped).total_seconds()) < 60:
            return f"was stopped with docker stop (exit code {code})"
        meaning = {0: "exited normally", 137: "killed", 143: "terminated", 139: "segmentation fault", 1: "error"}
        if code is None:
            return f"is {row.state}"
        extra = f" ({meaning[code]})" if code in meaning else ""
        return f"exited with code {code}{extra}"

    # --- feed and alerts ---------------------------------------------------------

    async def _store_docker_events(
        self, host: DockerHost, events: list[IngestEvent], rows: dict[str, DockerContainer]
    ) -> None:
        if not events:
            return
        by_name = {row.name: row for row in rows.values()}
        values = []
        for event in events:
            row = rows.get(event.container_id) or by_name.get(event.container_name)
            name = event.container_name or (row.name if row else event.container_id[:12])
            values.append(
                {
                    "project_id": host.project_id,
                    "host_id": host.id,
                    "container_id": row.id if row else None,
                    "container_name": name,
                    "source": EventSource.DOCKER.value,
                    "kind": event.action,
                    "occurred_at": event.time,
                    "summary": _event_summary(name, event)[:2000],
                    "exit_code": event.exit_code,
                    "event_key": event.id,
                }
            )
        await self.session.execute(
            insert(DockerEvent).values(values).on_conflict_do_nothing(constraint="uq_docker_events_host_key")
        )

    @staticmethod
    def _feed_row(host: DockerHost, decision: _Decision, now: datetime) -> DockerEvent:
        c = decision.container
        if c is None:
            summary = (
                f"{host.name} reports again after {format_duration(decision.downtime_seconds or 0)}"
                if decision.kind is NotificationKind.DOCKER_HOST_RECOVERED
                else f"{host.name} is offline: {decision.detail}"
            )
        else:
            summary = {
                NotificationKind.DOCKER_CONTAINER_UNHEALTHY: f"{c.name} is unhealthy: its healthcheck fails",
                NotificationKind.DOCKER_CONTAINER_OOM: f"{c.name} was {decision.detail}",
                NotificationKind.DOCKER_RESOURCE_HIGH: f"{c.name}: {decision.detail}",
                NotificationKind.DOCKER_CONTAINER_RECOVERED: (
                    f"{c.name} is running again after {format_duration(decision.downtime_seconds or 0)}"
                ),
            }.get(decision.kind, f"{c.name} {decision.detail}")
        return DockerEvent(
            project_id=host.project_id,
            host_id=host.id,
            container_id=c.id if c else None,
            container_name=c.name if c else None,
            source=EventSource.WATCHLY.value,
            kind=decision.event_kind,
            occurred_at=now,
            summary=summary[:2000],
            exit_code=c.exit_code if c and decision.event_kind == "down" else None,
            downtime_seconds=decision.downtime_seconds,
        )

    @staticmethod
    def _alert(host: DockerHost, decision: _Decision, now: datetime) -> Notification:
        project = host.project
        targets = {
            "recipients": project_recipients(project),
            "slack": slack_target(project),
            "telegram": telegram_target(project),
            "whatsapp": whatsapp_target(project),
        }
        if decision.container is None:
            return DockerHostEvent(
                kind=decision.kind,
                host=HostSnapshot.of(host),
                reason=decision.detail,
                occurred_at=now,
                downtime_seconds=decision.downtime_seconds,
                **targets,
            )
        return DockerContainerEvent(
            kind=decision.kind,
            container=ContainerSnapshot.of(decision.container, host),
            detail=decision.detail,
            occurred_at=now,
            downtime_seconds=decision.downtime_seconds,
            cooldown_seconds=settings.DOCKER_PROBLEM_ALERT_COOLDOWN_SECONDS,
            **targets,
        )


def _event_summary(name: str, event: IngestEvent) -> str:
    match event.action:
        case "start":
            return f"{name} started"
        case "die":
            code = "" if event.exit_code is None else f" with code {event.exit_code}"
            return f"{name} exited{code}"
        case "oom":
            return f"{name} ran out of memory"
        case "restart":
            return f"{name} was restarted"
        case "kill":
            return f"{name} was sent signal {event.signal or '?'}"
        case "stop":
            return f"{name} was stopped"
        case "destroy":
            return f"{name} was removed"
        case "pause":
            return f"{name} was paused"
        case "unpause":
            return f"{name} was unpaused"
        case "health_status":
            return f"{name} is {event.health or 'health unknown'}"
        case "rename":
            return f"{name} was renamed"
    return f"{name}: {event.action}"


__all__ = ["AGENT_QUIET", "DockerIngestService", "format_bytes", "host_trouble_reason"]
