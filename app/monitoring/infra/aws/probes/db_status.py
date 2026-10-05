"""What RDS says of a database: its status, and its replication's.

Read with `DescribeDBInstances`, so no connection goes into the VPC and no
database credentials are needed. Up while the database is `available`, or busy
with something it keeps serving through (a backup, a modification, a storage
change); that is the `db_busy` problem. Every other status (stopped,
rebooting, storage full, failed, ...) is down. A read replica whose
replication RDS reports broken or stopped is the `replication_broken` problem.
"""

import time

from app.monitoring.infra.aws import discovery
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.probes.base import ProbeResult, ProbeTarget, Trace, failed, ms_since, now

STEPS = ["aws_api", "status"]
#: Statuses in which the database serves connections.
AVAILABLE = frozenset({"available"})
#: Statuses in which it is busy but, as a rule, still serves them.
BUSY = frozenset(
    {
        "backing-up",
        "configuring-activity-stream",
        "configuring-enhanced-monitoring",
        "configuring-iam-database-auth",
        "configuring-log-exports",
        "converting-to-vpc",
        "maintenance",
        "modifying",
        "moving-to-vpc",
        "renaming",
        "resetting-master-credentials",
        "storage-config-upgrade",
        "storage-initialization",
        "storage-optimization",
        "upgrading",
    }
)
#: What a status means, where its name does not say it plainly.
EXPLAINED = {
    "storage-full": "it has used all its allocated storage",
    "incompatible-parameters": "its parameter group stops it from starting",
    "incompatible-network": "RDS cannot place it in its subnets",
    "insufficient-capacity": "AWS has no capacity for its instance class",
    "inaccessible-encryption-credentials": "its KMS key cannot be used",
    "inaccessible-encryption-credentials-recoverable": "its KMS key cannot be used",
}


def replication_problem(db: dict) -> str | None:
    """Why a read replica's replication is not working, if RDS says so."""
    for info in db.get("StatusInfos") or []:
        if info.get("StatusType") != "read replication":
            continue
        if info.get("Status") in {"error", "stopped", "terminated"} or not info.get("Normal", True):
            message = info.get("Message")
            return f"Replication {info.get('Status')}" + (f": {message}" if message else "")
    return None


async def probe(target: ProbeTarget) -> ProbeResult:
    trace = Trace(STEPS)
    checked_at, started = now(), time.perf_counter()
    ident = target.aws_id
    try:
        db = await discovery.read_database(target.region, ident)
    except AwsError as exc:
        trace.fail("aws_api", "aws_error", f"DescribeDBInstances: {exc}", ms_since(started))
        return failed(trace, checked_at, started, "aws_error", f"{ident}: {exc}")
    api_ms = ms_since(started)
    if db is None:
        message = f"RDS no longer has the database {ident}."
        trace.fail("aws_api", "db_not_found", message, api_ms)
        return failed(trace, checked_at, started, "db_not_found", message)
    trace.ok("aws_api", f"DescribeDBInstances answered for {ident}", api_ms)

    status = db.get("DBInstanceStatus") or "unknown"
    snapshot = {
        "status": status,
        "engine": db.get("Engine"),
        "engine_version": db.get("EngineVersion"),
        "instance_class": db.get("DBInstanceClass"),
        "multi_az": bool(db.get("MultiAZ")),
        "az": db.get("AvailabilityZone"),
        "allocated_storage_gb": db.get("AllocatedStorage"),
    }
    detail = {
        "status": status,
        "status_infos": db.get("StatusInfos") or [],
        "pending_modified_values": db.get("PendingModifiedValues") or {},
    }

    if status not in AVAILABLE and status not in BUSY:
        why = EXPLAINED.get(status)
        message = f"{ident} is {status}" + (f": {why}" if why else "") + "."
        trace.fail("status", "db_unavailable", message)
        result = failed(trace, checked_at, started, "db_unavailable", message, detail=detail, snapshot=snapshot)
        result.response_time_ms = api_ms
        return result

    problems = {}
    if status in BUSY:
        problems["db_busy"] = f"{ident} is {status}"
    if broken := replication_problem(db):
        source = db.get("ReadReplicaSourceDBInstanceIdentifier")
        problems["replication_broken"] = f"{broken}" + (f" (replica of {source})" if source else "")
    trace.ok("status", status)
    return ProbeResult(
        ok=True,
        checked_at=checked_at,
        summary=f"{ident}: {status}",
        response_time_ms=api_ms,
        detail=detail,
        snapshot=snapshot,
        problems=problems,
        steps=trace.steps,
    )
