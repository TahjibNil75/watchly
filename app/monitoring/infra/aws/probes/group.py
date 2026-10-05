"""A `ping`, `tcp` or `http` check of an Auto Scaling group: the same probe,
run on every instance the group has in service.

The instances are read from AWS at each run, since the group replaces them:
one removed by a scale-in is never probed, and one launched since the last
sync is. An instance still launching, or inside the group's health-check grace
period, is left out, as the group leaves it out of its own health checks. The
check is down when fewer than `min_healthy_instances` pass (when none does, by
default), and `instances_failing` is open while any fails.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta

from app.monitoring.infra.aws import discovery
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.models import ResourceKind
from app.monitoring.infra.aws.probes.base import (
    ProbeResult,
    ProbeTarget,
    Step,
    Trace,
    failed,
    ms_since,
    now,
)

#: Instances probed at once.
CONCURRENCY = 10
#: Instances probed per run, at most.
MAX_INSTANCES = 50


def warming(member: dict, grace_seconds: int, at: datetime) -> bool:
    """Still launching, or in service for less than the grace period."""
    state = member.get("lifecycle_state") or ""
    if state.startswith("Pending"):
        return True
    if state != "InService" or not grace_seconds or not member.get("launch_time"):
        return False
    try:
        launched = datetime.fromisoformat(member["launch_time"])
    except ValueError:
        return False
    return at - launched < timedelta(seconds=grace_seconds)


async def probe(target: ProbeTarget, inner: Callable[[ProbeTarget], Awaitable[ProbeResult]]) -> ProbeResult:
    trace = Trace(["aws_api"])
    checked_at, started = now(), time.perf_counter()
    name = target.aws_id
    try:
        found = await discovery.read_group(target.region, name, with_addresses=True)
    except AwsError as exc:
        trace.fail("aws_api", "aws_error", f"DescribeAutoScalingGroups: {exc}", ms_since(started))
        return failed(trace, checked_at, started, "aws_error", f"{name}: {exc}")
    if found is None:
        message = f"AWS no longer has the Auto Scaling group {name}."
        trace.fail("aws_api", "no_instances", message, ms_since(started))
        return failed(trace, checked_at, started, "no_instances", message)

    group, members = found
    desired = group.get("DesiredCapacity") or 0
    grace = group.get("HealthCheckGracePeriod") or 0
    starting = [m for m in members if warming(m, grace, checked_at)]
    ready = [m for m in members if m["lifecycle_state"] == "InService" and m not in starting]
    ready = ready[:MAX_INSTANCES]
    found_note = f"{len(ready)} instance{'s' if len(ready) != 1 else ''} to check ({desired} desired)"
    if starting:
        found_note += f", {len(starting)} still launching or warming up"
    trace.ok("aws_api", found_note, ms_since(started))
    trace.steps.extend(
        Step(step=m["id"], skipped=True, detail="launching or warming up: not checked yet") for m in starting
    )
    base_snapshot = {"desired": desired, "warming": len(starting), "instances": []}

    if not ready:
        if desired == 0 or starting:
            # Scaled to zero, or every instance still starting: nothing to judge yet.
            summary = "Scaled to zero: no instance to check" if desired == 0 else found_note
            return ProbeResult(
                ok=True,
                checked_at=checked_at,
                summary=summary,
                response_time_ms=ms_since(started),
                snapshot={**base_snapshot, "healthy": 0, "total": 0},
                steps=trace.steps,
            )
        message = f"No instance in service: {desired} desired."
        return failed(
            trace, checked_at, started, "no_instances", message,
            snapshot={**base_snapshot, "healthy": 0, "total": 0}, metric=0.0,
        )

    use_public_ip = bool(target.settings.get("use_public_ip"))
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(member: dict) -> tuple[dict, ProbeResult]:
        address = member.get("public_ip") if use_public_ip else member.get("private_ip")
        single = replace(target, kind=ResourceKind.SERVER, resource_name=member["id"], address=address)
        async with gate:
            return member, await inner(single)

    outcomes = await asyncio.gather(*(one(member) for member in ready))

    rows = []
    for member, result in outcomes:
        rows.append(
            {
                "id": member["id"],
                "az": member.get("az"),
                "address": result.address or (member.get("public_ip") if use_public_ip else member.get("private_ip")),
                "ok": result.ok,
                "summary": result.summary,
                "error_type": result.error_type,
                "response_time_ms": result.response_time_ms,
            }
        )
        trace.steps.append(
            Step(
                step=member["id"],
                ok=result.ok,
                skipped=False,
                time_ms=result.response_time_ms,
                error_type=result.error_type,
                detail=f"{rows[-1]['address'] or 'no address'}: {result.summary}",
            )
        )
    passed = [(m, r) for m, r in outcomes if r.ok]
    failing = [(m, r) for m, r in outcomes if not r.ok]
    minimum = min(target.settings.get("min_healthy_instances") or 1, len(ready))
    counted = f"{len(passed)} of {len(ready)} instances pass"
    snapshot = {**base_snapshot, "healthy": len(passed), "total": len(ready), "instances": rows}
    times = [r.response_time_ms for _, r in passed if r.response_time_ms is not None]

    if len(passed) < minimum:
        reasons = "; ".join(f"{m['id']}: {r.summary}" for m, r in failing[:3])
        error_types = {r.error_type for _, r in failing}
        # One cause everywhere (say, every connection timing out) is kept, so a
        # whole VPC going dark still reads as such; a mix is the instances'.
        error_type = error_types.pop() if len(error_types) == 1 else "instances_failing"
        message = f"{counted}, fewer than {minimum}: {reasons}"
        result = failed(
            trace, checked_at, started, error_type or "instances_failing", message,
            detail={"instances": rows}, snapshot=snapshot, metric=float(len(passed)),
        )
        if times:
            result.response_time_ms = round(sum(times) / len(times))
        return result

    problems: dict[str, str] = {}
    if failing:
        member, result = failing[0]
        problems["instances_failing"] = f"{counted}; {member['id']} failing: {result.summary}"
    for member, result in passed:
        for kind, detail in result.problems.items():
            problems.setdefault(kind, f"{member['id']}: {detail}")
    summary = counted if failing else f"{counted}: {passed[0][1].summary}"
    if failing:
        summary += f"; {failing[0][0]['id']}: {failing[0][1].summary}"
    return ProbeResult(
        ok=True,
        checked_at=checked_at,
        summary=summary,
        response_time_ms=round(sum(times) / len(times)) if times else ms_since(started),
        detail={"instances": rows},
        snapshot=snapshot,
        problems=problems,
        metric=float(len(passed)),
        steps=trace.steps,
    )
