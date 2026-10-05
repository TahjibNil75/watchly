"""Where to look when a check cannot get through, and what to add or change.

A failed `ping`, `tcp` or `http` check says what went wrong on the wire; this
says where in AWS to fix it:

- a timeout means something dropped the packets. The target's security groups
  are read and compared with where Watchly's connections come from: when none
  has an inbound rule for the port, the hint names the group and the rule to
  add. When they do allow it, the hint points past them: Watchly's own
  outbound rules, the subnet's network ACLs, the routes, the host's firewall.
- "connection refused" means the host answered and nothing listens on the
  port, so security groups are not the cause: the hint says so, and which
  ports the resource is known to use.
- a TLS failure means the port answers, but not with TLS.

Network ACLs and route tables are pointed at, not evaluated. VPC Reachability
Analyzer would, but AWS bills it per analysis. Needs `ec2:DescribeNetworkInterfaces`
and `ec2:DescribeSecurityGroups`; without them the hint says which groups to
open by hand.

Hints are advice: nothing here changes anything in AWS, and a failure to
produce one never fails a diagnose.
"""

import ipaddress
import logging
from dataclasses import dataclass

from app.core.instance_metadata import InstanceMetadata
from app.monitoring import egress
from app.monitoring.infra.aws import client, discovery
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.models import AwsCheck, AwsResource, AwsVpc, InfraCheckType, ResourceKind
from app.monitoring.infra.aws.probes.base import CONNECT_FAILURES, ProbeResult, ProbeTarget
from app.monitoring.infra.aws.schemas import DiagnoseHint
from app.monitoring.infra.aws.service import is_watchly_vpc

logger = logging.getLogger(__name__)

#: Failing targets explained per diagnose; an Auto Scaling group's instances
#: mostly share their groups, and the hints are deduplicated anyway.
MAX_TARGETS = 3
ICMP_RULE = "ICMP echo request (type 8)"


@dataclass(slots=True)
class _Failure:
    """One target that did not answer: a resource, or one instance of a group."""

    label: str
    address: str | None
    error_type: str


@dataclass(slots=True)
class _Traffic:
    protocol: str  # "tcp" or "icmp"
    port: int | None

    @property
    def rule(self) -> str:
        return ICMP_RULE if self.protocol == "icmp" else f"TCP {self.port}"


def _traffic(check: AwsCheck) -> _Traffic | None:
    """What the check sends, or None for a type that sends nothing to a port."""
    settings = check.settings or {}
    match check.check_type:
        case InfraCheckType.PING:
            return _Traffic("icmp", None)
        case InfraCheckType.TCP if settings.get("port"):
            return _Traffic("tcp", int(settings["port"]))
        case InfraCheckType.HTTP:
            return _Traffic("tcp", int(settings.get("port") or (443 if settings.get("scheme") == "https" else 80)))
    return None


def _failures(resource: AwsResource, result: ProbeResult, target: ProbeTarget) -> list[_Failure]:
    """The targets that failed: an Auto Scaling group's failing instances, or
    the resource itself."""
    if resource.kind is ResourceKind.AUTO_SCALING_GROUP:
        rows = (result.detail or {}).get("instances") or []
        return [
            _Failure(row["id"], row.get("address"), row["error_type"])
            for row in rows
            if not row.get("ok") and row.get("error_type")
        ]
    if result.ok or not result.error_type:
        return []
    return [_Failure(resource.name, target.address, result.error_type)]


def _is_ip(value: str | None) -> bool:
    try:
        ipaddress.ip_address(value or "")
    except ValueError:
        return False
    return True


def _covers(rule: dict, traffic: _Traffic) -> bool:
    """Whether a security group rule's protocol and ports include the traffic."""
    protocol = str(rule.get("IpProtocol"))
    if protocol in {"-1", "all"}:
        return True
    if traffic.protocol == "icmp":
        # For ICMP the "ports" are the type: 8 is an echo request, -1 any.
        return protocol in {"icmp", "1"} and rule.get("FromPort") in (-1, 8, None)
    if protocol not in {"tcp", "6"} or rule.get("FromPort") is None or traffic.port is None:
        return False
    return rule["FromPort"] <= traffic.port <= rule.get("ToPort", rule["FromPort"])


