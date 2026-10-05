"""The VPC map: what is in a VPC and how it connects, as
`GET /vpcs/{id}/topology` returns it.

Read from AWS (the discovery listing, cached as the add form's is; the subnets
and their route tables; each target group's target health) and laid over what
Watchly monitors there. A node that is a monitored resource carries its state,
and Watchly's paths go to it, coloured by its checks:

    internet ──▶ load balancer ──▶ target group ──▶ instance / IP target
                 Auto Scaling group ──registers──▶ target group
                 Auto Scaling group ──launches───▶ instance
    Watchly ──probes──▶ resource      (inside the VPC, or over the internet)
    Watchly ──▶ AWS API ──api──▶ target group / Auto Scaling group

Only `Describe*` calls. A listing AWS refuses leaves its part out of the map;
when AWS lists nothing at all, the monitored resources are drawn from what
Watchly last read.
"""

import asyncio
import ipaddress
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from app.core.instance_metadata import InstanceMetadata
from app.monitoring.infra.aws import client, discovery
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.discovery import Listing
from app.monitoring.infra.aws.models import (
    NETWORK_CHECKS,
    AwsResource,
    AwsVpc,
    CheckHealth,
    InfraCheckType,
    ResourceKind,
    ResourceState,
    is_public_path,
)
from app.monitoring.infra.aws.probes.target_health import target_group_name
from app.monitoring.infra.aws.schemas import Skipped, Topology, TopologyEdge, TopologyNode, TopologySubnet

#: How long subnets and target health are reused; the map polls every 30 s.
LIVE_TTL_SECONDS = 60
#: Target groups whose health one map reads at most.
MAX_TARGET_GROUPS = 50

#: (vpc id) -> (read at, (subnets, target health by group ARN, skipped)).
_live: dict[int, tuple[float, tuple[list[dict], dict[str, list[dict]], list[Skipped]]]] = {}

EdgeState = Literal["ok", "degraded", "failing", "unknown", "off"]

#: A target's state as an edge's.
TARGET_EDGE: dict[str, EdgeState] = {
    "healthy": "ok",
    "unhealthy": "failing",
    "unavailable": "failing",
    "initial": "degraded",
    "draining": "degraded",
    "unused": "off",
}
#: A check's health as an edge's.
CHECK_EDGE: dict[CheckHealth, EdgeState] = {
    CheckHealth.HEALTHY: "ok",
    CheckHealth.DEGRADED: "degraded",
    CheckHealth.DOWN: "failing",
    CheckHealth.UNKNOWN: "unknown",
}
_RANK: dict[str, int] = {"failing": 0, "degraded": 1, "unknown": 2, "ok": 3, "off": 4}

NODE_PREFIX = {
    ResourceKind.SERVER: "i",
    ResourceKind.LOAD_BALANCER: "lb",
    ResourceKind.AUTO_SCALING_GROUP: "asg",
    ResourceKind.DATABASE: "db",
}


def worst(states: list[EdgeState]) -> EdgeState:
    return min(states, key=_RANK.__getitem__) if states else "unknown"


def node_id(kind: ResourceKind, aws_id: str) -> str:
    """By AWS id: an instance id, a load balancer's ARN, a group's name, a
    database's identifier."""
    return f"{NODE_PREFIX[kind]}:{aws_id}"


@dataclass(slots=True)
class _Item:
    """One resource to draw, from AWS's listing or, failing that, from what
    Watchly last read of a monitored one."""

    kind: ResourceKind
    aws_id: str
    name: str
    detail: dict
    summary: str | None
    address: str | None
    aws_state: str | None
    public: bool
    key: str | None = None
    resource: AwsResource | None = None
    target_groups: list[dict] = field(default_factory=list)

    @property
    def id(self) -> str:
        return node_id(self.kind, self.aws_id)


