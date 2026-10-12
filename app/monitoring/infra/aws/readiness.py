"""What each resource still needs before Watchly can watch it: the setup
checklist on the Infrastructure page.

A resource that never turns healthy was most often never reachable: a
security group with no rule for Watchly, a port nothing listens on,
credentials that may not read CloudWatch. For every resource in scope this
says, step by step, what is in place and what is left to do, from:

- where Watchly runs, whether it can send pings, whether each AWS account's
  credentials work, and each VPC's reachability and last test;
- the resource's security groups, read now and compared with each network
  check's traffic and where Watchly connects from (`hints` does the same
  for one failed check);
- each check's last result: a refusal, a timeout the groups do not explain,
  an AWS call refused, the CloudWatch agent not reporting;
- the checks it lacks, as `discovery.suggest` would add them.

Security groups and Watchly's own placement show the network's layout, so
only Admin and DevOps get them read and named; everyone else gets the steps
the checks' results give. Nothing here changes anything in AWS.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.instance_metadata import InstanceMetadata
from app.monitoring.infra.aws import client, discovery, hints
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.diagnose import can_ping
from app.monitoring.infra.aws.models import (
    NETWORK_CHECKS,
    AwsAccount,
    AwsCheck,
    AwsResource,
    AwsVpc,
    InfraCheckType,
    ResourceKind,
    ResourceState,
    is_public_path,
)
from app.monitoring.infra.aws.probes.base import CONNECT_FAILURES
from app.monitoring.infra.aws.schemas import (
    AttentionResource,
    Readiness,
    ReadinessResource,
    ReadinessStatus,
    ReadinessStep,
    VpcBrief,
)
from app.monitoring.infra.aws.service import is_watchly_vpc

logger = logging.getLogger(__name__)

DONE, TODO, UNKNOWN, OPTIONAL = (
    ReadinessStatus.DONE,
    ReadinessStatus.TODO,
    ReadinessStatus.UNKNOWN,
    ReadinessStatus.OPTIONAL,
)

#: Security groups read for one VPC are reused this long.
GROUPS_TTL_SECONDS = 60
#: An Auto Scaling group's instances whose security groups are compared.
MAX_MEMBERS = 20
#: Security group ids per DescribeSecurityGroups filter.
FILTER_BATCH = 200

#: The AWS call behind each check that asks AWS, named when it is refused.
API_ACTIONS = {
    InfraCheckType.TARGET_HEALTH: "elasticloadbalancing:DescribeTargetHealth",
    InfraCheckType.GROUP_HEALTH: "autoscaling:DescribeAutoScalingGroups",
    InfraCheckType.DB_STATUS: "rds:DescribeDBInstances",
    InfraCheckType.DB_METRICS: "cloudwatch:GetMetricData",
    InfraCheckType.EC2_METRICS: "cloudwatch:GetMetricData",
}
ACCESS_DENIED = ("AccessDenied", "UnauthorizedOperation")

#: The check worth having on each kind that asks AWS rather than connects.
RECOMMENDED = {
    ResourceKind.SERVER: (InfraCheckType.EC2_METRICS, "server metrics", "CPU, CPU credits and EBS burst balance from CloudWatch"),
    ResourceKind.LOAD_BALANCER: (InfraCheckType.TARGET_HEALTH, "target health", "the load balancer's own verdict on each target"),
    ResourceKind.AUTO_SCALING_GROUP: (InfraCheckType.GROUP_HEALTH, "group health", "instances in service and healthy against desired capacity"),
    ResourceKind.DATABASE: (InfraCheckType.DB_STATUS, "database status", "what RDS says of it: available, storage full, stopped"),
}
#: The network check to suggest when a resource has none.
NETWORK_SUGGESTION = {
    ResourceKind.SERVER: "a ping, or a tcp check on a port it serves (22, 80, 443)",
    ResourceKind.LOAD_BALANCER: "an http check on one of its listeners (its /health, say)",
    ResourceKind.AUTO_SCALING_GROUP: "an http or tcp check on the port its target group's health check calls",
    ResourceKind.DATABASE: "a tcp check on its port (no login)",
}

#: Failures before any packet is sent: what to do about each.
SETUP_FAILURES = {
    "dns_error": (
        "Watchly could not resolve its address.",
        "For a private name, Watchly must use the VPC's resolver (AmazonProvidedDNS): check the VPC has DNS "
        "resolution and DNS hostnames on, and that Watchly's own DNS reaches it.",
    ),
    "blocked_address": (
        "Watchly's egress policy refused the address.",
        "The address is outside the VPC's registered ranges, or in EGRESS_DENY_CIDRS: run Test on the VPC to "
        "refresh its ranges, or change EGRESS_DENY_CIDRS.",
    ),
    "no_address": (
        "AWS gave it no address to connect to.",
        "Start the instance; for a check sent to the public IP, give it a public or Elastic IP, or turn "
        "use_public_ip off.",
    ),
    "no_instances": (
        "The group has no instance in service to check.",
        "Scale the group up, or pause this resource while it is empty on purpose.",
    ),
}


def _step(area: str, status: ReadinessStatus, title: str, **rest) -> ReadinessStep:
    return ReadinessStep(area=area, status=status, title=title, **rest)


# --- where Watchly's connections come from ----------------------------------------


@dataclass(slots=True)
class _Source:
    """What an inbound rule must name for Watchly's connections to one
    resource: its security groups, its address, or why neither is known."""

    groups: set[str]
    ip: str | None
    label: str | None
    unknown: str | None = None


def _source(watchly: InstanceMetadata | None, vpc: AwsVpc, public: bool) -> _Source:
    if watchly is None:
        return _Source(
            set(), None, None,
            "Watchly does not run on EC2 (or its instance metadata is off), so it cannot tell where its "
            "connections come from.",
        )
    same_vpc = is_watchly_vpc(watchly, vpc.account, vpc.aws_vpc_id)
    groups = {g for g, _ in watchly.security_groups} if same_vpc and not public else set()
    ip = watchly.public_ip if public else watchly.private_ip
    if groups:
        group_id, name = watchly.security_groups[0]
        return _Source(groups, ip, f"{group_id} ({name})")
    if ip:
        return _Source(groups, ip, f"{ip}/32")
    if public:
        return _Source(
            groups, None, None,
            "Watchly has no public IP: its connections to public addresses leave through a NAT gateway, whose "
            "address a rule must name.",
        )
    return _Source(groups, None, None, "Watchly's private IP is not known.")


# --- security groups, read per VPC ------------------------------------------------


@dataclass(slots=True)
class _Groups:
    """A VPC's security groups as read now, by id, and the groups of each
    Auto Scaling group's instances in service."""

    by_id: dict[str, dict] = field(default_factory=dict)
    members: dict[str, list[tuple[str, list[str]]]] = field(default_factory=dict)
    error: str | None = None