def _inbound_allows(
    groups: list[dict], traffic: _Traffic, watchly_groups: set[str], watchly_ip: str | None
) -> bool | None:
    """Whether the groups let Watchly in. None when a prefix list might: its
    members are not read."""
    unresolved = False
    for group in groups:
        for rule in group.get("IpPermissions", []):
            if not _covers(rule, traffic):
                continue
            if discovery.rule_from_watchly(rule, watchly_groups, watchly_ip):
                return True
            unresolved = unresolved or bool(rule.get("PrefixListIds"))
    return None if unresolved else False


def _outbound_allows(groups: list[dict], traffic: _Traffic, address: str, target_groups: set[str]) -> bool:
    """Whether the groups let Watchly's connections out to `address`. Prefix
    lists are given the benefit of the doubt."""
    ip = ipaddress.ip_address(address)
    for group in groups:
        for rule in group.get("IpPermissionsEgress", []):
            if not _covers(rule, traffic):
                continue
            if rule.get("PrefixListIds") or any(pair.get("GroupId") in target_groups for pair in rule.get("UserIdGroupPairs", [])):
                return True
            if any(ip in ipaddress.ip_network(r["CidrIp"], strict=False) for r in rule.get("IpRanges", []) if r.get("CidrIp")):
                return True
    return False


@dataclass(slots=True)
class _Context:
    resource: AwsResource
    vpc: AwsVpc
    target: ProbeTarget
    traffic: _Traffic
    watchly: InstanceMetadata | None
    region: client.Region
    #: Whether the connection leaves Watchly for the internet, so the target
    #: sees its public address rather than its private one or its groups.
    public: bool

    @property
    def same_vpc(self) -> bool:
        return is_watchly_vpc(self.watchly, self.vpc.account, self.vpc.aws_vpc_id)

    @property
    def watchly_groups(self) -> list[tuple[str, str]]:
        if self.watchly is None or self.public or not self.same_vpc:
            return []
        return list(self.watchly.security_groups)

    @property
    def watchly_ip(self) -> str | None:
        if self.watchly is None:
            return None
        return self.watchly.public_ip if self.public else self.watchly.private_ip

    @property
    def source(self) -> str | None:
        """What an inbound rule would name: Watchly's security group where
        that works (inside one VPC), else its address."""
        if self.watchly_groups:
            group_id, name = self.watchly_groups[0]
            return f"{group_id} ({name})"
        return f"{self.watchly_ip}/32" if self.watchly_ip else None


async def _locate(ctx: _Context, address: str | None) -> tuple[list[str], str | None]:
    """The security group ids and subnet of whatever answers at `address`:
    its network interface, else what the last sync recorded."""
    detail = ctx.resource.aws_detail or {}
    groups: list[str] = []
    subnet = detail.get("subnet_id")
    if _is_ip(address):
        name = "association.public-ip" if ctx.public else "addresses.private-ip-address"
        try:
            interfaces = await client.paginate(
                "ec2",
                "describe_network_interfaces",
                ctx.region,
                "NetworkInterfaces",
                Filters=[{"Name": name, "Values": [address]}, {"Name": "vpc-id", "Values": [ctx.vpc.aws_vpc_id]}],
            )
        except AwsError as exc:
            logger.info("Could not read the network interface of %s for a hint: %s", address, exc)
            interfaces = []
        groups = [g["GroupId"] for eni in interfaces for g in eni.get("Groups", []) if g.get("GroupId")]
        subnet = next((eni["SubnetId"] for eni in interfaces if eni.get("SubnetId")), subnet)
    groups = groups or list(detail.get("security_groups") or [])
    return list(dict.fromkeys(groups)), subnet


async def _read_groups(region: client.Region, ids: list[str]) -> dict[str, dict]:
    found = await client.paginate("ec2", "describe_security_groups", region, "SecurityGroups", GroupIds=ids)
    return {group["GroupId"]: group for group in found}


def _named(group: dict) -> str:
    return f"{group['GroupId']} ({group.get('GroupName') or 'unnamed'})"