def _from_resource(resource: AwsResource) -> _Item:
    detail = resource.aws_detail or {}
    if resource.kind is ResourceKind.LOAD_BALANCER:
        public = detail.get("scheme") == "internet-facing"
        summary = " · ".join(filter(None, [detail.get("type"), detail.get("scheme")]))
    elif resource.kind is ResourceKind.AUTO_SCALING_GROUP:
        public = any(m.get("public_ip") for m in detail.get("instances", []))
        summary = f"min {detail.get('min_size')}, max {detail.get('max_size')}, {detail.get('desired_capacity')} desired"
    elif resource.kind is ResourceKind.DATABASE:
        public = bool(detail.get("publicly_accessible"))
        summary = " · ".join(filter(None, [detail.get("engine"), detail.get("instance_class"), resource.aws_state]))
    else:
        public = bool(detail.get("public_ip"))
        summary = " · ".join(filter(None, [detail.get("instance_type"), resource.aws_state]))
    return _Item(
        kind=resource.kind,
        aws_id=resource.aws_id,
        name=resource.name,
        detail=detail,
        summary=summary or None,
        address=resource.address,
        aws_state=resource.aws_state,
        public=public,
        resource=resource,
        target_groups=detail.get("target_groups", []),
    )


async def _live_data(
    vpc: AwsVpc, target_group_arns: list[str], *, refresh: bool
) -> tuple[list[dict], dict[str, list[dict]], list[Skipped]]:
    hit = _live.get(vpc.id)
    if hit and not refresh and time.monotonic() - hit[0] < LIVE_TTL_SECONDS:
        return hit[1]
    region = client.Region.of(vpc)
    subnets, skipped = await discovery.subnet_rows(region, vpc.aws_vpc_id)
    arns = target_group_arns[:MAX_TARGET_GROUPS]
    answers = await asyncio.gather(
        *(client.call("elbv2", "describe_target_health", region, TargetGroupArn=arn) for arn in arns),
        return_exceptions=True,
    )
    health: dict[str, list[dict]] = {}
    refused = False
    for arn, answer in zip(arns, answers, strict=True):
        if isinstance(answer, AwsError):
            if not refused:
                reason = answer.code if answer.access_denied else str(answer)
                skipped.append(Skipped(service="elasticloadbalancing", call="DescribeTargetHealth", reason=reason))
                refused = True
            continue
        if isinstance(answer, BaseException):
            raise answer
        health[arn] = [
            {
                "id": (d.get("Target") or {}).get("Id"),
                "port": (d.get("Target") or {}).get("Port"),
                "state": (d.get("TargetHealth") or {}).get("State", "unknown"),
                "reason": (d.get("TargetHealth") or {}).get("Reason"),
                "description": (d.get("TargetHealth") or {}).get("Description"),
            }
            for d in answer.get("TargetHealthDescriptions", [])
        ]
    value = (subnets, health, skipped)
    _live[vpc.id] = (time.monotonic(), value)
    return value


def _group_health(targets: list[dict]) -> str:
    """A target group's health, from its targets'."""
    if not targets:
        return "empty"
    states = [t["state"] for t in targets if t["state"] != "unused"]
    if not states:
        # Every target stopped or in a zone the load balancer does not use.
        return "unknown"
    healthy = sum(1 for s in states if s == "healthy")
    failing = sum(1 for s in states if s in {"unhealthy", "unavailable"})
    if healthy == len(states):
        return "healthy"
    if failing == len(states):
        return "unhealthy"
    if failing:
        return "degraded"
    return "transition" if healthy == 0 else "degraded"


def _member_edge(member: dict) -> EdgeState:
    lifecycle = member.get("lifecycle_state") or ""
    if lifecycle == "InService":
        return "ok" if member.get("health_status") == "Healthy" else "failing"
    if lifecycle.startswith(("Pending", "Terminating", "Warmed")):
        return "degraded"
    return "unknown"


_EDGE_HEALTH = {"ok": "healthy", "failing": "unhealthy", "degraded": "transition"}


def _merged(edges: list[TopologyEdge]) -> list[TopologyEdge]:
    """One edge per path: two resources' `target_health` checks of one target
    group, say, are one AWS API edge, in the worse state, naming both."""
    merged: dict[tuple, TopologyEdge] = {}
    for edge in edges:
        key = (edge.source, edge.target, edge.kind, edge.via)
        first = merged.get(key)
        if first is None:
            merged[key] = edge.model_copy()
            continue
        first.state = worst([first.state, edge.state])
        for name in ("label", "detail"):
            parts = [p for p in (getattr(first, name), getattr(edge, name)) if p]
            setattr(first, name, " · ".join(dict.fromkeys(parts)) or None)
    return list(merged.values())


