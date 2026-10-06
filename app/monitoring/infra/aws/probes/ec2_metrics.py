"""A server's CloudWatch metrics, against the check's thresholds: what runs
out on an EC2 instance before it stops answering.

- `high_cpu`: CPUUtilization above `cpu_percent_max`.
- `low_cpu_credits`: a burstable (T) instance's CPUCreditBalance below
  `cpu_credits_percent_min` of the most it can bank. In standard mode its CPU
  is then held to its baseline; in unlimited mode it bursts on surplus
  credits (CPUSurplusCreditBalance), which AWS bills unless they are earned
  back.
- `low_burst_balance`: a gp2, st1 or sc1 volume's BurstBalance, or the
  instance's own EBS burst (EBSIOBalance%, EBSByteBalance%, on Nitro sizes
  up to 2xlarge), below `burst_balance_percent_min`. At 0 the volume falls to
  its baseline.
- `iops_saturated`: an io1, io2 or gp3 volume's IOPS (VolumeReadOps plus
  VolumeWriteOps) above `iops_percent_max` of what it is provisioned with.
- `low_disk_space`: a filesystem's disk_used_percent above
  `disk_used_percent_max`. EC2 cannot see inside an instance, so this needs
  the CloudWatch agent publishing to `CWAgent` with the InstanceId
  dimension, as its default configuration does; found with `ListMetrics`.

Each a problem, never an outage: whether the server answers is `ping`'s,
`tcp`'s and `http`'s to say. Fails only when CloudWatch cannot be asked.

Read with one `GetMetricData` over the last WINDOW_MINUTES in PERIOD-second
buckets (basic monitoring and CPU credits come every 5 minutes). It is
billed per metric read: CPU, 2 more on a burstable instance, 2 for the
instance's EBS burst, 1 per gp2/st1/sc1 volume or 2 per io1/io2/gp3 one, and
1 per disk the agent reports.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.monitoring.infra.aws import client, limits
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.probes.base import ProbeResult, ProbeTarget, Trace, failed, ms_since, now

STEPS = ["aws_api", "metrics"]
WINDOW_MINUTES = 30
PERIOD = 300
#: How long the agent's disks, as `ListMetrics` lists them, are reused.
DISK_LIST_SECONDS = 3600
MAX_DISKS = 20
#: Instance sizes that burst to their EBS bandwidth, on Nitro, and so report
#: EBSIOBalance% and EBSByteBalance%.
EBS_BURST_SIZES = frozenset({"nano", "micro", "small", "medium", "large", "xlarge", "2xlarge"})
#: Filesystems the agent may report that are not disks.
NOT_DISKS = frozenset({"tmpfs", "devtmpfs", "overlay", "squashfs", "iso9660", "nsfs"})


@dataclass(slots=True)
class Query:
    id: str
    namespace: str
    metric: str
    dimensions: list[dict]
    stat: str = "Average"

    def as_api(self) -> dict:
        return {
            "Id": self.id,
            "MetricStat": {
                "Metric": {"Namespace": self.namespace, "MetricName": self.metric, "Dimensions": self.dimensions},
                "Period": PERIOD,
                "Stat": self.stat,
            },
            "ReturnData": True,
        }


@dataclass(slots=True)
class Disk:
    path: str
    device: str | None
    fstype: str | None
    #: As the agent publishes them: GetMetricData needs every one.
    dimensions: list[dict] = field(default_factory=list)


def _instance(ident: str) -> list[dict]:
    return [{"Name": "InstanceId", "Value": ident}]


def _volume(volume_id: str) -> list[dict]:
    return [{"Name": "VolumeId", "Value": volume_id}]


def plan(ident: str, detail: dict, disks: list[Disk]) -> list[Query]:
    """What to read for this instance, by its type and its volumes'."""
    instance_type = detail.get("instance_type")
    queries = [Query("cpu", "AWS/EC2", "CPUUtilization", _instance(ident))]
    if limits.is_burstable(instance_type):
        queries.append(Query("credits", "AWS/EC2", "CPUCreditBalance", _instance(ident)))
        queries.append(Query("surplus", "AWS/EC2", "CPUSurplusCreditBalance", _instance(ident)))
    if (instance_type or "").partition(".")[2] in EBS_BURST_SIZES:
        queries.append(Query("ebs_io", "AWS/EC2", "EBSIOBalance%", _instance(ident), "Minimum"))
        queries.append(Query("ebs_byte", "AWS/EC2", "EBSByteBalance%", _instance(ident), "Minimum"))
    for index, volume in enumerate(detail.get("volumes") or []):
        kind = volume.get("type")
        if limits.provisioned_iops(kind, volume.get("iops")):
            queries.append(Query(f"reads{index}", "AWS/EBS", "VolumeReadOps", _volume(volume["id"]), "Sum"))
            queries.append(Query(f"writes{index}", "AWS/EBS", "VolumeWriteOps", _volume(volume["id"]), "Sum"))
        elif kind in limits.BURSTING or kind is None:
            # Unknown when DescribeVolumes was refused: a volume that does
            # not burst just has no datapoint.
            queries.append(Query(f"burst{index}", "AWS/EBS", "BurstBalance", _volume(volume["id"]), "Minimum"))
    for index, disk in enumerate(disks):
        queries.append(Query(f"disk{index}", "CWAgent", "disk_used_percent", disk.dimensions))
    return queries


