"""ICMP echo to a server's private IP (or its public one), through the
website checks' pinger.

The server's security group must allow ICMP echo from Watchly. Up when any
echo is answered; losing at least the threshold while it answers is the
`packet_loss` problem.
"""

import time

from app.core.config import settings
from app.monitoring.infra.aws.probes.base import (
    ProbeResult,
    ProbeTarget,
    Trace,
    failed,
    ms_since,
    now,
    policy_note,
)
from app.monitoring.websites.pinger import ping

STEPS = ["resolve", "policy", "echo"]


async def probe(target: ProbeTarget) -> ProbeResult:
    trace = Trace(STEPS)
    checked_at, started = now(), time.perf_counter()
    if not target.address:
        trace.fail("resolve", "no_address", target.missing_address)
        return failed(trace, checked_at, started, "no_address", target.missing_address)

    # Raises IcmpUnavailableError when this server cannot ping at all.
    outcome = await ping(
        target.address,
        count=target.settings.get("count", 3),
        timeout=target.timeout,
        privileged=settings.PING_PRIVILEGED,
        scope=target.scope,
    )
    stats = outcome.stats
    if outcome.error_type == "dns_error":
        trace.fail("resolve", "dns_error", outcome.error or "")
    elif outcome.error_type == "blocked_address":
        trace.ok("resolve", target.address, outcome.dns_ms)
        trace.fail("policy", "blocked_address", outcome.error or "")
    else:
        address = stats.address if stats else target.address
        trace.ok("resolve", address, outcome.dns_ms)
        trace.ok("policy", policy_note(address, target.scope))
        if outcome.is_up:
            trace.ok("echo", stats.summary, round(stats.avg_ms) if stats.avg_ms else None)
        else:
            trace.fail("echo", outcome.error_type or "no_reply", outcome.error or "No reply.")

    group = (
        {
            "address": stats.address,
            "sent": stats.sent,
            "received": stats.received,
            "loss_percent": stats.loss_percent,
            "min_ms": stats.min_ms,
            "avg_ms": stats.avg_ms,
            "max_ms": stats.max_ms,
            "jitter_ms": stats.jitter_ms,
        }
        if stats
        else None
    )
    if not outcome.is_up:
        return failed(
            trace,
            checked_at,
            started,
            outcome.error_type or "no_reply",
            outcome.error or "No reply.",
            dns_ms=outcome.dns_ms,
            address=stats.address if stats else None,
            detail={"ping": group} if group else None,
            metric=100.0 if stats else None,
        )

    problems = {}
    threshold = target.settings.get("packet_loss_threshold_percent")
    if threshold and stats.loss_percent >= threshold:
        problems["packet_loss"] = (
            f"{stats.loss_percent:g}% of pings lost (limit {threshold}%)"
        )
    return ProbeResult(
        ok=True,
        checked_at=checked_at,
        summary=stats.summary,
        response_time_ms=round(stats.avg_ms) if stats.avg_ms is not None else ms_since(started),
        dns_ms=outcome.dns_ms,
        address=stats.address,
        detail={"ping": group},
        snapshot={"address": stats.address, "avg_ms": stats.avg_ms, "loss_percent": stats.loss_percent},
        problems=problems,
        metric=stats.loss_percent,
        steps=trace.steps,
    )
