"""One probe per check type. `run` picks it and repeats a failure, as a
website check is repeated, before it counts. An Auto Scaling group's `ping`,
`tcp` and `http` run on each of its instances (`group.py`). A database's
`db_status` and `db_metrics` ask RDS and CloudWatch, without logging in, and
a server's `ec2_metrics` asks CloudWatch."""

import asyncio
import functools
from collections.abc import Awaitable, Callable

from app.core.config import settings
from app.monitoring.infra.aws.models import NETWORK_CHECKS, InfraCheckType, ResourceKind
from app.monitoring.infra.aws.probes import (
    db_metrics,
    db_status,
    ec2_metrics,
    group,
    group_health,
    http,
    ping,
    target_health,
    tcp,
)
from app.monitoring.infra.aws.probes.base import ProbeResult, ProbeTarget

Probe = Callable[[ProbeTarget], Awaitable[ProbeResult]]

PROBES: dict[InfraCheckType, Probe] = {
    InfraCheckType.PING: ping.probe,
    InfraCheckType.TCP: tcp.probe,
    InfraCheckType.HTTP: http.probe,
    InfraCheckType.TARGET_HEALTH: target_health.probe,
    InfraCheckType.GROUP_HEALTH: group_health.probe,
    InfraCheckType.DB_STATUS: db_status.probe,
    InfraCheckType.DB_METRICS: db_metrics.probe,
    InfraCheckType.EC2_METRICS: ec2_metrics.probe,
}

#: Failures a retry cannot fix: the policy, a missing address, a group with
#: nothing in service, or a database RDS no longer has.
_FINAL = frozenset({"blocked_address", "no_address", "no_instances", "db_not_found"})


def probe_for(target: ProbeTarget) -> Probe:
    """The probe for this target, fanned out over a group's instances."""
    probe = PROBES[target.check_type]
    if target.kind is ResourceKind.AUTO_SCALING_GROUP and target.check_type in NETWORK_CHECKS:
        return functools.partial(group.probe, inner=probe)
    return probe


async def probe_once(target: ProbeTarget) -> ProbeResult:
    """One run, no retries: what diagnose shows."""
    return await probe_for(target)(target)


async def run(target: ProbeTarget, retries: int = 0) -> ProbeResult:
    """Probe once, and again up to `retries` times CHECK_RETRY_DELAY_SECONDS
    apart while it fails; the last result counts. Raises only
    `IcmpUnavailableError`."""
    probe = probe_for(target)
    result = await probe(target)
    for _ in range(retries):
        if result.ok or result.error_type in _FINAL:
            break
        await asyncio.sleep(settings.CHECK_RETRY_DELAY_SECONDS)
        result = await probe(target)
    return result


__all__ = ["PROBES", "ProbeResult", "ProbeTarget", "probe_for", "probe_once", "run"]