async def _watchly_outbound(ctx: _Context, failure: _Failure, target_groups: set[str]) -> DiagnoseHint | None:
    """Watchly's own groups, when none lets the connection out. Only read for
    Watchly's own account: that is whose credentials its instance role holds."""
    watchly = ctx.watchly
    if watchly is None or not watchly.security_groups or not watchly.region or not _is_ip(failure.address):
        return None
    if watchly.account_id and ctx.vpc.account.aws_account_id and watchly.account_id != ctx.vpc.account.aws_account_id:
        return None
    ids = [group_id for group_id, _ in watchly.security_groups]
    try:
        groups = await _read_groups(client.Region(client.OWN, watchly.region), ids)
    except AwsError as exc:
        logger.info("Could not read Watchly's security groups for a hint: %s", exc)
        return None
    if _outbound_allows(list(groups.values()), ctx.traffic, failure.address, target_groups):
        return None
    first = groups.get(ids[0], {"GroupId": ids[0]})
    return DiagnoseHint(
        kind="security_group",
        resource=ids[0],
        detail=f"Watchly's own security group {_named(first)} has no outbound rule for {ctx.traffic.rule} to {failure.address}.",
        fix=f"Add an outbound rule: {ctx.traffic.rule}, destination {failure.address}/32 (or the target's security group).",
    )


async def _dropped(ctx: _Context, failure: _Failure) -> list[DiagnoseHint]:
    """Packets that never got an answer."""
    traffic = ctx.traffic
    ids, subnet = await _locate(ctx, failure.address)
    where = failure.label
    source = ctx.source
    groups: dict[str, dict] = {}
    unreadable: str | None = None
    if ids:
        try:
            groups = await _read_groups(ctx.region, ids)
        except AwsError as exc:
            unreadable = (
                "Watchly's AWS credentials may not read them (ec2:DescribeNetworkInterfaces, "
                "ec2:DescribeSecurityGroups)"
                if exc.access_denied
                else str(exc)
            )
    allowed = None
    if groups and source:
        allowed = _inbound_allows(
            list(groups.values()), traffic, {g for g, _ in ctx.watchly_groups}, ctx.watchly_ip
        )

    if allowed is False:
        group = groups[ids[0]] if ids[0] in groups else next(iter(groups.values()))
        others = [g for g in ids if g != group["GroupId"]]
        also = f" Its other groups ({', '.join(others)}) have none either." if others else ""
        return [
            DiagnoseHint(
                kind="security_group",
                resource=group["GroupId"],
                detail=f"{where}'s security group {_named(group)} has no inbound rule for {traffic.rule} from {source}.{also}",
                fix=f"Add an inbound rule: {traffic.rule}, source {source}.",
            )
        ]

    if allowed is True:
        hints = []
        outbound = await _watchly_outbound(ctx, failure, set(ids))
        if outbound is not None:
            hints.append(outbound)
        scope = f"subnet {subnet}" if subnet else f"the subnet of {where}"
        hints.append(
            DiagnoseHint(
                kind="network_acl",
                resource=subnet or ctx.vpc.aws_vpc_id,
                detail=(
                    f"The security groups of {where} ({', '.join(ids)}) already allow {traffic.rule} from Watchly, "
                    "so something else is dropping it."
                ),
                fix=(
                    f"Look at the network ACLs of {scope} (inbound {traffic.rule} from Watchly, and outbound replies on "
                    "ports 1024-65535), then the instance's own firewall (ufw, iptables, firewalld, Windows Firewall)."
                    if traffic.protocol == "tcp"
                    else f"Look at the network ACLs of {scope} (inbound and outbound ICMP), then the instance's own "
                    "firewall; some images drop ICMP echo."
                ),
            )
        )
        if not ctx.same_vpc and not ctx.public:
            hints.append(
                DiagnoseHint(
                    kind="route",
                    resource=ctx.vpc.aws_vpc_id,
                    detail=f"Watchly runs outside {ctx.vpc.aws_vpc_id}, so its packets cross a peering or a transit gateway.",
                    fix="Check the route tables on both sides: each needs a route to the other's range through the peering or gateway.",
                )
            )
        return hints

    # No verdict: the groups could not be read or found, a prefix list might
    # cover Watchly, or it is not known where its connections come from.
    if unreadable:
        why = f"Watchly could not read the security groups of {where}: {unreadable}."
    elif not ids:
        why = f"Watchly could not find the security groups of {where}."
    elif not source:
        why = "Watchly could not tell where its connections come from (it does not run on EC2, or the instance metadata is off)."
    else:
        why = "A rule using a prefix list may or may not cover Watchly; its members are not read."
    names = ", ".join(ids) if ids else "the ones attached to it"
    return [
        DiagnoseHint(
            kind="security_group",
            resource=ids[0] if ids else (failure.address or failure.label),
            detail=why,
            fix=f"Open {names} and make sure one has an inbound rule: {traffic.rule}, source {source or 'the address Watchly connects from'}.",
        )
    ]