_cache: dict[tuple, tuple[float, _Groups]] = {}


def _why(exc: AwsError) -> str:
    if exc.access_denied:
        return "Watchly's AWS credentials may not read them (ec2:DescribeSecurityGroups, ec2:DescribeInstances)"
    return str(exc)


async def _read_groups(vpc: AwsVpc, resources: list[AwsResource]) -> _Groups:
    key = (vpc.id, tuple(sorted(r.id for r in resources)))
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < GROUPS_TTL_SECONDS:
        return hit[1]

    region = client.Region.of(vpc)
    found = _Groups()
    members = {
        r.aws_id: discovery.in_service_ids(r.aws_detail)[:MAX_MEMBERS]
        for r in resources
        if r.kind is ResourceKind.AUTO_SCALING_GROUP
    }
    wanted = [i for ids in members.values() for i in ids]
    if wanted:
        try:
            instances = await discovery.instances_by_id(region, wanted)
        except AwsError as exc:
            instances, found.error = {}, _why(exc)
        for name, ids in members.items():
            found.members[name] = [
                (i, [g["GroupId"] for g in instances[i].get("SecurityGroups", []) if g.get("GroupId")])
                for i in ids
                if i in instances
            ]

    ids = {g for r in resources for g in (r.aws_detail or {}).get("security_groups") or []}
    ids.update(g for rows in found.members.values() for _, groups in rows for g in groups)
    ordered = sorted(ids)
    try:
        for start in range(0, len(ordered), FILTER_BATCH):
            # A filter, so a group deleted since the last sync is left out
            # instead of failing the call.
            rows = await client.paginate(
                "ec2", "describe_security_groups", region, "SecurityGroups",
                Filters=[{"Name": "group-id", "Values": ordered[start : start + FILTER_BATCH]}],
            )
            found.by_id.update({g["GroupId"]: g for g in rows})
    except AwsError as exc:
        found.error = found.error or _why(exc)

    if len(_cache) > 256:
        _cache.clear()
    _cache[key] = (time.monotonic(), found)
    return found


