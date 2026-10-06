"""A database's CloudWatch metrics, against the check's thresholds.

Read with one `GetMetricData` call over the last WINDOW_MINUTES, so no
connection goes into the VPC and no database credentials are needed. Each
threshold crossed by the latest datapoint is a problem, never an outage:
whether the database answers is `db_status`'s and `tcp`'s to say. Fails only
when CloudWatch cannot be asked.

- `high_cpu`, `low_memory`, `many_connections`, `replica_lag`: CPU, freeable
  memory, connections and replica lag against fixed figures.
- `low_storage`: free storage below a share of what it may use. With storage
  autoscaling on, that is up to its maximum, not what is allocated now:
  autoscaling grows it before it fills.
- `storage_filling`: at the rate free storage has shrunk over the last
  TREND_DAYS, it runs out within `storage_full_days_min` days. Read hourly,
  with a second `GetMetricData`, and reused for TREND_CACHE_SECONDS.
- `connections_near_limit`: connections above a share of `max_connections`,
  as its parameter group sets it (see `limits.max_connections`) or as the
  check says.
- `iops_saturated`: ReadIOPS plus WriteIOPS above a share of the IOPS it is
  provisioned with (io1, io2, gp3).
- `low_burst_balance`: a gp2 database's BurstBalance; at 0 it falls to its
  baseline of 3 IOPS a GB.
- `low_cpu_credits`: a burstable (`db.t*`) class's CPU credits.

Free storage, its trend, IOPS and burst balance are left out for Aurora,
whose cluster volume grows by itself.

RDS sends these every minute at no charge; reading them is billed per metric
(5 to 10 a run, and one more an hour for the trend).
"""

import time
from datetime import datetime, timedelta

from app.monitoring.infra.aws import client, limits
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.probes.base import ProbeResult, ProbeTarget, Trace, failed, ms_since, now
from app.monitoring.infra.aws.probes.ec2_metrics import credit_problem

STEPS = ["aws_api", "metrics"]
#: How far back the latest datapoint is looked for.
WINDOW_MINUTES = 15
MB = 1024 * 1024
GB = 1024 * MB
#: How much free-storage history the forecast is fitted to, in hourly
#: averages, and the least of it that makes one.
TREND_DAYS = 7
MIN_TREND_HOURS = 12
TREND_CACHE_SECONDS = 3600
#: A rise in free storage larger than this share of the allocated storage
#: (and at least 1 GB) within an hour is storage added, or a large cleanup:
#: the trend starts again after it.
STEP_UP_SHARE = 0.02


def is_aurora(detail: dict) -> bool:
    return (detail.get("engine") or "").startswith("aurora")


def queries(ident: str, detail: dict) -> dict[str, tuple[str, float]]:
    """Query id -> (RDS metric, factor to the unit kept). Free storage, IOPS
    and burst balance are left out for Aurora, whose cluster volume grows by
    itself; replica lag is read for a replica only; IOPS where they are
    provisioned, burst balance on gp2, CPU credits on a burstable class."""
    wanted = {
        "cpu": ("CPUUtilization", 1.0),
        "memory": ("FreeableMemory", 1 / MB),
        "connections": ("DatabaseConnections", 1.0),
    }
    if is_aurora(detail):
        if detail.get("cluster"):
            # Milliseconds; a writer has none.
            wanted["lag"] = ("AuroraReplicaLag", 1 / 1000)
    else:
        wanted["storage"] = ("FreeStorageSpace", 1 / GB)
        if detail.get("replica_of"):
            wanted["lag"] = ("ReplicaLag", 1.0)
        if limits.provisioned_iops(detail.get("storage_type"), detail.get("iops")):
            wanted["read_iops"] = ("ReadIOPS", 1.0)
            wanted["write_iops"] = ("WriteIOPS", 1.0)
        if detail.get("storage_type") == "gp2":
            wanted["burst"] = ("BurstBalance", 1.0)
    if limits.is_burstable(detail.get("instance_class")):
        wanted["credits"] = ("CPUCreditBalance", 1.0)
        wanted["surplus"] = ("CPUSurplusCreditBalance", 1.0)
    return wanted


