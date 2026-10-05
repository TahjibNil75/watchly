"""An Auto Scaling group's own view of its instances.

Read with `DescribeAutoScalingGroups`, so no connection goes into the VPC. An
instance counts as healthy when it is `InService` and the group marks it
`Healthy`: by its EC2 status checks, or by its load balancer's health check
with the `ELB` health check type. Down when fewer than `min_healthy_instances`
are (never more than the desired capacity asks for). Degraded while an
instance in service is unhealthy (`instances_unhealthy`), or while the group
is short of its desired capacity, counting the instances still launching
(`capacity_short`); then its latest failed scaling activity says why.
"""

import time

from app.monitoring.infra.aws import client, discovery
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.probes.base import ProbeResult, ProbeTarget, Trace, failed, ms_since, now

STEPS = ["aws_api", "instances"]
#: Instance ids named in a problem, at most.
NAMED = 5


async def last_failed_activity(region: client.Region, name: str) -> str | None:
    """Why the group's latest scaling activity failed, if it did; e.g.
    "Launching a new EC2 instance. Status Reason: InsufficientInstanceCapacity"."""
    try:
        response = await client.call(
            "autoscaling", "describe_scaling_activities", region, AutoScalingGroupName=name, MaxRecords=5
        )
    except AwsError:
        return None
    for activity in response.get("Activities", []):
        if activity.get("StatusCode") in {"Failed", "Cancelled"}:
            text = activity.get("StatusMessage") or activity.get("Description") or activity.get("StatusCode")
            return str(text)[:300]
        if activity.get("StatusCode") == "Successful":
            return None
    return None


async def probe(target: ProbeTarget) -> ProbeResult:
    trace = Trace(STEPS)
    checked_at, started = now(), time.perf_counter()
    name = target.aws_id
    try:
        found = await discovery.read_group(target.region, name, with_addresses=False)
    except AwsError as exc:
        trace.fail("aws_api", "aws_error", f"DescribeAutoScalingGroups: {exc}", ms_since(started))
        return failed(trace, checked_at, started, "aws_error", f"{name}: {exc}")
    api_ms = ms_since(started)
    if found is None:
        message = f"AWS no longer has the Auto Scaling group {name}."
        trace.fail("aws_api", "no_instances", message, api_ms)
        return failed(trace, checked_at, started, "no_instances", message)
    trace.ok("aws_api", f"DescribeAutoScalingGroups answered for {name}", api_ms)

    group, members = found
    desired = group.get("DesiredCapacity") or 0
    in_service = [m for m in members if m["lifecycle_state"] == "InService"]
    healthy = [m for m in in_service if m["health_status"] == "Healthy"]
    unhealthy = [m for m in in_service if m["health_status"] != "Healthy"]
    launching = [m for m in members if (m["lifecycle_state"] or "").startswith("Pending")]
    minimum = min(target.settings.get("min_healthy_instances", 1), desired)
    counted = f"{len(healthy)} of {desired} desired instances healthy and in service"
    snapshot = {
        "healthy": len(healthy),
        "desired": desired,
        "in_service": len(in_service),
        "launching": len(launching),
        "min_size": group.get("MinSize"),
        "max_size": group.get("MaxSize"),
        "unhealthy": [m["id"] for m in unhealthy],
    }
    detail = {"instances": members, "health_check_type": group.get("HealthCheckType")}

    if len(healthy) < minimum:
        why = await last_failed_activity(target.region, name)
        message = f"{name}: {counted}, fewer than {minimum}" + (f". Last scaling activity failed: {why}" if why else "")
        trace.fail("instances", "instances_below_minimum", message)
        result = failed(
            trace, checked_at, started, "instances_below_minimum", message,
            detail=detail, snapshot=snapshot, metric=float(len(healthy)),
        )
        result.response_time_ms = api_ms
        return result

    problems = {}
    if unhealthy:
        ids = ", ".join(m["id"] for m in unhealthy[:NAMED])
        problems["instances_unhealthy"] = f"{ids} marked unhealthy by the group ({counted})"
    if len(healthy) + len(launching) < desired:
        why = await last_failed_activity(target.region, name)
        problems["capacity_short"] = f"{counted}" + (f". Last scaling activity failed: {why}" if why else "")
    note = counted + (f", {len(launching)} launching" if launching else "")
    trace.ok("instances", note)
    return ProbeResult(
        ok=True,
        checked_at=checked_at,
        summary=f"{name}: {note}",
        response_time_ms=api_ms,
        detail=detail,
        snapshot=snapshot,
        problems=problems,
        metric=float(len(healthy)),
        steps=trace.steps,
    )