@dataclass(slots=True)
class _Verdict:
    """Whether the resource's security groups let Watchly's traffic in."""

    allowed: bool | None
    detail: str | None = None
    fix: str | None = None
    group: str | None = None


def _verdict(
    resource: AwsResource, traffic: hints.Traffic, source: _Source, groups: _Groups
) -> _Verdict:
    if source.label is None:
        return _Verdict(None, source.unknown)
    if groups.error:
        return _Verdict(None, f"The security groups could not be read: {groups.error}.")
    detail = resource.aws_detail or {}
    if resource.kind is ResourceKind.AUTO_SCALING_GROUP:
        sets = groups.members.get(resource.aws_id) or []
        if not sets:
            return _Verdict(None, "No instance in service to read security groups from.")
    else:
        sets = [(resource.name, list(detail.get("security_groups") or []))]

    unresolved = False
    for who, ids in sets:
        if not ids:
            if resource.kind is ResourceKind.LOAD_BALANCER:
                return _Verdict(
                    None,
                    "This load balancer has no security groups: traffic goes on to its targets, whose groups "
                    "must allow it.",
                )
            return _Verdict(None, f"No security groups are recorded for {who}; open the resource to sync it.")
        found = [groups.by_id[g] for g in ids if g in groups.by_id]
        if not found:
            return _Verdict(None, f"The security groups of {who} ({', '.join(ids)}) were not found.")
        allowed = hints.inbound_allows(found, traffic, source.groups, source.ip)
        if allowed is False:
            first = found[0]
            name = f"{first['GroupId']} ({first.get('GroupName') or 'unnamed'})"
            return _Verdict(
                False,
                f"The security groups of {who} ({', '.join(ids)}) have no inbound rule for {traffic.rule} "
                f"from {source.label}.",
                f"Add an inbound rule to {name}: {traffic.rule}, source {source.label}.",
                first["GroupId"],
            )
        unresolved = unresolved or allowed is None
    if unresolved:
        return _Verdict(None, "A rule using a prefix list may or may not cover Watchly; its members are not read.")
    return _Verdict(True, f"The security groups allow {traffic.rule} from {source.label}.")


# --- one resource's steps ---------------------------------------------------------


