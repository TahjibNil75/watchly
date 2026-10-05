"""Infrastructure check history: hourly rollups, the stats read from them,
and retention of raw results; as `websites/history.py`, per check."""

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.monitoring.infra.aws.models import (
    NETWORK_CHECKS,
    AwsCheck,
    AwsCheckHourly,
    AwsCheckResult,
    InfraCheckType,
    ResourceKind,
)
from app.monitoring.infra.aws.schemas import CheckStats, CheckStatsBucket, ResourceStats
from app.monitoring.notifications.reports import retained_checks_since
from app.monitoring.websites.history import HOUR, RANGES, truncate
from app.monitoring.websites.schemas import StatsRange

#: Re-sums every hour from :since on; idempotent, like the websites' rollup.
ROLLUP_SQL = text("""
INSERT INTO aws_check_hourly
    (check_id, hour, checks, up_checks, timed_checks, sum_ms, max_ms, metric_min, metric_max)
SELECT check_id,
       date_trunc('hour', checked_at, 'UTC') AS hour,
       count(*),
       count(*) FILTER (WHERE ok),
       count(response_time_ms) FILTER (WHERE ok),
       coalesce(sum(response_time_ms) FILTER (WHERE ok), 0),
       max(response_time_ms) FILTER (WHERE ok),
       min(metric),
       max(metric)
FROM aws_check_results
WHERE checked_at >= :since
GROUP BY check_id, hour
ON CONFLICT (check_id, hour) DO UPDATE SET
    checks = EXCLUDED.checks,
    up_checks = EXCLUDED.up_checks,
    timed_checks = EXCLUDED.timed_checks,
    sum_ms = EXCLUDED.sum_ms,
    max_ms = EXCLUDED.max_ms,
    metric_min = EXCLUDED.metric_min,
    metric_max = EXCLUDED.metric_max
""")


def metric_name(check: AwsCheck, kind: ResourceKind | None = None) -> str | None:
    """What a check's `metric` measures. An Auto Scaling group's connecting
    checks count the instances that passed."""
    if kind is ResourceKind.AUTO_SCALING_GROUP and check.check_type in NETWORK_CHECKS:
        return "healthy_instances"
    match check.check_type:
        case InfraCheckType.PING:
            return "packet_loss_percent"
        case InfraCheckType.TARGET_HEALTH:
            return "healthy_targets"
        case InfraCheckType.GROUP_HEALTH:
            return "healthy_instances"
        case InfraCheckType.DB_METRICS:
            return "cpu_percent"
    return None


class _Tally:
    __slots__ = ("checks", "up_checks", "timed", "sum_ms", "max_ms", "metric_min", "metric_max")

    def __init__(self) -> None:
        self.checks = self.up_checks = self.timed = self.sum_ms = 0
        self.max_ms: int | None = None
        self.metric_min: float | None = None
        self.metric_max: float | None = None

    def add(self, row: AwsCheckHourly) -> None:
        self.checks += row.checks
        self.up_checks += row.up_checks
        self.timed += row.timed_checks
        self.sum_ms += row.sum_ms
        if row.max_ms is not None:
            self.max_ms = row.max_ms if self.max_ms is None else max(self.max_ms, row.max_ms)
        if row.metric_min is not None:
            self.metric_min = row.metric_min if self.metric_min is None else min(self.metric_min, row.metric_min)
        if row.metric_max is not None:
            self.metric_max = row.metric_max if self.metric_max is None else max(self.metric_max, row.metric_max)

    def figures(self) -> dict:
        return {
            "checks": self.checks,
            "up_checks": self.up_checks,
            "uptime_percent": round(100 * self.up_checks / self.checks, 3) if self.checks else None,
            "avg_response_ms": round(self.sum_ms / self.timed) if self.timed else None,
            "max_response_ms": self.max_ms,
            "metric_min": self.metric_min,
            "metric_max": self.metric_max,
        }


class InfraHistoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rolled_up_to(self) -> datetime | None:
        return await self.session.scalar(select(func.max(AwsCheckHourly.hour)))

    async def rollup(self, now: datetime | None = None) -> None:
        """Bring `aws_check_hourly` up to date. Called on every tick."""
        previous_hour = truncate(now or datetime.now(UTC), HOUR) - HOUR
        latest = await self._rolled_up_to()
        since = datetime(1970, 1, 1, tzinfo=UTC) if latest is None else min(latest, previous_hour)
        await self.session.execute(ROLLUP_SQL, {"since": since})
        await self.session.commit()

    async def purge(self, now: datetime | None = None, *, batch_size: int = 5_000, max_batches: int = 20) -> int:
        """Delete raw results older than CHECK_RETENTION_DAYS that are already
        rolled up, a committed batch at a time."""
        now = now or datetime.now(UTC)
        latest = await self._rolled_up_to()
        if latest is None:
            return 0
        cutoff = min(retained_checks_since(now), latest - HOUR)
        deleted = 0
        for _ in range(max_batches):
            batch = (
                select(AwsCheckResult.id)
                .where(AwsCheckResult.checked_at < cutoff)
                .limit(batch_size)
                .scalar_subquery()
            )
            result = await self.session.execute(delete(AwsCheckResult).where(AwsCheckResult.id.in_(batch)))
            await self.session.commit()
            deleted += result.rowcount or 0
            if (result.rowcount or 0) < batch_size:
                break
        return deleted

    async def stats(
        self,
        checks: list[AwsCheck],
        range_: StatsRange,
        now: datetime | None = None,
        *,
        kind: ResourceKind | None = None,
    ) -> ResourceStats:
        """Each check's figures over `range_`, whole and bucketed."""
        width, count = RANGES[range_]
        now = now or datetime.now(UTC)
        start = truncate(now, width) - width * (count - 1)
        ids = [check.id for check in checks]
        rows = await self.session.scalars(
            select(AwsCheckHourly)
            .where(AwsCheckHourly.check_id.in_(ids), AwsCheckHourly.hour >= start)
            .order_by(AwsCheckHourly.hour)
        ) if ids else []

        buckets = {check.id: [_Tally() for _ in range(count)] for check in checks}
        wholes = {check.id: _Tally() for check in checks}
        for row in rows:
            index = (truncate(row.hour, width) - start) // width
            if 0 <= index < count:
                buckets[row.check_id][index].add(row)
                wholes[row.check_id].add(row)

        return ResourceStats(
            range=range_,
            start=start,
            end=now,
            bucket_seconds=int(width.total_seconds()),
            checks=[
                CheckStats(
                    check_id=check.id,
                    check_type=check.check_type,
                    name=check.name,
                    metric=metric_name(check, kind),
                    series=[
                        CheckStatsBucket(start=start + width * index, **tally.figures())
                        for index, tally in enumerate(buckets[check.id])
                    ],
                    **wholes[check.id].figures(),
                )
                for check in checks
            ],
        )


__all__ = ["InfraHistoryService", "metric_name"]