def window(at: datetime) -> tuple[datetime, datetime]:
    """WINDOW_MINUTES ending on a PERIOD boundary, so the latest bucket is a
    whole one and an IOPS sum is not read off a part of one."""
    end = at.replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % (PERIOD // 60))
    return end - timedelta(minutes=WINDOW_MINUTES), end


async def read_latest(region: client.Region, queries: list[Query]) -> dict[str, float]:
    """The latest datapoint of each query; one with none in the window is
    left out. Raises AwsError."""
    start, end = window(now())
    latest: dict[str, float] = {}
    for offset in range(0, len(queries), 500):
        response = await client.call(
            "cloudwatch",
            "get_metric_data",
            region,
            MetricDataQueries=[q.as_api() for q in queries[offset : offset + 500]],
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampDescending",
        )
        for row in response.get("MetricDataResults", []):
            values = row.get("Values") or []
            if values and row.get("Id"):
                latest[row["Id"]] = float(values[0])
    return latest


_disk_lists: dict[tuple, tuple[float, list[Disk], str | None]] = {}


async def agent_disks(region: client.Region, ident: str) -> tuple[list[Disk], str | None]:
    """The filesystems the CloudWatch agent reports for this instance, and a
    note when there are none to read."""
    key = (region.account, region.name, ident)
    hit = _disk_lists.get(key)
    if hit is not None and time.monotonic() - hit[0] < DISK_LIST_SECONDS:
        return hit[1], hit[2]
    disks: list[Disk] = []
    note = None
    try:
        rows = await client.paginate(
            "cloudwatch", "list_metrics", region, "Metrics",
            Namespace="CWAgent", MetricName="disk_used_percent",
            Dimensions=[{"Name": "InstanceId", "Value": ident}],
            RecentlyActive="PT3H",
        )
    except AwsError as exc:
        rows = []
        note = f"the CloudWatch agent's disks could not be listed ({exc.code})"
    for row in rows:
        dimensions = row.get("Dimensions") or []
        named = {d.get("Name"): d.get("Value") for d in dimensions}
        if not named.get("path") or named.get("fstype") in NOT_DISKS:
            continue
        disks.append(Disk(path=named["path"], device=named.get("device"), fstype=named.get("fstype"), dimensions=dimensions))
    disks.sort(key=lambda d: d.path)
    if not rows and note is None:
        note = "no CloudWatch agent disk metrics for it"
    if len(_disk_lists) > 4096:
        _disk_lists.clear()
    _disk_lists[key] = (time.monotonic(), disks[:MAX_DISKS], note)
    return disks[:MAX_DISKS], note


def credit_problem(credits: float | None, surplus: float | None, credit_max: float | None, percent_min) -> str | None:
    """Why a burstable instance (or RDS class) is short of CPU credits, if it
    is: spending surplus credits, or below `percent_min` of what it can bank."""
    if percent_min is None:
        return None
    if surplus:
        return (
            f"No CPU credits left: bursting on {surplus:.0f} surplus credits, which AWS bills "
            "unless they are earned back (unlimited mode)"
        )
    if credits is None or not credit_max:
        return None
    percent = credits * 100 / credit_max
    if percent < percent_min:
        return (
            f"{credits:.0f} of {credit_max:.0f} CPU credits left, {percent:.0f}% (limit {percent_min:g}%): "
            "at 0 the CPU is held to its baseline"
        )
    return None


@dataclass(slots=True)
class Reading:
    """What one run read, in the units shown."""

    cpu: float | None
    credits: float | None
    credit_max: float | None
    surplus: float | None
    ebs_io: float | None
    ebs_byte: float | None
    volumes: list[dict]
    disks: list[dict]

    @property
    def lowest_burst(self) -> float | None:
        values = [v["burst_balance_percent"] for v in self.volumes if v.get("burst_balance_percent") is not None]
        return min(values) if values else None


def read(figures: dict[str, float], detail: dict, disks: list[Disk]) -> Reading:
    volumes = []
    for index, volume in enumerate(detail.get("volumes") or []):
        limit = limits.provisioned_iops(volume.get("type"), volume.get("iops"))
        ops = [figures.get(f"reads{index}"), figures.get(f"writes{index}")]
        iops = sum(v for v in ops if v is not None) / PERIOD if any(v is not None for v in ops) else None
        volumes.append(
            {
                "id": volume["id"],
                "device": volume.get("device"),
                "type": volume.get("type"),
                "size_gb": volume.get("size_gb"),
                "burst_balance_percent": figures.get(f"burst{index}"),
                "iops": iops,
                "iops_limit": limit,
            }
        )
    return Reading(
        cpu=figures.get("cpu"),
        credits=figures.get("credits"),
        credit_max=limits.cpu_credit_max(detail.get("instance_type")),
        surplus=figures.get("surplus"),
        ebs_io=figures.get("ebs_io"),
        ebs_byte=figures.get("ebs_byte"),
        volumes=volumes,
        disks=[
            {"path": d.path, "device": d.device, "fstype": d.fstype, "used_percent": figures.get(f"disk{i}")}
            for i, d in enumerate(disks)
        ],
    )


def _volume_name(volume: dict) -> str:
    extra = ", ".join(filter(None, [volume.get("type"), volume.get("device")]))
    return f"{volume['id']} ({extra})" if extra else volume["id"]


def _disk_name(disk: dict) -> str:
    return f"{disk['path']} on {disk['device']}" if disk.get("device") else disk["path"]


def problems_of(reading: Reading, settings: dict) -> dict[str, str]:
    problems: dict[str, str] = {}
    cpu_max = settings.get("cpu_percent_max")
    if cpu_max is not None and reading.cpu is not None and reading.cpu > cpu_max:
        problems["high_cpu"] = f"CPU at {reading.cpu:.0f}% (limit {cpu_max:g}%)"

    if why := credit_problem(reading.credits, reading.surplus, reading.credit_max, settings.get("cpu_credits_percent_min")):
        problems["low_cpu_credits"] = why

    burst_min = settings.get("burst_balance_percent_min")
    if burst_min is not None:
        low = [
            f"{_volume_name(v)}: {v['burst_balance_percent']:.0f}% left"
            for v in reading.volumes
            if v["burst_balance_percent"] is not None and v["burst_balance_percent"] < burst_min
        ]
        for label, value in (("EBS IO balance", reading.ebs_io), ("EBS byte balance", reading.ebs_byte)):
            if value is not None and value < burst_min:
                low.append(f"instance {label}: {value:.0f}% left")
        if low:
            problems["low_burst_balance"] = (
                f"{'; '.join(low)} (limit {burst_min:g}%): at 0 it falls to its baseline"
            )

    iops_max = settings.get("iops_percent_max")
    if iops_max is not None:
        busy = []
        for v in reading.volumes:
            if v["iops"] is None or not v["iops_limit"]:
                continue
            percent = v["iops"] * 100 / v["iops_limit"]
            if percent > iops_max:
                busy.append(f"{_volume_name(v)}: {v['iops']:.0f} of {v['iops_limit']:.0f} IOPS, {percent:.0f}%")
        if busy:
            problems["iops_saturated"] = f"{'; '.join(busy)} (limit {iops_max:g}%)"

    disk_max = settings.get("disk_used_percent_max")
    if disk_max is not None:
        full = [
            f"{_disk_name(d)}: {d['used_percent']:.0f}% used"
            for d in reading.disks
            if d["used_percent"] is not None and d["used_percent"] > disk_max
        ]
        if full:
            problems["low_disk_space"] = f"{'; '.join(full)} (limit {disk_max:g}%)"
    return problems


def summary_of(reading: Reading) -> str:
    parts = []
    if reading.cpu is not None:
        parts.append(f"CPU {reading.cpu:.0f}%")
    if reading.surplus:
        parts.append(f"{reading.surplus:.0f} surplus credits")
    elif reading.credits is not None:
        parts.append(
            f"{reading.credits:.0f} of {reading.credit_max:.0f} credits" if reading.credit_max
            else f"{reading.credits:.0f} credits"
        )
    if reading.lowest_burst is not None:
        parts.append(f"burst {reading.lowest_burst:.0f}%")
    busiest = max(
        (v["iops"] * 100 / v["iops_limit"] for v in reading.volumes if v["iops"] is not None and v["iops_limit"]),
        default=None,
    )
    if busiest is not None:
        parts.append(f"IOPS {busiest:.0f}% of provisioned")
    used = [d for d in reading.disks if d["used_percent"] is not None]
    if used:
        fullest = max(used, key=lambda d: d["used_percent"])
        parts.append(f"{fullest['path']} {fullest['used_percent']:.0f}% used")
    return " · ".join(parts)


async def probe(target: ProbeTarget) -> ProbeResult:
    trace = Trace(STEPS)
    checked_at, started = now(), time.perf_counter()
    ident = target.aws_id
    detail = target.aws_detail or {}
    disks, disk_note = [], None
    if target.settings.get("disk_used_percent_max") is not None:
        disks, disk_note = await agent_disks(target.region, ident)
    try:
        figures = await read_latest(target.region, plan(ident, detail, disks))
    except AwsError as exc:
        trace.fail("aws_api", "aws_error", f"GetMetricData: {exc}", ms_since(started))
        return failed(trace, checked_at, started, "aws_error", f"{ident}: {exc}")
    api_ms = ms_since(started)
    trace.ok("aws_api", f"GetMetricData answered for {ident}", api_ms)

    reading = read(figures, detail, disks)
    snapshot = {
        "cpu_percent": reading.cpu,
        "cpu_credits": reading.credits,
        "cpu_credits_max": reading.credit_max,
        "surplus_credits": reading.surplus,
        "ebs_io_balance_percent": reading.ebs_io,
        "ebs_byte_balance_percent": reading.ebs_byte,
        "volumes": reading.volumes,
        "disks": reading.disks,
        "disk_note": disk_note,
    }
    if not figures:
        # Stopped, or too new to have reported.
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

    line = summary_of(reading)
    trace.ok("metrics", line)
    return ProbeResult(
        ok=True,
        checked_at=checked_at,
        summary=f"{ident}: {line}",
        response_time_ms=api_ms,
        detail={"metrics": {key: round(value, 3) for key, value in figures.items()}},
        snapshot=snapshot,
        problems=problems_of(reading, target.settings),
        metric=reading.cpu,
        steps=trace.steps,
    )