def _network_step(
    check: AwsCheck,
    resource: AwsResource,
    watchly: InstanceMetadata | None,
    groups: _Groups | None,
    pings: bool,
) -> ReadinessStep | None:
    """Whether Watchly's packets for `check` get there and are answered."""
    traffic = hints.traffic_of(check)
    if traffic is None:
        return None
    vpc = resource.vpc
    public = is_public_path(resource.kind, resource.aws_detail, check.settings)
    title = f"{check.name}: {traffic.rule} from Watchly {'over the internet' if public else 'inside the VPC'}"
    base = {"check_id": check.id}
    if traffic.protocol == "icmp" and not pings:
        return _step(
            "watchly", TODO, title, **base,
            detail="This Watchly server cannot send pings, so the check never runs.",
            fix="Use a tcp check instead, or let the API send pings (see Watchly above).",
        )

    source = _source(watchly, vpc, public)
    verdict = _verdict(resource, traffic, source, groups) if groups is not None else _Verdict(None)
    last = check.last_result or {}
    error = last.get("error_type") if last and not last.get("ok") else None
    summary = (last.get("summary") or "").rstrip(".")

    if last.get("ok"):
        return _step("network", DONE, title, **base, detail="Answered at its last run.")
    if error == "connect_refused" and traffic.protocol == "tcp":
        return _step(
            "service", TODO, title, **base,
            detail=f"The host answered but refused port {traffic.port}: it is reachable, and nothing listens there.",
            fix=hints.refused_fix(resource, traffic.port),
        )
    if error == "tls_error":
        return _step(
            "service", TODO, title, **base,
            detail=f"Port {traffic.port} answers, but not with TLS.",
            fix="Turn TLS (or https) off for this check if the service is plain; otherwise point it at the port that serves TLS.",
        )
    if error in SETUP_FAILURES:
        detail, fix = SETUP_FAILURES[error]
        return _step("network", TODO, title, **base, detail=" ".join(filter(None, [detail, summary and f"{summary}."])), fix=fix)
    if verdict.allowed is False:
        return _step("network", TODO, title, **base, detail=verdict.detail, fix=verdict.fix, aws_ref=verdict.group)
    if error in CONNECT_FAILURES:
        if verdict.allowed is True:
            subnet = (resource.aws_detail or {}).get("subnet_id")
            return _step(
                "network", TODO, title, **base,
                detail=f"{verdict.detail} Yet it gets no answer: {summary}.",
                fix=(
                    f"Look at the network ACLs of {f'subnet {subnet}' if subnet else 'its subnet'} (the port inbound, "
                    "replies on 1024-65535 outbound), Watchly's own outbound rules, then the host's firewall "
                    "(ufw, iptables, firewalld, Windows Firewall)."
                    + ("" if public or (watchly and is_watchly_vpc(watchly, vpc.account, vpc.aws_vpc_id))
                       else " Watchly runs in another VPC: both sides' route tables need a route through the peering or transit gateway.")
                ),
                aws_ref=subnet,
            )
        return _step(
            "network", TODO, title, **base,
            detail=f"No answer: {summary}. {verdict.detail or ''}".strip(),
            fix=(
                f"Allow Watchly in the resource's security group: inbound {traffic.rule}"
                + (f", source {source.label}." if groups is not None and source.label else " from Watchly's address.")
                + " Then check the subnet's network ACLs. Diagnose on the check names the exact group and rule"
                " (for Admin and DevOps)."
            ),
        )
    if error:
        # It got there: what fails now is the resource's health, not the setup.
        return _step("network", DONE, title, **base, detail=f"Reached; the check fails on its own terms: {summary}.")
    if not check.last_checked_at:
        if verdict.allowed is True:
            return _step("network", DONE, title, **base, detail=f"{verdict.detail} Not checked yet.")
        return _step(
            "network", UNKNOWN, title, **base,
            detail=" ".join(filter(None, ["Not checked yet.", verdict.detail])),
            fix=f"Make sure its security group lets {traffic.rule} in from Watchly, then Check now.",
        )
    return _step("network", UNKNOWN, title, **base, detail=verdict.detail)


def _api_step(check: AwsCheck) -> ReadinessStep:
    """Whether Watchly's credentials may make the AWS call the check makes."""
    action = API_ACTIONS[check.check_type]
    title = f"{check.name}: Watchly may call {action}"
    last = check.last_result
    if not last:
        return _step("permission", UNKNOWN, title, check_id=check.id, detail="Not checked yet.")
    if last.get("ok") or last.get("error_type") != "aws_error":
        return _step("permission", DONE, title, check_id=check.id, detail="AWS answers.")
    summary = last.get("summary") or ""
    if any(code in summary for code in ACCESS_DENIED):
        return _step(
            "permission", TODO, title, check_id=check.id,
            detail=f"AWS refused it: {summary}",
            fix=f"Allow {action} in the IAM policy of the account's role or user (AWS & access shows each permission), then Check now.",
        )
    return _step(
        "permission", TODO, title, check_id=check.id,
        detail=f"The AWS call failed: {summary}",
        fix="Check the account's credentials and region under AWS & access.",
    )


def _agent_step(check: AwsCheck) -> ReadinessStep | None:
    """Disk use, which only the CloudWatch agent reports."""
    if check.settings.get("disk_used_percent_max") is None:
        return None
    note = (check.snapshot or {}).get("disk_note")
    title = f"{check.name}: disk use from the CloudWatch agent"
    if not check.snapshot:
        return _step("agent", UNKNOWN, title, check_id=check.id, detail="Not checked yet.")
    if not note:
        return _step("agent", DONE, title, check_id=check.id, detail="The agent reports its disks.")
    if "could not be listed" in note:
        return _step(
            "permission", TODO, title, check_id=check.id,
            detail=note[0].upper() + note[1:] + ".",
            fix="Allow cloudwatch:ListMetrics in the IAM policy of the account's role or user.",
        )
    return _step(
        "agent", TODO, title, check_id=check.id,
        detail="No CloudWatch agent disk metrics for it, so its disks are not watched.",
        fix=(
            "Install the CloudWatch agent on the instance with the disk plugin (disk_used_percent, InstanceId "
            "in append_dimensions) and give its instance role CloudWatchAgentServerPolicy; or clear the disk threshold."
        ),
    )


