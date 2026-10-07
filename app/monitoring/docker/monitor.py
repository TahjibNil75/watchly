"""What runs on the scheduler's tick for Docker monitoring.

The agents push; nothing here talks to a host. A host whose agent has gone
quiet for DOCKER_OFFLINE_AFTER_SECONDS (or three of its intervals, if longer)
is offline: one alert for the host, and its containers' alerts wait for it to
report again, since nobody can tell what they are doing meanwhile.

Also: rolling raw samples up by the hour, and retention of samples, the feed,
and containers removed from their hosts.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.monitoring.alerts.base import NotificationKind
from app.monitoring.docker.events import DockerHostEvent, HostSnapshot
from app.monitoring.docker.ingest import AGENT_QUIET
from app.monitoring.docker.models import (
    DockerContainer,
    DockerEvent,
    DockerHost,
    DockerSample,
    DockerSampleHourly,
    EventSource,
    HostStatus,
)
from app.monitoring.docker.schemas import ContainerStats, StatsPoint
from app.monitoring.notifications.dispatcher import Notifier
from app.monitoring.notifications.recipients import (
    project_recipients,
    slack_target,
    telegram_target,
    whatsapp_target,
)
from app.monitoring.websites.history import DAY, HOUR, truncate
from app.monitoring.websites.schemas import StatsRange

logger = logging.getLogger(__name__)

#: Re-sums every hour from :since on; idempotent, like the other rollups.
ROLLUP_SQL = text("""
INSERT INTO docker_sample_hourly
    (container_id, hour, samples, cpu_avg, cpu_max, mem_avg, mem_max, mem_pct_max, net_rx_avg, net_tx_avg)
SELECT container_id,
       date_trunc('hour', at, 'UTC') AS hour,
       count(*),
       avg(cpu_pct),
       max(cpu_pct),
       avg(mem_used_bytes),
       max(mem_used_bytes),
       max(mem_pct),
       avg(net_rx_bps),
       avg(net_tx_bps)
FROM docker_samples
WHERE at >= :since
GROUP BY container_id, hour
ON CONFLICT (container_id, hour) DO UPDATE SET
    samples = EXCLUDED.samples,
    cpu_avg = EXCLUDED.cpu_avg,
    cpu_max = EXCLUDED.cpu_max,
    mem_avg = EXCLUDED.mem_avg,
    mem_max = EXCLUDED.mem_max,
    mem_pct_max = EXCLUDED.mem_pct_max,
    net_rx_avg = EXCLUDED.net_rx_avg,
    net_tx_avg = EXCLUDED.net_tx_avg
""")

#: The last day straight from the raw samples, in five-minute buckets.
RAW_SERIES_SQL = text("""
SELECT date_bin('5 minutes', at, TIMESTAMPTZ '2000-01-01') AS bucket,
       avg(cpu_pct), max(cpu_pct), avg(mem_used_bytes), max(mem_used_bytes), max(mem_pct),
       avg(net_rx_bps), avg(net_tx_bps)
