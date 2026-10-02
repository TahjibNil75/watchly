"""Check history over time: hourly rollups, the stats read from them, and
retention of the raw checks.

`website_checks` gains a row per site per check — about 105k a year at the
default interval — so raw checks are purged after CHECK_RETENTION_DAYS. Before
that, every tick sums them into `website_check_hourly`, one row per site per
UTC hour, which is kept and is what charts and long-range uptime read.

Figures are defined as in the monthly report (see `notifications.reports`):
uptime is the share of checks that succeeded, and response times count
successful checks only.
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.monitoring.notifications.reports import retained_checks_since
from app.monitoring.websites.models import RESPONSE_BUCKETS_MS, WebsiteCheckHourly
from app.monitoring.websites.schemas import StatsBucket, StatsRange, WebsiteStats
from app.monitoring.websites.service import WebsiteService

#: The steps of an HTTP check that are timed, as `website_checks` columns
#: without the `_ms`.
STEPS = ("dns", "connect", "tls", "first_byte")

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

#: Each range as (bucket width, number of buckets). The last bucket is the
#: current, still-filling hour or day.
RANGES: dict[StatsRange, tuple[timedelta, int]] = {
    StatsRange.DAY: (HOUR, 24),
    StatsRange.WEEK: (HOUR, 7 * 24),
    StatsRange.MONTH: (DAY, 30),
    StatsRange.QUARTER: (DAY, 90),
}

# One `count(*) FILTER` per bucket, as width_bucket numbers them (from 1).
_HISTOGRAM = ", ".join(
    f"count(*) FILTER (WHERE bucket = {n})"
    for n in range(1, len(RESPONSE_BUCKETS_MS) + 1)
)

_STEP_COLUMNS = ", ".join(f"sum_{s}_ms, n_{s}" for s in STEPS)
_STEP_INPUTS = ", ".join(f"CASE WHEN is_up THEN {s}_ms END AS {s}" for s in STEPS)
_STEP_SUMS = ", ".join(f"coalesce(sum({s}), 0), count({s})" for s in STEPS)
_STEP_UPDATES = ", ".join(
    f"sum_{s}_ms = EXCLUDED.sum_{s}_ms, n_{s} = EXCLUDED.n_{s}" for s in STEPS
)

#: Re-sums every hour from :since on. Idempotent, so an hour can be rolled up
#: again as late checks land in it. Migration 0017 ran this statement to
#: backfill, from a copy of its own.
ROLLUP_SQL = text(f"""
INSERT INTO website_check_hourly
    (website_id, hour, checks, up_checks, timed_checks, sum_ms, max_ms, histogram,
     packets_sent, packets_received,
     {_STEP_COLUMNS})
SELECT website_id,
       hour,
       count(*),
       count(*) FILTER (WHERE is_up),
       count(bucket),
       coalesce(sum(response_time_ms) FILTER (WHERE bucket IS NOT NULL), 0),
       max(response_time_ms) FILTER (WHERE bucket IS NOT NULL),
       ARRAY[{_HISTOGRAM}],
       coalesce(sum(packets_sent), 0),
       coalesce(sum(packets_received), 0),
       {_STEP_SUMS}
FROM (
    SELECT website_id,
           date_trunc('hour', checked_at, 'UTC') AS hour,
           is_up,
           response_time_ms,
           CASE WHEN is_up AND response_time_ms IS NOT NULL
                THEN width_bucket(greatest(response_time_ms, 0), CAST(:bounds AS integer[]))
           END AS bucket,
           packets_sent,
           packets_received,
           {_STEP_INPUTS}
    FROM website_checks
    WHERE checked_at >= :since
) AS c
GROUP BY website_id, hour
ON CONFLICT (website_id, hour) DO UPDATE SET
    checks = EXCLUDED.checks,
    up_checks = EXCLUDED.up_checks,
    timed_checks = EXCLUDED.timed_checks,
    sum_ms = EXCLUDED.sum_ms,
    max_ms = EXCLUDED.max_ms,
    histogram = EXCLUDED.histogram,
    packets_sent = EXCLUDED.packets_sent,
    packets_received = EXCLUDED.packets_received,
    {_STEP_UPDATES}