def metric_query(query_id: str, metric: str, ident: str, period: int = 60) -> dict:
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": "AWS/RDS",
                "MetricName": metric,
                "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": ident}],
            },
            "Period": period,
            "Stat": "Average",
        },
        "ReturnData": True,
    }


async def read_latest(region: client.Region, ident: str, wanted: dict[str, tuple[str, float]]) -> dict[str, float]:
    """The latest datapoint of each query, in the unit kept; a metric with no
    datapoint in the window is left out. Raises AwsError."""
    end = now()
    response = await client.call(
        "cloudwatch",
        "get_metric_data",
        region,
        MetricDataQueries=[metric_query(query_id, metric, ident) for query_id, (metric, _) in wanted.items()],
        StartTime=end - timedelta(minutes=WINDOW_MINUTES),
        EndTime=end,
        ScanBy="TimestampDescending",
    )
    latest: dict[str, float] = {}
    for row in response.get("MetricDataResults", []):
        values = row.get("Values") or []
        if values and row.get("Id") in wanted:
            latest[row["Id"]] = float(values[0]) * wanted[row["Id"]][1]
    return latest


# --- the storage trend ----------------------------------------------------------


def storage_trend(points: list[tuple[datetime, float]], allocated_gb: float | None) -> tuple[float, float] | None:
    """(GB a day free storage shrinks by, hours of history that says so),
    fitted by least squares to the hours since its last step up; None while
    it is not shrinking, or with under MIN_TREND_HOURS to go on."""
    points = sorted(points)
    step = max(1.0, (allocated_gb or 0) * STEP_UP_SHARE)
    start = 0
    for index in range(1, len(points)):
        if points[index][1] - points[index - 1][1] > step:
            start = index
    run = points[start:]
    if len(run) < 3:
        return None
    hours = (run[-1][0] - run[0][0]).total_seconds() / 3600
    if hours < MIN_TREND_HOURS:
        return None
    xs = [(moment - run[0][0]).total_seconds() / 86400 for moment, _ in run]
    ys = [value for _, value in run]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    if spread == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / spread
    if slope >= 0:
        return None
    return -slope, hours


_trends: dict[tuple, tuple[float, tuple[float, float] | None]] = {}


async def read_trend(region: client.Region, ident: str, allocated_gb: float | None) -> tuple[float, float] | None:
    """`storage_trend` of the last TREND_DAYS of hourly free storage, reused
    for TREND_CACHE_SECONDS. Raises AwsError."""
    key = (region.account, region.name, ident)
    hit = _trends.get(key)
    if hit is not None and time.monotonic() - hit[0] < TREND_CACHE_SECONDS:
        return hit[1]
    end = now().replace(minute=0, second=0, microsecond=0)
    response = await client.call(
        "cloudwatch",
        "get_metric_data",
        region,
        MetricDataQueries=[metric_query("free", "FreeStorageSpace", ident, period=3600)],
        StartTime=end - timedelta(days=TREND_DAYS),
        EndTime=end,
        ScanBy="TimestampAscending",
    )
    points: list[tuple[datetime, float]] = []
    for row in response.get("MetricDataResults", []):
        for moment, value in zip(row.get("Timestamps") or [], row.get("Values") or [], strict=False):
            points.append((moment, float(value) / GB))
    trend = storage_trend(points, allocated_gb)
    if len(_trends) > 4096:
        _trends.clear()
    _trends[key] = (time.monotonic(), trend)
    return trend


def days_text(days: float) -> str:
    if days < 1:
        return "less than a day"
    whole = round(days)
    return f"about {whole} day" + ("" if whole == 1 else "s")


def span_text(hours: float) -> str:
    return f"{hours / 24:.0f} days" if hours >= 48 else f"{hours:.0f} hours"


# --- problems -------------------------------------------------------------------


def storage_room(figures: dict[str, float], detail: dict) -> tuple[float, float, str] | None:
    """(GB it may still fill, GB it may hold in all, what that is) once
    storage autoscaling is counted in; None without free storage."""
    if "storage" not in figures:
        return None
    allocated = detail.get("allocated_storage_gb")
    ceiling = detail.get("max_allocated_storage_gb")
    free = figures["storage"]
    if allocated and ceiling and ceiling > allocated:
        return free + ceiling - allocated, ceiling, f"{free:.1f} GB free of {allocated} GB, autoscaling up to {ceiling} GB"
    if allocated:
        return free, allocated, f"{free:.1f} GB free of {allocated} GB"
    return None