FROM docker_samples
WHERE container_id = :container_id AND at >= :since
GROUP BY bucket
ORDER BY bucket
""")


def offline_after(host: DockerHost) -> timedelta:
    return timedelta(seconds=max(settings.DOCKER_OFFLINE_AFTER_SECONDS, 3 * host.interval_seconds))


class DockerMonitor:
    def __init__(self, session: AsyncSession, notifier: Notifier | None = None) -> None:
        self.session = session
        self.notifier = notifier or Notifier(session)

    async def mark_offline(self, now: datetime | None = None) -> int:
        """Hosts whose agent went quiet become offline; those that were online
        alert. Returns how many changed."""
        now = now or datetime.now(UTC)
        floor = now - timedelta(seconds=settings.DOCKER_OFFLINE_AFTER_SECONDS)
        hosts = list(
            await self.session.scalars(
                select(DockerHost)
                .where(
                    DockerHost.status.in_([HostStatus.ONLINE.value, HostStatus.DOCKER_DOWN.value]),
                    DockerHost.last_seen_at < floor,
                )
                .order_by(DockerHost.id)
                # A host mid-push holds its lock; it is not offline.
                .with_for_update(skip_locked=True)
            )
        )
        alerts = []
        changed = 0
        for host in hosts:
            if host.last_seen_at is None or now - host.last_seen_at < offline_after(host):
                continue
            changed += 1
            if host.status == HostStatus.ONLINE.value:
                host.down_since = host.last_seen_at
                host.down_alerted = True
                project = host.project
                alerts.append(
                    DockerHostEvent(
                        kind=NotificationKind.DOCKER_HOST_OFFLINE,
                        host=HostSnapshot.of(host),
                        reason=AGENT_QUIET,
                        occurred_at=now,
                        recipients=project_recipients(project),
                        slack=slack_target(project),
                        telegram=telegram_target(project),
                        whatsapp=whatsapp_target(project),
                    )
                )
                self.session.add(
                    DockerEvent(
                        project_id=host.project_id,
                        host_id=host.id,
                        source=EventSource.WATCHLY.value,
                        kind="host_offline",
                        occurred_at=now,
                        summary=f"{host.name} is offline: {AGENT_QUIET}",
                    )
                )
                logger.warning("Docker host %s went quiet; marking it offline.", host.name)
            host.status = HostStatus.OFFLINE.value
        await self.session.commit()
        for alert in alerts:
            await self.notifier.dispatch(alert)
        return changed


class DockerHistoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rolled_up_to(self) -> datetime | None:
        return await self.session.scalar(select(func.max(DockerSampleHourly.hour)))

    async def rollup(self, now: datetime | None = None) -> None:
        """Bring `docker_sample_hourly` up to date. Called on every tick."""
        previous_hour = truncate(now or datetime.now(UTC), HOUR) - HOUR
        latest = await self._rolled_up_to()
        since = datetime(1970, 1, 1, tzinfo=UTC) if latest is None else min(latest, previous_hour)
        await self.session.execute(ROLLUP_SQL, {"since": since})
        await self.session.commit()

    async def purge(self, now: datetime | None = None, *, batch_size: int = 5_000, max_batches: int = 20) -> int:
        """Delete raw samples past DOCKER_SAMPLE_RETENTION_HOURS that are
        already rolled up, the feed past CHECK_RETENTION_DAYS, and containers
        removed DOCKER_REMOVED_CONTAINER_DAYS ago."""
        now = now or datetime.now(UTC)
        deleted = 0
        latest = await self._rolled_up_to()
        if latest is not None:
            cutoff = min(now - timedelta(hours=settings.DOCKER_SAMPLE_RETENTION_HOURS), latest - HOUR)
            for _ in range(max_batches):
                batch = select(DockerSample.id).where(DockerSample.at < cutoff).limit(batch_size).scalar_subquery()
                result = await self.session.execute(delete(DockerSample).where(DockerSample.id.in_(batch)))
                await self.session.commit()
                deleted += result.rowcount or 0
                if (result.rowcount or 0) < batch_size:
                    break

        feed_cutoff = now - timedelta(days=settings.CHECK_RETENTION_DAYS)
        result = await self.session.execute(delete(DockerEvent).where(DockerEvent.occurred_at < feed_cutoff))
        deleted += result.rowcount or 0
        removed_cutoff = now - timedelta(days=settings.DOCKER_REMOVED_CONTAINER_DAYS)
        result = await self.session.execute(
            delete(DockerContainer).where(DockerContainer.removed_at < removed_cutoff)
        )
        deleted += result.rowcount or 0
        await self.session.commit()
        return deleted

    async def stats(
        self, container: DockerContainer, range_: StatsRange, now: datetime | None = None
    ) -> ContainerStats:
        """CPU, memory and network over `range_`: the last day from raw
        samples in five-minute buckets, longer ranges from the hourly
        rollups (by the hour for a week, by the day beyond)."""
        now = now or datetime.now(UTC)
        host = container.host
        limit = (container.metrics or {}).get("mem_limit_bytes")
        if range_ is StatsRange.DAY:
            rows = await self.session.execute(
                RAW_SERIES_SQL, {"container_id": container.id, "since": now - DAY}
            )
            points = [
                StatsPoint(
                    at=r[0],
                    cpu_avg=_round(r[1]),
                    cpu_max=_round(r[2]),
                    mem_avg=_round(r[3]),
                    mem_max=r[4],
                    mem_pct_max=_round(r[5]),
                    net_rx_avg=_round(r[6]),
                    net_tx_avg=_round(r[7]),
                )
                for r in rows
            ]
            return ContainerStats(range=range_, bucket_seconds=300, cpus=host.cpus, mem_limit_bytes=limit, points=points)

        days = {StatsRange.WEEK: 7, StatsRange.MONTH: 30, StatsRange.QUARTER: 90}[range_]
        width = HOUR if range_ is StatsRange.WEEK else DAY
        start = truncate(now, width) - width * (days * (24 if width == HOUR else 1) - 1)
        hourly = list(
            await self.session.scalars(
                select(DockerSampleHourly)
                .where(DockerSampleHourly.container_id == container.id, DockerSampleHourly.hour >= start)
                .order_by(DockerSampleHourly.hour)
            )
        )
        buckets: dict[datetime, list[DockerSampleHourly]] = {}
        for row in hourly:
            buckets.setdefault(truncate(row.hour, width), []).append(row)
        points = [_merge(at, rows) for at, rows in sorted(buckets.items())]
        return ContainerStats(
            range=range_,
            bucket_seconds=int(width.total_seconds()),
            cpus=host.cpus,
            mem_limit_bytes=limit,
            points=points,
        )


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _weighted(rows: list[DockerSampleHourly], field: str) -> float | None:
    pairs = [(getattr(r, field), r.samples) for r in rows if getattr(r, field) is not None]
    total = sum(n for _, n in pairs)
    return _round(sum(v * n for v, n in pairs) / total) if total else None


def _max(rows: list[DockerSampleHourly], field: str):
    values = [getattr(r, field) for r in rows if getattr(r, field) is not None]
    return max(values) if values else None


def _merge(at: datetime, rows: list[DockerSampleHourly]) -> StatsPoint:
    return StatsPoint(
        at=at,
        cpu_avg=_weighted(rows, "cpu_avg"),
        cpu_max=_round(_max(rows, "cpu_max")),
        mem_avg=_weighted(rows, "mem_avg"),
        mem_max=_max(rows, "mem_max"),
        mem_pct_max=_round(_max(rows, "mem_pct_max")),
        net_rx_avg=_weighted(rows, "net_rx_avg"),
        net_tx_avg=_weighted(rows, "net_tx_avg"),
    )


__all__ = ["DockerHistoryService", "DockerMonitor", "offline_after"]