def resource_steps(
    resource: AwsResource, watchly: InstanceMetadata | None, groups: _Groups | None, pings: bool
) -> list[ReadinessStep]:
    if resource.missing_since is not None:
        return [
            _step(
                "checks", TODO, "AWS no longer lists it",
                detail="Its checks are stopped.",
                fix="Delete it here if it was removed on purpose, or add its replacement.",
            )
        ]
    steps: list[ReadinessStep] = []
    if not resource.is_enabled:
        steps.append(_step("checks", OPTIONAL, "Paused", detail="Its checks do not run until it is resumed."))
    checks = [c for c in resource.checks if c.is_enabled]
    if not checks:
        steps.append(
            _step(
                "checks", TODO, "Add a check",
                detail="Nothing watches it.",
                fix=f"Open it and add {NETWORK_SUGGESTION[resource.kind]}, and {RECOMMENDED[resource.kind][1]}.",
            )
        )
        return steps

    for check in checks:
        if check.check_type in NETWORK_CHECKS:
            step = _network_step(check, resource, watchly, groups, pings)
        else:
            step = _api_step(check)
        if step is not None:
            steps.append(step)
        if check.check_type is InfraCheckType.EC2_METRICS and (agent := _agent_step(check)) is not None:
            steps.append(agent)

    types = {c.check_type for c in checks}
    if not types & NETWORK_CHECKS:
        steps.append(
            _step(
                "checks", OPTIONAL, "Nothing checks it over the network",
                detail="Only AWS's view of it is watched: it could stop answering unseen.",
                fix=f"Add {NETWORK_SUGGESTION[resource.kind]}.",
            )
        )
    wanted, label, why = RECOMMENDED[resource.kind]
    if wanted not in types:
        steps.append(_step("checks", OPTIONAL, f"Add a {label} check", detail=why[0].upper() + why[1:] + "."))
    return steps


# --- what applies to everything ---------------------------------------------------


def _watchly_steps(watchly: InstanceMetadata | None, pings_needed: bool, pings: bool) -> list[ReadinessStep]:
    steps = []
    if watchly is None:
        steps.append(
            _step(
                "watchly", UNKNOWN, "Watchly does not run on EC2",
                detail=(
                    "It reaches private addresses only if this server's network routes to the VPC (a VPN, say), "
                    "and security group rules must name the address it connects from."
                ),
                fix="Run Watchly on an EC2 instance in (or peered with) the VPC, with a read-only instance role.",
            )
        )
    else:
        groups = ", ".join(f"{g} ({n})" for g, n in watchly.security_groups) or "none"
        steps.append(
            _step(
                "watchly", DONE, "Watchly runs on EC2",
                detail=f"{watchly.instance_id} in {watchly.vpc_id}, {watchly.private_ip}; security groups {groups}.",
                aws_ref=watchly.security_groups[0][0] if watchly.security_groups else None,
            )
        )
    if pings_needed:
        steps.append(
            _step("watchly", DONE, "Watchly can send pings")
            if pings
            else _step(
                "watchly", TODO, "Watchly cannot send pings",
                detail="Every ping check is skipped.",
                fix=(
                    "Open net.ipv4.ping_group_range for the API's user (docker-compose.yml does), or give it "
                    "CAP_NET_RAW and set PING_PRIVILEGED=true."
                ),
            )
        )
    return steps


def _account_step(account: AwsAccount) -> ReadinessStep:
    title = f"{account.name}: credentials work"
    if account.last_error:
        return _step(
            "account", TODO, title,
            detail=account.last_error,
            fix="Fix its credentials or role under AWS & access, then Test it: the test lists every permission Watchly needs.",
        )
    if account.verified_at is None:
        return _step("account", UNKNOWN, title, detail="Never verified.", fix="Test it under AWS & access.")
    return _step("account", DONE, title, detail=f"Verified for {account.aws_account_id or 'its account'}.")