def connection_limit(settings: dict, detail: dict) -> tuple[int | None, bool]:
    """(max_connections, whether it is an estimate): the check's own, else
    the one worked out from its parameter group."""
    if settings.get("max_connections"):
        return int(settings["max_connections"]), False
    limit = detail.get("max_connections")
    return (int(limit) if limit else None), (detail.get("max_connections_note") or "").startswith("about")


def _problems(
    figures: dict[str, float], settings: dict, detail: dict, trend: tuple[float, float] | None
) -> dict[str, str]:
    problems: dict[str, str] = {}
    cpu_max = settings.get("cpu_percent_max")
    if cpu_max is not None and "cpu" in figures and figures["cpu"] > cpu_max:
        problems["high_cpu"] = f"CPU at {figures['cpu']:.0f}% (limit {cpu_max:g}%)"

    room = storage_room(figures, detail)
    storage_min = settings.get("free_storage_percent_min")
    if storage_min is not None and room is not None:
        left, total, text = room
        percent = left * 100 / total
        if percent < storage_min:
            problems["low_storage"] = f"{text}, {percent:.0f}% left (limit {storage_min:g}%)"

    days_min = settings.get("storage_full_days_min")
    if days_min is not None and room is not None and trend is not None:
        rate, hours = trend
        days = room[0] / rate
        if days < days_min:
            problems["storage_filling"] = (
                f"Full in {days_text(days)} at the last {span_text(hours)}' rate, {rate:.1f} GB a day: "
                f"{room[2]} (limit {days_min:g} days)"
            )

    memory_min = settings.get("freeable_memory_mb_min")
    if memory_min is not None and "memory" in figures and figures["memory"] < memory_min:
        problems["low_memory"] = f"{figures['memory']:.0f} MB freeable (limit {memory_min} MB)"

    connections_max = settings.get("connections_max")
    if connections_max is not None and "connections" in figures and figures["connections"] > connections_max:
        problems["many_connections"] = f"{figures['connections']:.0f} connections open (limit {connections_max})"
    share_max = settings.get("connections_percent_max")
    limit, estimated = connection_limit(settings, detail)
    if share_max is not None and limit and "connections" in figures:
        percent = figures["connections"] * 100 / limit
        if percent > share_max:
            problems["connections_near_limit"] = (
                f"{figures['connections']:.0f} of {'about ' if estimated else ''}{limit} connections, "
                f"{percent:.0f}% (limit {share_max:g}%)"
            )

    iops_max = settings.get("iops_percent_max")
    provisioned = limits.provisioned_iops(detail.get("storage_type"), detail.get("iops"))
    if iops_max is not None and provisioned and ("read_iops" in figures or "write_iops" in figures):
        iops = figures.get("read_iops", 0.0) + figures.get("write_iops", 0.0)
        percent = iops * 100 / provisioned
        if percent > iops_max:
            problems["iops_saturated"] = (
                f"{iops:.0f} of {provisioned:.0f} provisioned IOPS, {percent:.0f}% (limit {iops_max:g}%)"
            )

    burst_min = settings.get("burst_balance_percent_min")
    if burst_min is not None and "burst" in figures and figures["burst"] < burst_min:
        baseline = max(100, 3 * (detail.get("allocated_storage_gb") or 0))
        problems["low_burst_balance"] = (
            f"{figures['burst']:.0f}% burst balance left (limit {burst_min:g}%): "
            f"at 0 it falls to its baseline of {baseline} IOPS"
        )

    if why := credit_problem(
        figures.get("credits"),
        figures.get("surplus"),
        limits.cpu_credit_max(detail.get("instance_class")),
        settings.get("cpu_credits_percent_min"),
    ):
        problems["low_cpu_credits"] = why

    lag_max = settings.get("replica_lag_seconds_max")
    if lag_max is not None and "lag" in figures and figures["lag"] > lag_max:
        problems["replica_lag"] = f"Replica {figures['lag']:.0f} s behind (limit {lag_max} s)"
    return problems


