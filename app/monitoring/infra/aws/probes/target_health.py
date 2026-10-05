"""What the load balancer's own health check says about each target of a
target group.

Read with `DescribeTargetHealth`, so no connection goes into the VPC. Down
when fewer than `min_healthy_targets` targets are healthy; degraded
(`targets_unhealthy`) while any registered target is unhealthy. Targets still
registering (`initial`) or leaving (`draining`) count as neither.
"""

import time

from app.monitoring.infra.aws import client
from app.monitoring.infra.aws.probes.base import ProbeResult, ProbeTarget, Trace, failed, ms_since, now

STEPS = ["aws_api", "targets"]
#: States that mean the load balancer's health check is failing for the target.
FAILING = frozenset({"unhealthy", "unavailable"})


def target_group_name(arn: str) -> str:
    """`.../targetgroup/orders-api-tg/73e2d6bc24d8a067` -> `orders-api-tg`."""
    parts = arn.split("targetgroup/", 1)
    return parts[1].split("/", 1)[0] if len(parts) == 2 else arn


async def probe(target: ProbeTarget) -> ProbeResult:
    trace = Trace(STEPS)
    checked_at, started = now(), time.perf_counter()
    arn = target.settings["target_group_arn"]
    name = target_group_name(arn)
    try:
        response = await client.call(
            "elbv2", "describe_target_health", target.region, TargetGroupArn=arn
        )
    except client.AwsError as exc:
        trace.fail("aws_api", "aws_error", f"DescribeTargetHealth: {exc}", ms_since(started))
        return failed(trace, checked_at, started, "aws_error", f"{name}: {exc}")
    api_ms = ms_since(started)
    trace.ok("aws_api", f"DescribeTargetHealth answered for {name}", api_ms)

    targets = []
    for description in response.get("TargetHealthDescriptions", []):
        spec = description.get("Target", {})
        health = description.get("TargetHealth", {})
        targets.append(
            {
                "id": spec.get("Id"),
                "port": spec.get("Port"),
                "az": spec.get("AvailabilityZone"),
                "state": health.get("State", "unknown"),
                "reason": health.get("Reason"),
                "description": health.get("Description"),
            }
        )
    healthy = sum(1 for t in targets if t["state"] == "healthy")
    failing = [t for t in targets if t["state"] in FAILING]
    minimum = target.settings.get("min_healthy_targets", 1)
    snapshot = {
        "target_group": name,
        "healthy": healthy,
        "total": len(targets),
        "unhealthy": [t["id"] for t in failing],
    }
    detail = {"targets": targets, "target_group_arn": arn}
    counted = f"{healthy} of {len(targets)} targets healthy"

    if healthy < minimum:
        reasons = "; ".join(
            f"{t['id']} {t['reason'] or t['state']}" for t in failing[:5]
        )
        message = f"{name}: {counted}, fewer than {minimum}" + (f" ({reasons})" if reasons else "")
        trace.fail("targets", "targets_below_minimum", message)
        result = failed(
            trace, checked_at, started, "targets_below_minimum", message,
            detail=detail, snapshot=snapshot, metric=float(healthy),
        )
        result.response_time_ms = api_ms
        return result

    problems = {}
    if failing:
        first = failing[0]
        problems["targets_unhealthy"] = (
            f"{name}: {counted}; {first['id']} failing with "
            f"{first['reason'] or first['state']}"
            + (f" ({first['description']})" if first.get("description") else "")
        )
    trace.ok("targets", counted)
    return ProbeResult(
        ok=True,
        checked_at=checked_at,
        summary=f"{name}: {counted}",
        response_time_ms=api_ms,
        detail=detail,
        snapshot=snapshot,
        problems=problems,
        metric=float(healthy),
        steps=trace.steps,
    )