def _vpc_step(vpc: AwsVpc) -> ReadinessStep:
    step = _vpc_status(vpc)
    step.vpc_id = vpc.id
    return step


def _vpc_status(vpc: AwsVpc) -> ReadinessStep:
    title = f"{vpc.name}: Watchly reaches into it"
    if vpc.unreachable_since is not None:
        return _step(
            "vpc", TODO, title,
            detail=f"Most checks into it have failed to connect since {vpc.unreachable_since:%Y-%m-%d %H:%M} UTC.",
            fix=(
                "Allow Watchly in its resources' security groups (below). If Watchly runs in another VPC, both "
                "sides' route tables need a route through the peering or transit gateway. Then run Test on the VPC."
            ),
            aws_ref=vpc.aws_vpc_id,
        )
    test = vpc.last_test
    if not test:
        return _step(
            "vpc", OPTIONAL, title,
            detail="Never tested.",
            fix="Open the VPC and run Test: it checks where Watchly runs, its AWS access, and connects to a sample of resources.",
            aws_ref=vpc.aws_vpc_id,
        )
    if not test.get("ok"):
        failed = "; ".join(s.get("detail") or s.get("step") for s in test.get("steps", []) if not s.get("ok"))
        return _step(
            "vpc", TODO, title,
            detail=f"Its last test failed: {failed}",
            fix="Fix what the test names, then run it again.",
            aws_ref=vpc.aws_vpc_id,
        )
    return _step("vpc", DONE, title, detail="Its last test passed.", aws_ref=vpc.aws_vpc_id)


# --- the whole checklist ----------------------------------------------------------


def _todo(steps: list[ReadinessStep]) -> int:
    return sum(1 for s in steps if s.status is TODO)


async def readiness(resources: list[AwsResource], *, manager: bool) -> Readiness:
    """The checklist for `resources`. With `manager`, security groups are
    read and named, and Watchly's placement and the accounts are covered."""
    watchly = await client.identity()
    pings = can_ping()
    by_vpc: dict[int, list[AwsResource]] = {}
    for resource in resources:
        by_vpc.setdefault(resource.vpc_id, []).append(resource)
    vpcs = {r.vpc_id: r.vpc for r in resources}

    groups: dict[int, _Groups | None] = dict.fromkeys(by_vpc)
    if manager and watchly is not None:
        read = await asyncio.gather(
            *(_read_groups(vpcs[vpc_id], rows) for vpc_id, rows in by_vpc.items()), return_exceptions=True
        )
        for vpc_id, result in zip(by_vpc, read, strict=True):
            if isinstance(result, BaseException):
                logger.exception("Could not read security groups of VPC %s", vpc_id, exc_info=result)
                result = _Groups(error=str(result))
            groups[vpc_id] = result

    general: list[ReadinessStep] = []
    pings_needed = any(
        c.is_enabled and c.check_type is InfraCheckType.PING for r in resources for c in r.checks
    )
    if manager:
        general.extend(_watchly_steps(watchly, pings_needed, pings))
        accounts = {v.account.id: v.account for v in vpcs.values()}
        general.extend(_account_step(a) for a in sorted(accounts.values(), key=lambda a: a.name.lower()))
    elif pings_needed and not pings:
        general.extend(s for s in _watchly_steps(watchly, True, False) if s.status is TODO)
    general.extend(_vpc_step(v) for v in sorted(vpcs.values(), key=lambda v: v.name.lower()))

    rows = []
    for resource in resources:
        steps = resource_steps(resource, watchly, groups[resource.vpc_id], pings)
        rows.append(
            ReadinessResource(
                resource=AttentionResource(
                    id=resource.id, name=resource.name, kind=resource.kind, vpc=VpcBrief.model_validate(resource.vpc)
                ),
                state=resource.state,
                todo=_todo(steps),
                steps=steps,
            )
        )
    rows.sort(key=lambda row: (-row.todo, row.state is not ResourceState.DOWN, row.resource.name.lower()))
    return Readiness(
        checked_at=datetime.now(UTC),
        total=len(rows),
        ready=sum(1 for row in rows if row.todo == 0),
        security_groups_read=manager and watchly is not None,
        general=general,
        resources=rows,
    )
