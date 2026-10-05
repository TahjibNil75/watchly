"""A database's CloudWatch metrics, against the check's thresholds.

Read with one `GetMetricData` call over the last WINDOW_MINUTES, so no
connection goes into the VPC and no database credentials are needed. Each
threshold crossed by the latest datapoint is a problem (`high_cpu`,
`low_storage`, `low_memory`, `many_connections`, `replica_lag`), never an
outage: whether the database answers is `db_status`'s and `tcp`'s to say.
Fails only when CloudWatch cannot be asked.

RDS sends these every minute at no charge; reading them is billed per metric
(about 5 a run).
"""

import time
from datetime import timedelta

from app.monitoring.infra.aws import client
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.probes.base import ProbeResult, ProbeTarget, Trace, failed, ms_since, now

STEPS = ["aws_api", "metrics"]
#: How far back the latest datapoint is looked for.
WINDOW_MINUTES = 15
MB = 1024 * 1024
GB = 1024 * MB


def is_aurora(detail: dict) -> bool:
    return (detail.get("engine") or "").startswith("aurora")


def queries(ident: str, detail: dict) -> dict[str, tuple[str, float]]:
    """Query id -> (RDS metric, factor to the unit kept). Free storage is
    left out for Aurora, whose cluster volume grows by itself; replica lag is
    read for a replica only."""
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
    return wanted


def metric_query(query_id: str, metric: str, ident: str) -> dict:
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": "AWS/RDS",
                "MetricName": metric,
                "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": ident}],
            },
            "Period": 60,
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


def _problems(figures: dict[str, float], settings: dict, detail: dict) -> dict[str, str]:
    problems: dict[str, str] = {}
    cpu_max = settings.get("cpu_percent_max")
    if cpu_max is not None and "cpu" in figures and figures["cpu"] > cpu_max:
        problems["high_cpu"] = f"CPU at {figures['cpu']:.0f}% (limit {cpu_max:g}%)"
    storage_min = settings.get("free_storage_percent_min")
    allocated = detail.get("allocated_storage_gb")
    if storage_min is not None and "storage" in figures and allocated:
        percent = figures["storage"] * 100 / allocated
        if percent < storage_min:
            problems["low_storage"] = (
                f"{figures['storage']:.1f} GB free of {allocated} GB, {percent:.0f}% (limit {storage_min:g}%)"
            )
    memory_min = settings.get("freeable_memory_mb_min")
    if memory_min is not None and "memory" in figures and figures["memory"] < memory_min:
        problems["low_memory"] = f"{figures['memory']:.0f} MB freeable (limit {memory_min} MB)"
    connections_max = settings.get("connections_max")
    if connections_max is not None and "connections" in figures and figures["connections"] > connections_max:
        problems["many_connections"] = f"{figures['connections']:.0f} connections open (limit {connections_max})"
    lag_max = settings.get("replica_lag_seconds_max")
    if lag_max is not None and "lag" in figures and figures["lag"] > lag_max:
        problems["replica_lag"] = f"Replica {figures['lag']:.0f} s behind (limit {lag_max} s)"
    return problems


def summary_of(figures: dict[str, float]) -> str:
    parts = []
    if "cpu" in figures:
        parts.append(f"CPU {figures['cpu']:.0f}%")
    if "storage" in figures:
        parts.append(f"{figures['storage']:.1f} GB free")
    if "memory" in figures:
        parts.append(f"{figures['memory']:.0f} MB freeable")
    if "connections" in figures:
        parts.append(f"{figures['connections']:.0f} connections")
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
    api_ms = ms_since(started)
    trace.ok("aws_api", f"GetMetricData answered for {ident}", api_ms)

    snapshot = {
        "cpu_percent": figures.get("cpu"),
        "free_storage_gb": figures.get("storage"),
        "allocated_storage_gb": detail.get("allocated_storage_gb"),
        "freeable_memory_mb": figures.get("memory"),
        "connections": figures.get("connections"),
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

    problems = _problems(figures, target.settings, detail)
    line = summary_of(figures)
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