def _refused(ctx: _Context, failure: _Failure) -> DiagnoseHint:
    """The host answered with a refusal: reachable, so not a security group."""
    detail = ctx.resource.aws_detail or {}
    port = ctx.traffic.port
    known = sorted({row["port"] for row in detail.get("listeners") or [] if row.get("port")})
    if ctx.resource.kind is ResourceKind.LOAD_BALANCER and known:
        fix = f"The load balancer listens on {', '.join(map(str, known))}: use one of those ports, or add a listener for {port}."
    elif ctx.resource.kind is ResourceKind.DATABASE and detail.get("port"):
        fix = f"RDS says this database listens on {detail['port']}: use that port."
    else:
        fix = (
            f"Start the service, and make it listen on 0.0.0.0:{port} (or the private IP), not 127.0.0.1; "
            "or change the check's port to the one it does listen on."
        )
    where = f"{failure.address}:{port}" if failure.address else f"port {port}"
    return DiagnoseHint(
        kind="port",
        resource=where,
        detail=f"{failure.label} answered on {where} but refused: the host is reachable, so security groups are not the cause. Nothing listens on port {port}.",
        fix=fix,
    )


def _tls(ctx: _Context, failure: _Failure) -> DiagnoseHint:
    port = ctx.traffic.port
    return DiagnoseHint(
        kind="port",
        resource=f"{failure.address or failure.label}:{port}",
        detail=f"Port {port} of {failure.label} answers, but the TLS handshake failed: the port may not speak TLS.",
        fix=f"If the service on {port} is plain, turn TLS (or https) off for this check; otherwise point it at the port that serves TLS (often 443).",
    )


async def hints_for(
    check: AwsCheck, resource: AwsResource, vpc: AwsVpc, result: ProbeResult, target: ProbeTarget
) -> list[DiagnoseHint]:
    """Hints for a failed run of `check`; none when it passed, sends nothing
    to a port, or no hint applies. Never raises."""
    traffic = _traffic(check)
    if traffic is None:
        return []
    failures = _failures(resource, result, target)[:MAX_TARGETS]
    if not failures:
        return []
    try:
        watchly = await client.identity()
        address = target.address
        ctx = _Context(
            resource=resource,
            vpc=vpc,
            target=target,
            traffic=traffic,
            watchly=watchly,
            region=target.region,
            public=bool(check.settings.get("use_public_ip"))
            or (resource.aws_detail or {}).get("scheme") == "internet-facing"
            or (_is_ip(address) and not egress.is_private(address or "")),
        )
        hints: list[DiagnoseHint] = []
        seen: set[tuple[str, str]] = set()
        for failure in failures:
            if failure.error_type in CONNECT_FAILURES:
                found = await _dropped(ctx, failure)
            elif failure.error_type == "connect_refused" and traffic.protocol == "tcp":
                found = [_refused(ctx, failure)]
            elif failure.error_type == "tls_error" and traffic.protocol == "tcp":
                found = [_tls(ctx, failure)]
            else:
                found = []
            for hint in found:
                if (hint.kind, hint.resource) not in seen:
                    seen.add((hint.kind, hint.resource))
                    hints.append(hint)
        return hints
    except Exception:  # noqa: BLE001 - advice must never fail a diagnose
        logger.exception("Could not work out hints for %s", resource.name)
        return []