def _subnet_of(address: str | None, subnets: list[dict]) -> str | None:
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return None
    for subnet in subnets:
        try:
            if subnet["cidr"] and ip in ipaddress.ip_network(subnet["cidr"]):
                return subnet["id"]
        except ValueError:
            continue
    return None


async def build(
    vpc: AwsVpc,
    resources: list[AwsResource],
    listing: Listing | None,
    aws_error: str | None,
    identity: InstanceMetadata | None,
    in_vpc: bool,
    *,
    refresh: bool = False,
) -> Topology:
    """The map of `vpc`. `resources` are the monitored ones the caller can see."""
    monitored = {r.aws_id: r for r in resources}
    items: dict[str, _Item] = {}
    for info in listing.items if listing else []:
        item = _Item(
            kind=info.kind,
            aws_id=info.aws_id,
            name=info.name,
            detail=info.aws_detail,
            summary=info.summary,
            address=info.address,
            aws_state=info.aws_state,
            public=info.public,
            key=info.key,
            resource=monitored.get(info.aws_id),
            target_groups=info.aws_detail.get("target_groups", []),
        )
        items[item.id] = item
    # Monitored, but not in the listing: gone from AWS, or AWS listed nothing.
    for resource in resources:
        item = _from_resource(resource)
        items.setdefault(item.id, item)

    arns = sorted({tg["arn"] for item in items.values() for tg in item.target_groups if tg.get("arn")})
    skipped = list(listing.skipped) if listing else []
    subnets: list[dict] = []
    health: dict[str, list[dict]] = {}
    if aws_error is None:
        subnets, health, refused = await _live_data(vpc, arns, refresh=refresh)
        skipped.extend(s for s in refused if s not in skipped)
    known_subnets = {s["id"] for s in subnets}

    nodes: dict[str, TopologyNode] = {}
    edges: list[TopologyEdge] = []

    def subnet_for(detail: dict, address: str | None) -> str | None:
        subnet = detail.get("subnet_id")
        if subnet and (subnet in known_subnets or not subnets):
            return subnet
        return _subnet_of(address, subnets) or subnet

    for item in items.values():
        resource = item.resource
        detail = item.detail
        node = TopologyNode(
            id=item.id,
            kind={
                ResourceKind.SERVER: "instance",
                ResourceKind.LOAD_BALANCER: "load_balancer",
                ResourceKind.AUTO_SCALING_GROUP: "auto_scaling_group",
                ResourceKind.DATABASE: "database",
            }[item.kind],
            label=resource.name if resource else item.name,
            address=item.address if item.kind is not ResourceKind.AUTO_SCALING_GROUP else None,
            public_ip=detail.get("public_ip") if item.kind is ResourceKind.SERVER else None,
            aws_id=item.aws_id,
            aws_state=item.aws_state,
            detail=item.summary,
            public=item.public,
            key=item.key if resource is None else None,
            resource_id=resource.id if resource else None,
            state=resource.state if resource else None,
        )
        if item.kind is ResourceKind.SERVER:
            node.subnet = subnet_for(detail, item.address)
        elif item.kind is ResourceKind.DATABASE:
            # Its endpoint is a DNS name: the subnet comes from its subnet group.
            node.subnet = subnet_for(detail, None)
        nodes[node.id] = node

    # Auto Scaling groups: their instances, and their own health.
    for item in [i for i in items.values() if i.kind is ResourceKind.AUTO_SCALING_GROUP]:
        members = item.detail.get("instances", [])
        in_service = [m for m in members if m.get("lifecycle_state") == "InService"]
        healthy = [m for m in in_service if m.get("health_status") == "Healthy"]
        desired = item.detail.get("desired_capacity") or 0
        group = nodes[item.id]
        if not desired and not members:
            group.aws_health = "empty"
        elif len(healthy) >= desired and len(healthy) == len(in_service):
            group.aws_health = "healthy"
        elif not healthy:
            group.aws_health = "unhealthy"
        else:
            group.aws_health = "degraded"
        for member in members:
            ident = member.get("id")
            if not ident:
                continue
            target = f"i:{ident}"
            if target not in nodes:
                nodes[target] = TopologyNode(
                    id=target,
                    kind="instance",
                    label=ident,
                    address=member.get("private_ip"),
                    public_ip=member.get("public_ip"),
                    aws_id=ident,
                    detail=member.get("instance_type"),
                    subnet=_subnet_of(member.get("private_ip"), subnets),
                    public=bool(member.get("public_ip")),
                )
            state = _member_edge(member)
            nodes[target].aws_health = nodes[target].aws_health or _EDGE_HEALTH.get(state)
            edges.append(
                TopologyEdge(
                    source=item.id,
                    target=target,
                    kind="launches",
                    state=state,
                    label=member.get("lifecycle_state"),
                    detail=f"{member.get('lifecycle_state')} · {member.get('health_status')}",
                )
            )

    # A target group's health check, from whichever resource read it: an Auto
    # Scaling group's row has none when DescribeTargetGroups was refused.
    health_checks = {
        tg["arn"]: tg["health_check"]
        for item in items.values()
        for tg in item.target_groups
        if tg.get("arn") and tg.get("health_check")
    }
    group_state: dict[str, EdgeState] = {
        "healthy": "ok",
        "unhealthy": "failing",
        "degraded": "degraded",
        "transition": "degraded",
    }

    # Load balancers → target groups → targets, and the Auto Scaling groups
    # that register their instances in them.
    for item in items.values():
        for tg in item.target_groups:
            arn = tg.get("arn")
            if not arn:
                continue
            tg_id = f"tg:{arn}"
            targets = health.get(arn)
            if tg_id not in nodes:
                healthy = sum(1 for t in targets or [] if t["state"] == "healthy")
                unused = sum(1 for t in targets or [] if t["state"] == "unused")
                counted = (
                    f"{healthy} of {len(targets)} healthy" + (f", {unused} unused" if unused else "")
                    if targets is not None
                    else None
                )
                nodes[tg_id] = TopologyNode(
                    id=tg_id,
                    kind="target_group",
                    label=tg.get("name") or target_group_name(arn),
                    aws_id=arn,
                    detail=counted,
                    health_check=health_checks.get(arn),
                    aws_health=_group_health(targets) if targets is not None else None,
                )
                for target in targets or []:
                    ident = target.get("id") or ""
                    if ident.startswith("i-"):
                        target_id = f"i:{ident}"
                        if target_id not in nodes:
                            nodes[target_id] = TopologyNode(
                                id=target_id, kind="instance", label=ident, aws_id=ident,
                                detail="Not listed in this VPC",
                            )
                    elif ident.startswith("arn:"):
                        # A Lambda target: nothing in a subnet to draw.
                        continue
                    else:
                        target_id = f"ip:{ident}"
                        nodes.setdefault(
                            target_id,
                            TopologyNode(
                                id=target_id, kind="ip_target", label=ident, address=ident,
                                subnet=_subnet_of(ident, subnets), detail="IP target",
                            ),
                        )
                    state = TARGET_EDGE.get(target["state"], "unknown")
                    previous = nodes[target_id].aws_health
                    mine = _EDGE_HEALTH.get(state, "unknown")
                    # The worst word any target group has for it.
                    order = ["unhealthy", "degraded", "transition", "unknown", "healthy"]
                    if previous is None or order.index(mine) < order.index(previous):
                        nodes[target_id].aws_health = mine
                    reason = target.get("description") or target.get("reason")
                    edges.append(
                        TopologyEdge(
                            source=tg_id,
                            target=target_id,
                            kind="targets",
                            state=state,
                            label=f":{target['port']}" if target.get("port") else None,
                            detail=f"{target['state']}" + (f": {reason}" if reason else ""),
                        )
                    )
            state = group_state.get(nodes[tg_id].aws_health or "", "unknown")
            if item.kind is ResourceKind.LOAD_BALANCER:
                edges.append(
                    TopologyEdge(
                        source=item.id,
                        target=tg_id,
                        kind="forwards",
                        state=state,
                        label=f"{tg.get('protocol')}:{tg.get('port')}" if tg.get("protocol") else None,
                    )
                )
            elif item.kind is ResourceKind.AUTO_SCALING_GROUP:
                edges.append(
                    TopologyEdge(
                        source=item.id,
                        target=tg_id,
                        kind="registers",
                        state=state,
                        detail="Registers the instances it launches",
                    )
                )

    # Outside the VPC: the internet, Watchly, and the AWS API it reads.
    public_nodes = [n for n in nodes.values() if n.public and n.kind in {"load_balancer", "instance"}]
    for node in public_nodes:
        edges.append(
            TopologyEdge(
                source="internet",
                target=node.id,
                kind="internet",
                label=node.public_ip if node.kind == "instance" else "internet-facing",
            )
        )
    nodes["watchly"] = TopologyNode(
        id="watchly",
        kind="watchly",
        label="Watchly",
        address=identity.private_ip if identity else None,
        public_ip=identity.public_ip if identity else None,
        aws_id=identity.instance_id if identity else None,
        subnet=identity.subnet_id if in_vpc and identity else None,
        detail=(
            "Runs in this VPC"
            if in_vpc
            else f"Runs in {identity.vpc_id}: reaches this VPC through peering or a transit gateway"
            if identity
            else "Not on EC2: reaches this VPC only if this server's network routes to it"
        ),
    )
    nodes["aws_api"] = TopologyNode(
        id="aws_api",
        kind="aws_api",
        label=f"AWS API · {vpc.region}",
        detail=f"Read with {vpc.account.name}'s credentials",
        aws_health="unhealthy" if aws_error else "degraded" if skipped else "healthy",
    )
    edges.append(
        TopologyEdge(
            source="watchly",
            target="aws_api",
            kind="api",
            state="failing" if aws_error else "degraded" if skipped else "ok",
            detail=aws_error or ("; ".join(f"{s.call}: {s.reason}" for s in skipped[:3]) or None),
        )
    )

    # Watchly's checks of each monitored resource.
    for resource in resources:
        item_id = node_id(resource.kind, resource.aws_id)
        target = item_id if item_id in nodes else None
        if target is None:
            continue
        paused = resource.state in {ResourceState.PAUSED, ResourceState.MAINTENANCE, ResourceState.MISSING}
        by_path: dict[str, list] = {}
        for check in resource.checks:
            if not check.is_enabled:
                continue
            if check.check_type in NETWORK_CHECKS:
                via = "internet" if is_public_path(resource.kind, resource.aws_detail, check.settings) else "vpc"
                by_path.setdefault(via, []).append(check)
                continue
            # Read from the AWS API: target health, group health, a database's
            # status and metrics.
            if check.check_type is InfraCheckType.TARGET_HEALTH:
                api_target = f"tg:{check.settings.get('target_group_arn')}"
            else:
                api_target = item_id
            if api_target in nodes:
                edges.append(
                    TopologyEdge(
                        source="aws_api",
                        target=api_target,
                        kind="api",
                        state="off" if paused else CHECK_EDGE[check.health],
                        label=check.name,
                        detail=(check.last_result or {}).get("summary"),
                    )
                )
        for via, checks in by_path.items():
            states = [CHECK_EDGE[c.health] for c in checks]
            failing = [
                f"{c.name}: {(c.last_result or {}).get('summary')}"
                for c in checks
                if c.health is CheckHealth.DOWN and c.last_result
            ]
            edges.append(
                TopologyEdge(
                    source="watchly",
                    target=target,
                    kind="probes",
                    via=via,
                    state="off" if paused else worst(states),
                    label=" · ".join(c.name for c in checks),
                    detail="; ".join(failing) or None,
                )
            )

    if any(e.source == "internet" or e.via == "internet" for e in edges):
        nodes["internet"] = TopologyNode(id="internet", kind="internet", label="Internet")

    return Topology(
        vpc_id=vpc.id,
        read_at=datetime.now(UTC),
        watchly="in_vpc" if in_vpc else "outside" if identity else "not_on_ec2",
        subnets=[TopologySubnet(**s) for s in subnets],
        nodes=list(nodes.values()),
        edges=_merged(edges),
        skipped=skipped,
        aws_error=aws_error,
    )