def summary_of(figures: dict[str, float], trend: tuple[float, float] | None = None) -> str:
    parts = []
    if "cpu" in figures:
        parts.append(f"CPU {figures['cpu']:.0f}%")
    if "storage" in figures:
        parts.append(f"{figures['storage']:.1f} GB free")
    if trend is not None:
        parts.append(f"−{trend[0]:.1f} GB a day")
    if "memory" in figures:
        parts.append(f"{figures['memory']:.0f} MB freeable")
    if "connections" in figures:
        parts.append(f"{figures['connections']:.0f} connections")
    if "read_iops" in figures or "write_iops" in figures:
        parts.append(f"{figures.get('read_iops', 0) + figures.get('write_iops', 0):.0f} IOPS")
    if "burst" in figures:
        parts.append(f"burst {figures['burst']:.0f}%")
    if figures.get("surplus"):
        parts.append(f"{figures['surplus']:.0f} surplus credits")
    elif "credits" in figures:
        parts.append(f"{figures['credits']:.0f} CPU credits")
    if "lag" in figures:
        parts.append(f"lag {figures['lag']:.0f} s")
    return " · ".join(parts)


async def probe(target: ProbeTarget) -> ProbeResult:
    trace = Trace(STEPS)
    checked_at, started = now(), time.perf_counter()
    ident = target.aws_id
    detail = target.aws_detail or {}
    wanted = queries(ident, detail)
    try:
        figures = await read_latest(target.region, ident, wanted)
    except AwsError as exc:
        trace.fail("aws_api", "aws_error", f"GetMetricData: {exc}", ms_since(started))
        return failed(trace, checked_at, started, "aws_error", f"{ident}: {exc}")
    trend = None
    if "storage" in figures and target.settings.get("storage_full_days_min") is not None:
        try:
            trend = await read_trend(target.region, ident, detail.get("allocated_storage_gb"))
        except AwsError:
            # The latest figures still count; only the forecast is missing.
            trend = None
    api_ms = ms_since(started)
    trace.ok("aws_api", f"GetMetricData answered for {ident}", api_ms)

    limit, estimated = connection_limit(target.settings, detail)
    iops = (
        figures.get("read_iops", 0.0) + figures.get("write_iops", 0.0)
        if "read_iops" in figures or "write_iops" in figures
        else None
    )
    room = storage_room(figures, detail)
    snapshot = {
        "cpu_percent": figures.get("cpu"),
        "free_storage_gb": figures.get("storage"),
        "allocated_storage_gb": detail.get("allocated_storage_gb"),
        "max_allocated_storage_gb": detail.get("max_allocated_storage_gb"),
        "storage_shrink_gb_per_day": trend[0] if trend else None,
        "storage_full_in_days": room[0] / trend[0] if trend and room else None,
        "freeable_memory_mb": figures.get("memory"),
        "connections": figures.get("connections"),
        "max_connections": limit,
        "max_connections_estimated": estimated,
        "iops": iops,
        "iops_limit": limits.provisioned_iops(detail.get("storage_type"), detail.get("iops")),
        "burst_balance_percent": figures.get("burst"),
        "cpu_credits": figures.get("credits"),
        "cpu_credits_max": limits.cpu_credit_max(detail.get("instance_class")),
        "surplus_credits": figures.get("surplus"),
        "replica_lag_seconds": figures.get("lag"),
    }
    if not figures:
        # Stopped, or too new to have reported: db_status says which.
        note = f"no CloudWatch datapoint in the last {WINDOW_MINUTES} minutes"
        trace.ok("metrics", note[0].upper() + note[1:])
        return ProbeResult(
            ok=True,
            checked_at=checked_at,
            summary=f"{ident}: {note}",
            response_time_ms=api_ms,
            snapshot=snapshot,
            steps=trace.steps,
        )

    problems = _problems(figures, target.settings, detail, trend)
    line = summary_of(figures, trend)
    trace.ok("metrics", line)
    return ProbeResult(
        ok=True,
        checked_at=checked_at,
        summary=f"{ident}: {line}",
        response_time_ms=api_ms,
        detail={"metrics": {key: round(value, 3) for key, value in figures.items()}},
        snapshot=snapshot,
        problems=problems,
        metric=figures.get("cpu"),
        steps=trace.steps,
    )