""")


def truncate(moment: datetime, width: timedelta) -> datetime:
    """Start of the UTC hour or day holding `moment`."""
    moment = moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return moment.replace(hour=0) if width == DAY else moment


def percentile_ms(histogram: list[int], max_ms: int | None, q: float = 0.95) -> int | None:
    """The q-th percentile of the checks counted in `histogram`.

    Nearest-rank, like the monthly report, then placed within its bucket by
    linear interpolation. `max_ms` caps the open-ended last bucket and keeps the
    answer from exceeding the slowest check actually seen.
    """
    total = sum(histogram)
    if not total:
        return None
    rank = max(math.ceil(total * q), 1)
    seen = 0
    for index, count in enumerate(histogram):
        if seen + count >= rank:
            low = RESPONSE_BUCKETS_MS[index]
            high = (
                RESPONSE_BUCKETS_MS[index + 1]
                if index + 1 < len(RESPONSE_BUCKETS_MS)
                else (max_ms or low)
            )
            if max_ms is not None:
                high = min(high, max_ms)
            high = max(high, low)
            return round(low + (high - low) * (rank - seen) / count)
        seen += count
    return None  # unreachable: the counts add up to total


@dataclass(slots=True)
class Tally:
    """Hourly rows added together."""

    checks: int = 0
    up_checks: int = 0
    timed_checks: int = 0
    sum_ms: int = 0
    max_ms: int | None = None
    histogram: list[int] = field(default_factory=lambda: [0] * len(RESPONSE_BUCKETS_MS))
    packets_sent: int = 0
    packets_received: int = 0
    #: Per step: milliseconds spent, and checks that performed it.
    step_ms: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STEPS, 0))
    step_n: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STEPS, 0))

    def add(self, row: WebsiteCheckHourly) -> None:
        self.checks += row.checks
        self.up_checks += row.up_checks
        self.timed_checks += row.timed_checks
        self.sum_ms += row.sum_ms
        self.packets_sent += row.packets_sent
        self.packets_received += row.packets_received
        for step in STEPS:
            self.step_ms[step] += getattr(row, f"sum_{step}_ms")
            self.step_n[step] += getattr(row, f"n_{step}")
        if row.max_ms is not None:
            self.max_ms = row.max_ms if self.max_ms is None else max(self.max_ms, row.max_ms)
        for index, count in enumerate(row.histogram[: len(self.histogram)]):
            self.histogram[index] += count

    def figures(self) -> dict:
        return {
            "checks": self.checks,
            "up_checks": self.up_checks,
            "uptime_percent": (
                round(100 * self.up_checks / self.checks, 3) if self.checks else None
            ),
            "avg_response_ms": (
                round(self.sum_ms / self.timed_checks) if self.timed_checks else None
            ),
            "p50_response_ms": percentile_ms(self.histogram, self.max_ms, 0.5),
            "p95_response_ms": percentile_ms(self.histogram, self.max_ms),
            "p99_response_ms": percentile_ms(self.histogram, self.max_ms, 0.99),
            "max_response_ms": self.max_ms,
            **{
                f"avg_{step}_ms": (
                    round(self.step_ms[step] / self.step_n[step]) if self.step_n[step] else None
                )
                for step in STEPS
            },
            "packet_loss_percent": (
                round(100 * (self.packets_sent - self.packets_received) / self.packets_sent, 3)
                if self.packets_sent
                else None
            ),
        }


class HistoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rolled_up_to(self) -> datetime | None:
        """The newest hour with a rollup row, if any."""
        return await self.session.scalar(select(func.max(WebsiteCheckHourly.hour)))

    async def rollup(self, now: datetime | None = None) -> None:
        """Bring `website_check_hourly` up to date. Called on every tick.

        Re-sums from the newest hour already rolled up, and never later than
        the previous hour, so a check committed just after its hour was summed
        is still counted.
        """
        previous_hour = truncate(now or datetime.now(UTC), HOUR) - HOUR
        latest = await self._rolled_up_to()
        # Nothing rolled up yet: take all history. Migration 0017 normally
        # did that already.
        since = datetime(1970, 1, 1, tzinfo=UTC) if latest is None else min(latest, previous_hour)
        await self.session.execute(
            ROLLUP_SQL, {"since": since, "bounds": list(RESPONSE_BUCKETS_MS)}
        )
        await self.session.commit()

    async def purge(self, now: datetime | None = None) -> int:
        """Delete raw checks older than CHECK_RETENTION_DAYS. Called on every
        tick, after `rollup`; returns how many were deleted.

        Never deletes a check that is not yet in an hourly row, nor one the
        monthly report may still read — whatever CHECK_RETENTION_DAYS says.
        """
        now = now or datetime.now(UTC)
        latest = await self._rolled_up_to()
        if latest is None:
            return 0  # nothing rolled up: every check is still the only copy
        # `rollup` re-reads the hour before `latest`; leave it alone.
        cutoff = min(retained_checks_since(now), latest - HOUR)
        return await WebsiteService(self.session).purge_old_checks(cutoff)

    async def stats(
        self, website_id: int, range_: StatsRange, now: datetime | None = None
    ) -> WebsiteStats:
        """One site's figures over `range_`, whole and bucketed."""
        width, count = RANGES[range_]
        now = now or datetime.now(UTC)
        start = truncate(now, width) - width * (count - 1)
        rows = await self.session.scalars(
            select(WebsiteCheckHourly)
            .where(
                WebsiteCheckHourly.website_id == website_id,
                WebsiteCheckHourly.hour >= start,
            )
            .order_by(WebsiteCheckHourly.hour)
        )

        buckets = [Tally() for _ in range(count)]
        whole = Tally()
        for row in rows:
            index = (truncate(row.hour, width) - start) // width
            if 0 <= index < count:
                buckets[index].add(row)
                whole.add(row)

        return WebsiteStats(
            range=range_,
            start=start,
            end=now,
            bucket_seconds=int(width.total_seconds()),
            series=[
                StatsBucket(start=start + width * index, **bucket.figures())
                for index, bucket in enumerate(buckets)
            ],
            **whole.figures(),
        )
