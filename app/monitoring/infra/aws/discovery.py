"""What is in a VPC, as AWS describes it: EC2 instances in public and private
subnets, Application and Network Load Balancers (internal or internet-facing)
with their target groups, EC2 Auto Scaling groups with their instances, and
RDS DB instances (Aurora's included).

Three uses: discovery (the add form's list, with suggested checks), the sync
that keeps each resource's address and state current, and looking one
resource up by its id when it is added by hand. All of it is `Describe*`
calls, which AWS does not bill.
"""

import ipaddress
import logging
import time
from dataclasses import dataclass, field

from app.monitoring.infra.aws import client, limits
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.models import InfraCheckType, ResourceKind
from app.monitoring.infra.aws.schemas import Skipped, Suggestion, TargetGroupBrief

logger = logging.getLogger(__name__)

#: EC2 states in which an instance is gone for good.
GONE = frozenset({"terminated", "shutting-down"})
#: Ports suggested for one server at most.
MAX_PORT_SUGGESTIONS = 5
#: Load balancer types watched, and how they are named.
LOAD_BALANCER_TYPES = {"application": "ALB", "network": "NLB"}
#: NLB listener protocols a `tcp` check can knock on.
TCP_LISTENERS = frozenset({"TCP", "TLS", "TCP_UDP"})
#: Ids per `DescribeInstances` or `DescribeTargetGroups` call.
BATCH = 100
#: RDS states in which a DB instance is gone for good.
DB_GONE = frozenset({"deleting", "deleted"})


@dataclass(slots=True)
class AwsResourceInfo:
    """One resource as AWS describes it."""

    kind: ResourceKind
    aws_id: str
    aws_arn: str | None
    name: str
    address: str | None
    aws_state: str | None
    #: What the resource's page shows: type, public IP, scheme, AZ, subnet,
    #: groups, tags.
    aws_detail: dict
    #: One line for the discovery list.
    summary: str
    #: An instance with a public IP, an internet-facing load balancer, or a
    #: group with an instance that has one.
    public: bool = False
    public_ip: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    subnet: str | None = None
    auto_scaling_group: str | None = None
    target_groups: list[TargetGroupBrief] = field(default_factory=list)

    @property
    def key(self) -> str:
        prefix = {
            ResourceKind.SERVER: "ec2",
            ResourceKind.LOAD_BALANCER: "elb",
            ResourceKind.AUTO_SCALING_GROUP: "asg",
            ResourceKind.DATABASE: "rds",
        }[self.kind]
        ident = self.name if self.kind is ResourceKind.LOAD_BALANCER else self.aws_id
        return f"{prefix}:{ident}"


@dataclass(slots=True)
class Listing:
    """Everything listed in one VPC, and the calls that were refused."""

    items: list[AwsResourceInfo] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    #: Kinds whose listing worked, so a resource not in it is really gone.
    listed_kinds: set[ResourceKind] = field(default_factory=set)


def _skip(skipped: list[Skipped], exc: AwsError) -> None:
    service, _, call = exc.operation.partition(":")
    reason = exc.code if exc.access_denied else str(exc)
    skipped.append(Skipped(service=service, call=_camel(call), reason=reason))


def _camel(operation: str) -> str:
    """`describe_target_groups` -> `DescribeTargetGroups`, as AWS names its actions."""
    return "".join(part.capitalize() for part in operation.split("_"))


async def _subnets(region: client.Region, aws_vpc_id: str) -> dict[str, dict]:
    subnets = await client.paginate(
        "ec2", "describe_subnets", region, "Subnets",
        Filters=[{"Name": "vpc-id", "Values": [aws_vpc_id]}],
    )
    return {
        s["SubnetId"]: {
            "label": client.tags(s.get("Tags")).get("Name") or s["SubnetId"],
            "cidr": s.get("CidrBlock"),
            "az": s.get("AvailabilityZone"),
        }
        for s in subnets
    }


def _routes_to_internet(table: dict) -> bool:
    return any(
        (route.get("GatewayId") or "").startswith("igw-")
        and (route.get("DestinationCidrBlock") == "0.0.0.0/0" or route.get("DestinationIpv6CidrBlock") == "::/0")
        for route in table.get("Routes", [])
    )


async def subnet_rows(region: client.Region, aws_vpc_id: str) -> tuple[list[dict], list[Skipped]]:
    """The VPC's subnets for the map, each public when its route table sends
    0.0.0.0/0 to an internet gateway. Without `ec2:DescribeRouteTables`,
    `public` is a guess from MapPublicIpOnLaunch: true, or None for unknown."""
    skipped: list[Skipped] = []
    try:
        subnets = await client.paginate(
            "ec2", "describe_subnets", region, "Subnets",
            Filters=[{"Name": "vpc-id", "Values": [aws_vpc_id]}],
        )
    except AwsError as exc:
        _skip(skipped, exc)
        return [], skipped
    public: dict[str, bool] | None = None
    try:
        tables = await client.paginate(
            "ec2", "describe_route_tables", region, "RouteTables",
            Filters=[{"Name": "vpc-id", "Values": [aws_vpc_id]}],
        )
        main = next(
            (t for t in tables if any(a.get("Main") for a in t.get("Associations", []))), None
        )
        public = {
            a["SubnetId"]: _routes_to_internet(t)
            for t in tables
            for a in t.get("Associations", [])
            if a.get("SubnetId")
        }
        # A subnet with no table of its own uses the VPC's main one.
        default = _routes_to_internet(main) if main else False
        public = {s["SubnetId"]: public.get(s["SubnetId"], default) for s in subnets}
    except AwsError as exc:
        _skip(skipped, exc)
    rows = [
        {
            "id": s["SubnetId"],
            "label": client.tags(s.get("Tags")).get("Name") or s["SubnetId"],
            "cidr": s.get("CidrBlock"),
            "az": s.get("AvailabilityZone"),
            "public": public[s["SubnetId"]] if public is not None else (True if s.get("MapPublicIpOnLaunch") else None),
        }
        for s in subnets
    ]
    return sorted(rows, key=lambda r: (r["public"] is not True, r["az"] or "", r["label"])), skipped


def _instance_info(instance: dict, region: client.Region, owner: str | None, subnets: dict) -> AwsResourceInfo:
    tags = client.tags(instance.get("Tags"))
    subnet = subnets.get(instance.get("SubnetId") or "", {})
    state = (instance.get("State") or {}).get("Name")
    asg = tags.get("aws:autoscaling:groupName")
    groups = [g["GroupId"] for g in instance.get("SecurityGroups", []) if "GroupId" in g]
    public_ip = instance.get("PublicIpAddress")
    detail = {
        "instance_type": instance.get("InstanceType"),
        "az": (instance.get("Placement") or {}).get("AvailabilityZone"),
        "subnet": subnet.get("label") or instance.get("SubnetId"),
        "subnet_id": instance.get("SubnetId"),
        "private_ip": instance.get("PrivateIpAddress"),
        "public_ip": public_ip,
        "public_dns": instance.get("PublicDnsName") or None,
        "security_groups": groups,
        "auto_scaling_group": asg,
        "tags": tags,
        # Their type, size and IOPS are added by `add_volume_details`.
        "volumes": [
            {"id": mapping["Ebs"]["VolumeId"], "device": mapping.get("DeviceName")}
            for mapping in instance.get("BlockDeviceMappings") or []
            if (mapping.get("Ebs") or {}).get("VolumeId")
        ],
    }
    summary = f"{instance.get('InstanceType')} · {state}"
    summary += f" · public {public_ip}" if public_ip else " · private"
    if asg:
        summary += f" · Auto Scaling group {asg}"
    return AwsResourceInfo(
        kind=ResourceKind.SERVER,
        aws_id=instance["InstanceId"],
        aws_arn=f"arn:aws:ec2:{region.name}:{owner}:instance/{instance['InstanceId']}" if owner else None,
        name=tags.get("Name") or instance["InstanceId"],
        address=instance.get("PrivateIpAddress"),
        aws_state=state,
        aws_detail=detail,
        summary=summary,
        public=bool(public_ip),
        public_ip=public_ip,
        tags=tags,
        subnet=detail["subnet"],
        auto_scaling_group=asg,
    )


async def add_volume_details(region: client.Region, infos: list[AwsResourceInfo]) -> None:
    """Each server's EBS volumes with their type, size, IOPS and throughput,
    from one `DescribeVolumes` per BATCH volumes: what `ec2_metrics` needs
    to tell a volume that bursts from one with IOPS to run out of. Raises
    AwsError; the volumes then keep only their id and device."""
    volumes = [v for info in infos if info.kind is ResourceKind.SERVER for v in info.aws_detail.get("volumes", [])]
    ids = list(dict.fromkeys(v["id"] for v in volumes))
    found: dict[str, dict] = {}
    for start in range(0, len(ids), BATCH):
        # A filter rather than VolumeIds, which fails whole for one volume
        # deleted since the instances were listed.
        rows = await client.paginate(
            "ec2", "describe_volumes", region, "Volumes",
            Filters=[{"Name": "volume-id", "Values": ids[start : start + BATCH]}],
        )
        for row in rows:
            found[row["VolumeId"]] = {
                "type": row.get("VolumeType"),
                "size_gb": row.get("Size"),
                "iops": row.get("Iops"),
                "throughput": row.get("Throughput"),
            }
    for volume in volumes:
        volume.update(found.get(volume["id"], {}))


async def _vpc_instances(region: client.Region, aws_vpc_id: str) -> list[tuple[dict, str | None]]:
    """The VPC's instances that are not gone, each with its owner account."""
    reservations = await client.paginate(
        "ec2", "describe_instances", region, "Reservations",
        Filters=[{"Name": "vpc-id", "Values": [aws_vpc_id]}],
    )
    return [
        (instance, reservation.get("OwnerId"))
        for reservation in reservations
        for instance in reservation.get("Instances", [])
        if (instance.get("State") or {}).get("Name") not in GONE
    ]


async def instances_by_id(region: client.Region, ids: list[str]) -> dict[str, dict]:
    """These instances as `DescribeInstances` has them. A filter rather than
    `InstanceIds`, so one that is already gone is left out instead of failing
    the call."""
    found: dict[str, dict] = {}
    ids = sorted({i for i in ids if i})
    for start in range(0, len(ids), BATCH):
        reservations = await client.paginate(
            "ec2", "describe_instances", region, "Reservations",
            Filters=[{"Name": "instance-id", "Values": ids[start : start + BATCH]}],
        )
        for reservation in reservations:
            for instance in reservation.get("Instances", []):
                found[instance["InstanceId"]] = instance
    return found


def _health_check_text(group: dict) -> str:
    protocol = group.get("HealthCheckProtocol") or ""
    path = group.get("HealthCheckPath") or ""
    port = group.get("HealthCheckPort") or "traffic-port"
    port = group.get("Port") if port == "traffic-port" else port
    matcher = (group.get("Matcher") or {}).get("HttpCode")
    text = f"{protocol} {path} on {port}".replace("  ", " ").strip()
    return f"{text}, {matcher}" if matcher else text


def _target_group_row(group: dict) -> dict:
    """A target group as a resource's `aws_detail` keeps it, with its health
    check: for an Auto Scaling group, the health API of its instances."""
    port = group.get("HealthCheckPort") or "traffic-port"
    return {
        "arn": group["TargetGroupArn"],
        "name": group.get("TargetGroupName"),
        "protocol": group.get("Protocol"),
        "port": group.get("Port"),
        "health_check_protocol": group.get("HealthCheckProtocol"),
        "health_check_port": group.get("Port") if port == "traffic-port" else int(port),
        "health_check_path": group.get("HealthCheckPath"),
        "health_check_matcher": (group.get("Matcher") or {}).get("HttpCode"),
        "health_check": _health_check_text(group),
    }


def _target_group_name(arn: str) -> str:
    """`.../targetgroup/orders-api-tg/73e2d6bc24d8a067` -> `orders-api-tg`."""
    parts = arn.split("targetgroup/", 1)
    return parts[1].split("/", 1)[0] if len(parts) == 2 else arn


def _listener_row(listener: dict) -> dict:
    """A listener as `aws_detail` keeps it: with its TLS policy, and whether
    an HTTP one sends everyone to HTTPS."""
    redirect = next(
        (
            action.get("RedirectConfig") or {}
            for action in listener.get("DefaultActions", [])
            if action.get("Type") == "redirect"
        ),
        None,
    )
    return {
        "protocol": listener.get("Protocol"),
        "port": listener.get("Port"),
        "ssl_policy": listener.get("SslPolicy"),
        "redirects_to_https": redirect is not None and redirect.get("Protocol") == "HTTPS",
    }


def _first_key(value: dict | None) -> str | None:
    """`{"Block": {}}` -> `block`: how WAF spells an action."""
    if not value:
        return None
    return next(iter(value)).lower()


def _waf_rule_row(rule: dict) -> dict:
    """One Web ACL rule, flattened to what `edge_security` grades: which
    managed or own rule group it runs, or what kind of rule it is, and
    whether it only counts."""
    statement = rule.get("Statement") or rule.get("FirewallManagerStatement") or {}
    row = {
        "name": rule.get("Name"),
        "priority": rule.get("Priority"),
        "kind": "custom",
        "vendor": None,
        "group": None,
        "version": None,
        "action": _first_key(rule.get("Action")),
        "count_override": "none",
        "counted_rules": [],
        "limit": None,
    }
    group = statement.get("ManagedRuleGroupStatement") or statement.get("RuleGroupReferenceStatement")
    if group is not None:
        managed = "ManagedRuleGroupStatement" in statement
        counted = [
            item.get("Name")
            for item in group.get("RuleActionOverrides", [])
            if "Count" in (item.get("ActionToUse") or {})
        ] + [item.get("Name") for item in group.get("ExcludedRules", [])]
        whole = "Count" in (rule.get("OverrideAction") or {})
        row.update(
            kind="managed" if managed else "group",
            vendor=group.get("VendorName") if managed else None,
            # `.../regional/rulegroup/<name>/<id>` for a group of the account's own.
            group=group.get("Name") if managed else group.get("ARN", "").split("/")[-2:][0],
            version=group.get("Version"),
            # A rule group's own rules decide, unless the whole group counts.
            action="count" if whole else "enforce",
            count_override="all" if whole else ("partial" if counted else "none"),
            counted_rules=counted[:20],
        )
    elif "RateBasedStatement" in statement:
        row.update(kind="rate", limit=statement["RateBasedStatement"].get("Limit"))
    elif "GeoMatchStatement" in statement or "GeoMatchStatement" in (
        statement.get("NotStatement") or {}
    ).get("Statement", {}):
        row["kind"] = "geo"
    elif "IPSetReferenceStatement" in statement:
        row["kind"] = "ip_set"
    return row


async def _waf_detail(arn: str, region: client.Region, skipped: list[Skipped]) -> dict:
    """The regional Web ACL in front of an ALB, as `aws_detail["waf"]` keeps
    it: `{"acl": None}` when there is none, `{"error": code}` when AWS would
    not say."""
    try:
        response = await client.call("wafv2", "get_web_acl_for_resource", region, ResourceArn=arn)
    except AwsError as exc:
        if exc.code == "WAFNonexistentItemException":
            return {"acl": None}
        _skip(skipped, exc)
        return {"error": exc.code}
    acl = response.get("WebACL")
    if not acl:
        return {"acl": None}
    logging_on: bool | None
    try:
        config = await client.call(
            "wafv2", "get_logging_configuration", region, ResourceArn=acl["ARN"]
        )
        logging_on = bool((config.get("LoggingConfiguration") or {}).get("LogDestinationConfigs"))
    except AwsError as exc:
        if exc.code == "WAFNonexistentItemException":
            logging_on = False
        else:
            _skip(skipped, exc)
            logging_on = None
    rules = [
        *acl.get("PreProcessFirewallManagerRuleGroups", []),
        *acl.get("Rules", []),
        *acl.get("PostProcessFirewallManagerRuleGroups", []),
    ]
    return {
        "acl": {
            "name": acl.get("Name"),
            "arn": acl.get("ARN"),
            "default_action": _first_key(acl.get("DefaultAction")),
            "capacity": acl.get("Capacity"),
            "firewall_manager": bool(acl.get("ManagedByFirewallManager")),
            "logging": logging_on,
            "rules": [_waf_rule_row(rule) for rule in rules],
        }
    }


#: ALB attributes `edge_security` grades, and what `aws_detail` calls them.
LB_ATTRIBUTES = {
    "waf.fail_open.enabled": "waf_fail_open",
    "routing.http.drop_invalid_header_fields.enabled": "drop_invalid_headers",
    "routing.http.desync_mitigation_mode": "desync_mode",
    "deletion_protection.enabled": "deletion_protection",
    "access_logs.s3.enabled": "access_logs",
}


async def _lb_attributes(arn: str, region: client.Region, skipped: list[Skipped]) -> dict | None:
    try:
        response = await client.call(
            "elbv2", "describe_load_balancer_attributes", region, LoadBalancerArn=arn
        )
    except AwsError as exc:
        _skip(skipped, exc)
        return None
    found = {}
    for item in response.get("Attributes", []):
        name = LB_ATTRIBUTES.get(item.get("Key"))
        if name is not None:
            value = item.get("Value")
            found[name] = value == "true" if value in ("true", "false") else value
    return found


async def _load_balancer_info(
    lb: dict, region: client.Region, *, count_targets: bool
) -> tuple[AwsResourceInfo, list[Skipped]]:
    skipped: list[Skipped] = []
    arn = lb["LoadBalancerArn"]
    target_groups: list[dict] = []
    listeners: list[dict] = []
    try:
        target_groups = await client.paginate(
            "elbv2", "describe_target_groups", region, "TargetGroups", LoadBalancerArn=arn
        )
    except AwsError as exc:
        _skip(skipped, exc)
    try:
        listeners = await client.paginate(
            "elbv2", "describe_listeners", region, "Listeners", LoadBalancerArn=arn
        )
    except AwsError as exc:
        _skip(skipped, exc)

    briefs = []
    for group in target_groups:
        count = None
        if count_targets:
            try:
                health = await client.call(
                    "elbv2", "describe_target_health", region, TargetGroupArn=group["TargetGroupArn"]
                )
                count = len(health.get("TargetHealthDescriptions", []))
            except AwsError as exc:
                _skip(skipped, exc)
        briefs.append(
            TargetGroupBrief(
                arn=group["TargetGroupArn"],
                name=group.get("TargetGroupName", ""),
                targets=count,
                health_check=_health_check_text(group),
            )
        )
    listener_rows = sorted(
        (_listener_row(item) for item in listeners),
        key=lambda row: (row["protocol"] != "HTTP", row["port"] or 0),
    )
    scheme = lb.get("Scheme")
    detail = {
        "scheme": scheme,
        "type": lb.get("Type"),
        "listeners": listener_rows,
        "target_groups": [_target_group_row(group) for group in target_groups],
        "security_groups": lb.get("SecurityGroups", []),
        "azs": [z.get("ZoneName") for z in lb.get("AvailabilityZones", [])],
    }
    if lb.get("Type") == "application":
        # What `edge_security` grades: WAF attaches to ALBs only.
        detail["waf"] = await _waf_detail(arn, region, skipped)
        detail["attributes"] = await _lb_attributes(arn, region, skipped)
    first = listener_rows[0] if listener_rows else None
    summary = f"{LOAD_BALANCER_TYPES.get(lb.get('Type'), lb.get('Type'))} · {scheme}"
    if first:
        summary += f" · {first['protocol']}:{first['port']}"
    summary += f" · {len(target_groups)} target group{'s' if len(target_groups) != 1 else ''}"
    info = AwsResourceInfo(
        kind=ResourceKind.LOAD_BALANCER,
        aws_id=arn,
        aws_arn=arn,
        name=lb.get("LoadBalancerName") or arn,
        address=lb.get("DNSName"),
        aws_state=(lb.get("State") or {}).get("Code"),
        aws_detail=detail,
        summary=summary,
        public=scheme == "internet-facing",
        target_groups=briefs,
    )
    return info, skipped


def _is_watched_lb(lb: dict, aws_vpc_id: str) -> bool:
    """An ALB or NLB of this VPC, internal or internet-facing; not a Gateway
    Load Balancer."""
    return lb.get("VpcId") == aws_vpc_id and lb.get("Type") in LOAD_BALANCER_TYPES


async def list_load_balancers(
    region: client.Region, aws_vpc_id: str, *, count_targets: bool
) -> tuple[list[AwsResourceInfo], list[Skipped]]:
    balancers = await client.paginate(
        "elbv2", "describe_load_balancers", region, "LoadBalancers"
    )
    items, skipped = [], []
    for lb in balancers:
        if not _is_watched_lb(lb, aws_vpc_id):
            continue
        info, refused = await _load_balancer_info(lb, region, count_targets=count_targets)
        items.append(info)
        skipped.extend(refused)
    return items, skipped


def member_rows(group: dict, instances: dict[str, dict]) -> list[dict]:
    """An Auto Scaling group's instances, with the addresses EC2 gives them."""
    rows = []
    for member in group.get("Instances", []):
        raw = instances.get(member.get("InstanceId") or "", {})
        launched = raw.get("LaunchTime")
        rows.append(
            {
                "id": member.get("InstanceId"),
                "az": member.get("AvailabilityZone"),
                "lifecycle_state": member.get("LifecycleState"),
                "health_status": member.get("HealthStatus"),
                "instance_type": member.get("InstanceType") or raw.get("InstanceType"),
                "private_ip": raw.get("PrivateIpAddress"),
                "public_ip": raw.get("PublicIpAddress"),
                "launch_time": launched.isoformat() if hasattr(launched, "isoformat") else launched,
            }
        )
    return sorted(rows, key=lambda row: row["id"] or "")


def in_service_ids(aws_detail: dict | None) -> list[str]:
    """Ids of the instances an Auto Scaling group has in service, sorted."""
    return sorted(
        m["id"] for m in (aws_detail or {}).get("instances", []) if m.get("id") and m.get("lifecycle_state") == "InService"
    )


def _group_subnets(group: dict) -> list[str]:
    return [s.strip() for s in (group.get("VPCZoneIdentifier") or "").split(",") if s.strip()]


def _group_info(group: dict, instances: dict[str, dict], target_groups: dict[str, dict], subnets: dict) -> AwsResourceInfo:
    name = group["AutoScalingGroupName"]
    tags = client.tags(group.get("Tags"))
    members = member_rows(group, instances)
    in_service = [m for m in members if m["lifecycle_state"] == "InService"]
    healthy = [m for m in in_service if m["health_status"] == "Healthy"]
    desired = group.get("DesiredCapacity") or 0
    subnet_ids = _group_subnets(group)
    template = group.get("LaunchTemplate") or (
        ((group.get("MixedInstancesPolicy") or {}).get("LaunchTemplate") or {}).get("LaunchTemplateSpecification")
        or {}
    )
    groups = [
        _target_group_row(target_groups[arn]) if arn in target_groups else {"arn": arn, "name": _target_group_name(arn)}
        for arn in group.get("TargetGroupARNs", [])
    ]
    detail = {
        "min_size": group.get("MinSize"),
        "max_size": group.get("MaxSize"),
        "desired_capacity": desired,
        "health_check_type": group.get("HealthCheckType"),
        "health_check_grace_period": group.get("HealthCheckGracePeriod"),
        "launch_template": (
            f"{template['LaunchTemplateName']} ({template.get('Version') or '$Default'})"
            if template.get("LaunchTemplateName")
            else group.get("LaunchConfigurationName")
        ),
        "subnets": [subnets.get(s, {}).get("label") or s for s in subnet_ids],
        "subnet_ids": subnet_ids,
        "azs": group.get("AvailabilityZones", []),
        "target_groups": groups,
        "instances": members,
        "status": group.get("Status"),
        "tags": tags,
    }
    summary = f"{len(healthy)} of {desired} healthy · min {group.get('MinSize')}, max {group.get('MaxSize')}"
    summary += f" · {group.get('HealthCheckType') or 'EC2'} health check"
    if groups:
        summary += f" · {len(groups)} target group{'s' if len(groups) != 1 else ''}"
    return AwsResourceInfo(
        kind=ResourceKind.AUTO_SCALING_GROUP,
        aws_id=name,
        aws_arn=group.get("AutoScalingGroupARN"),
        name=name,
        address=None,
        # Deleting groups have a Status; the rest say how full they are.
        aws_state=(group.get("Status") or f"{len(in_service)} of {desired} in service")[:64],
        aws_detail=detail,
        summary=summary,
        public=any(m["public_ip"] for m in members),
        tags=tags,
    )


async def _groups_info(
    region: client.Region, groups: list[dict], subnets: dict, instances: dict[str, dict]
) -> tuple[list[AwsResourceInfo], list[Skipped]]:
    """`groups` as resources, with their target groups' health checks and
    their instances' addresses; `instances` saves describing those again."""
    skipped: list[Skipped] = []
    target_groups: dict[str, dict] = {}
    arns = sorted({arn for group in groups for arn in group.get("TargetGroupARNs", [])})
    try:
        for start in range(0, len(arns), 20):
            response = await client.call(
                "elbv2", "describe_target_groups", region, TargetGroupArns=arns[start : start + 20]
            )
            target_groups.update({g["TargetGroupArn"]: g for g in response.get("TargetGroups", [])})
    except AwsError as exc:
        _skip(skipped, exc)
    missing = [
        m["InstanceId"] for group in groups for m in group.get("Instances", []) if m.get("InstanceId") not in instances
    ]
    if missing:
        try:
            instances = {**instances, **await instances_by_id(region, missing)}
        except AwsError as exc:
            _skip(skipped, exc)
    return [_group_info(group, instances, target_groups, subnets) for group in groups], skipped


async def list_groups(
    region: client.Region, vpc_subnets: set[str], subnets: dict, instances: dict[str, dict]
) -> tuple[list[AwsResourceInfo], list[Skipped]]:
    """The Auto Scaling groups that launch into this VPC's subnets."""
    groups = await client.paginate(
        "autoscaling", "describe_auto_scaling_groups", region, "AutoScalingGroups"
    )
    mine = [group for group in groups if vpc_subnets.intersection(_group_subnets(group))]
    if not mine:
        return [], []
    return await _groups_info(region, mine, subnets, instances)


def _db_subnet(db: dict, subnets: dict) -> tuple[str | None, str | None]:
    """The subnet of the DB subnet group in the instance's AZ, and its label.
    RDS does not say which subnet the instance is in; a subnet group has one
    per AZ as a rule."""
    az = db.get("AvailabilityZone")
    for subnet in (db.get("DBSubnetGroup") or {}).get("Subnets", []):
        if (subnet.get("SubnetAvailabilityZone") or {}).get("Name") == az and subnet.get("SubnetIdentifier"):
            ident = subnet["SubnetIdentifier"]
            return ident, subnets.get(ident, {}).get("label") or ident
    return None, None


def _db_info(db: dict, subnets: dict) -> AwsResourceInfo:
    ident = db["DBInstanceIdentifier"]
    tags = client.tags(db.get("TagList"))
    endpoint = db.get("Endpoint") or {}
    subnet_id, subnet_label = _db_subnet(db, subnets)
    engine = db.get("Engine")
    detail = {
        "engine": engine,
        "engine_version": db.get("EngineVersion"),
        "instance_class": db.get("DBInstanceClass"),
        "endpoint": endpoint.get("Address"),
        "port": endpoint.get("Port") or db.get("DbInstancePort") or None,
        "multi_az": bool(db.get("MultiAZ")),
        "az": db.get("AvailabilityZone"),
        "secondary_az": db.get("SecondaryAvailabilityZone"),
        "subnet": subnet_label,
        "subnet_id": subnet_id,
        "subnet_group": (db.get("DBSubnetGroup") or {}).get("DBSubnetGroupName"),
        "publicly_accessible": bool(db.get("PubliclyAccessible")),
        "storage_type": db.get("StorageType"),
        "allocated_storage_gb": db.get("AllocatedStorage"),
        "max_allocated_storage_gb": db.get("MaxAllocatedStorage"),
        # Provisioned (io1, io2, gp3); RDS gives none for gp2.
        "iops": db.get("Iops"),
        "storage_throughput": db.get("StorageThroughput"),
        "parameter_group": next(
            (g.get("DBParameterGroupName") for g in db.get("DBParameterGroups") or [] if g.get("DBParameterGroupName")),
            None,
        ),
        # Added by `add_connection_limit`.
        "max_connections": None,
        "max_connections_note": None,
        "cluster": db.get("DBClusterIdentifier"),
        "replica_of": db.get("ReadReplicaSourceDBInstanceIdentifier"),
        "replicas": db.get("ReadReplicaDBInstanceIdentifiers") or [],
        "security_groups": [
            g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", []) if g.get("VpcSecurityGroupId")
        ],
        "tags": tags,
    }
    summary = " · ".join(
        filter(
            None,
            [
                f"{engine} {db.get('EngineVersion') or ''}".strip(),
                db.get("DBInstanceClass"),
                "Multi-AZ" if detail["multi_az"] else None,
                f"Aurora cluster {detail['cluster']}" if detail["cluster"] else None,
                f"replica of {detail['replica_of']}" if detail["replica_of"] else None,
                db.get("DBInstanceStatus"),
            ],
        )
    )
    return AwsResourceInfo(
        kind=ResourceKind.DATABASE,
        aws_id=ident,
        aws_arn=db.get("DBInstanceArn"),
        name=ident,
        address=endpoint.get("Address"),
        aws_state=db.get("DBInstanceStatus"),
        aws_detail=detail,
        summary=summary,
        public=detail["publicly_accessible"],
        tags=tags,
        subnet=subnet_label,
    )


def _db_in_vpc(db: dict, aws_vpc_id: str) -> bool:
    return (db.get("DBSubnetGroup") or {}).get("VpcId") == aws_vpc_id and db.get("DBInstanceStatus") not in DB_GONE


async def add_connection_limit(region: client.Region, db: dict, info: AwsResourceInfo) -> None:
    """The database's `max_connections`, for `db_metrics` to measure its
    connections against, and where it came from or why it is not known."""
    limit, note = await limits.max_connections(region, db)
    info.aws_detail["max_connections"] = limit
    info.aws_detail["max_connections_note"] = note


async def list_databases(region: client.Region, aws_vpc_id: str, subnets: dict) -> list[AwsResourceInfo]:
    """The RDS DB instances in this VPC's subnets."""
    databases = await client.paginate("rds", "describe_db_instances", region, "DBInstances")
    infos = []
    for db in databases:
        if _db_in_vpc(db, aws_vpc_id):
            info = _db_info(db, subnets)
            await add_connection_limit(region, db, info)
            infos.append(info)
    return infos


async def read_database(region: client.Region, ident: str) -> dict | None:
    """A DB instance as RDS has it now; None once it is gone. Raises AwsError."""
    try:
        response = await client.call("rds", "describe_db_instances", region, DBInstanceIdentifier=ident)
    except AwsError as exc:
        if exc.code in {"DBInstanceNotFound", "DBInstanceNotFoundFault"}:
            return None
        raise
    found = response.get("DBInstances") or []
    return found[0] if found else None


async def read_group(region: client.Region, name: str, *, with_addresses: bool) -> tuple[dict, list[dict]] | None:
    """An Auto Scaling group as AWS has it now, and its instances (with their
    addresses, if asked); None once it is gone. Raises AwsError."""
    response = await client.call(
        "autoscaling", "describe_auto_scaling_groups", region, AutoScalingGroupNames=[name]
    )
    groups = response.get("AutoScalingGroups") or []
    if not groups:
        return None
    group = groups[0]
    instances: dict[str, dict] = {}
    if with_addresses:
        instances = await instances_by_id(region, [m.get("InstanceId") for m in group.get("Instances", [])])
    return group, member_rows(group, instances)


async def list_vpc(region: client.Region, aws_vpc_id: str, *, count_targets: bool = False) -> Listing:
    """Every resource in the VPC. A listing AWS refuses is noted in
    `skipped` and the rest carries on."""
    listing = Listing()
    try:
        subnets = await _subnets(region, aws_vpc_id)
    except AwsError as exc:
        _skip(listing.skipped, exc)
        subnets = {}
    raw_instances: dict[str, dict] = {}
    try:
        for instance, owner in await _vpc_instances(region, aws_vpc_id):
            raw_instances[instance["InstanceId"]] = instance
            listing.items.append(_instance_info(instance, region, owner, subnets))
        listing.listed_kinds.add(ResourceKind.SERVER)
    except AwsError as exc:
        _skip(listing.skipped, exc)
    if raw_instances:
        try:
            await add_volume_details(region, listing.items)
        except AwsError as exc:
            _skip(listing.skipped, exc)
    try:
        balancers, refused = await list_load_balancers(region, aws_vpc_id, count_targets=count_targets)
        listing.items.extend(balancers)
        listing.skipped.extend(refused)
        listing.listed_kinds.add(ResourceKind.LOAD_BALANCER)
    except AwsError as exc:
        _skip(listing.skipped, exc)
    # The subnets that failed to list are still known from the instances.
    vpc_subnets = set(subnets) | {i.get("SubnetId") for i in raw_instances.values() if i.get("SubnetId")}
    try:
        groups, refused = await list_groups(region, vpc_subnets, subnets, raw_instances)
        listing.items.extend(groups)
        listing.skipped.extend(refused)
        listing.listed_kinds.add(ResourceKind.AUTO_SCALING_GROUP)
    except AwsError as exc:
        _skip(listing.skipped, exc)
    try:
        listing.items.extend(await list_databases(region, aws_vpc_id, subnets))
        listing.listed_kinds.add(ResourceKind.DATABASE)
    except AwsError as exc:
        _skip(listing.skipped, exc)
    return listing


async def describe_one(
    region: client.Region, aws_vpc_id: str, kind: ResourceKind, aws_id: str
) -> AwsResourceInfo | None:
    """The resource with this id, if it is in this VPC; None otherwise.
    Raises AwsError when AWS cannot be asked."""
    if kind is ResourceKind.SERVER:
        try:
            response = await client.call("ec2", "describe_instances", region, InstanceIds=[aws_id])
        except AwsError as exc:
            if exc.code in {"InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"}:
                return None
            raise
        subnets = await _subnets(region, aws_vpc_id)
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                if instance.get("VpcId") == aws_vpc_id and (instance.get("State") or {}).get("Name") not in GONE:
                    info = _instance_info(instance, region, reservation.get("OwnerId"), subnets)
                    try:
                        await add_volume_details(region, [info])
                    except AwsError as exc:
                        logger.info("Volumes of %s not described: %s", aws_id, exc)
                    return info
        return None

    if kind is ResourceKind.DATABASE:
        db = await read_database(region, aws_id)
        if db is None or not _db_in_vpc(db, aws_vpc_id):
            return None
        info = _db_info(db, await _subnets(region, aws_vpc_id))
        await add_connection_limit(region, db, info)
        return info

    if kind is ResourceKind.AUTO_SCALING_GROUP:
        response = await client.call(
            "autoscaling", "describe_auto_scaling_groups", region, AutoScalingGroupNames=[aws_id]
        )
        groups = response.get("AutoScalingGroups") or []
        if not groups:
            return None
        subnets = await _subnets(region, aws_vpc_id)
        if not set(subnets).intersection(_group_subnets(groups[0])):
            return None
        infos, _ = await _groups_info(region, groups[:1], subnets, {})
        return infos[0]

    lookup = {"LoadBalancerArns": [aws_id]} if aws_id.startswith("arn:") else {"Names": [aws_id]}
    try:
        response = await client.call("elbv2", "describe_load_balancers", region, **lookup)
    except AwsError as exc:
        if exc.code in {"LoadBalancerNotFound", "LoadBalancerNotFoundException", "ValidationError"}:
            return None
        raise
    for lb in response.get("LoadBalancers", []):
        if _is_watched_lb(lb, aws_vpc_id):
            info, _ = await _load_balancer_info(lb, region, count_targets=False)
            return info
    return None


# --- suggestions --------------------------------------------------------------


def rule_from_watchly(rule: dict, watchly_groups: set[str], watchly_ip: str | None) -> bool:
    """Whether an inbound security group rule names Watchly as a source: one
    of its security groups, or a range holding its address."""
    if any(pair.get("GroupId") in watchly_groups for pair in rule.get("UserIdGroupPairs", [])):
        return True
    if not watchly_ip:
        return False
    address = ipaddress.ip_address(watchly_ip)
    return any(
        address in ipaddress.ip_network(r["CidrIp"], strict=False)
        for r in rule.get("IpRanges", [])
        if r.get("CidrIp")
    )


def _ports_open_to(groups: list[dict], watchly_groups: set[str], watchly_ip: str | None) -> tuple[bool, list[int]]:
    """Whether ICMP is allowed from Watchly by these security groups, and
    which TCP ports are."""
    icmp, ports = False, []
    for group in groups:
        for rule in group.get("IpPermissions", []):
            if not rule_from_watchly(rule, watchly_groups, watchly_ip):
                continue
            protocol = str(rule.get("IpProtocol"))
            if protocol in {"-1", "all"}:
                icmp = True
                ports.append(22)
            elif protocol in {"icmp", "1"}:
                icmp = True
            elif protocol in {"tcp", "6"} and rule.get("FromPort") is not None:
                ports.append(int(rule["FromPort"]))
    return icmp, sorted(set(ports))[:MAX_PORT_SUGGESTIONS]


def suggest(
    info: AwsResourceInfo,
    *,
    security_groups: dict[str, dict] | None = None,
    watchly_groups: set[str] | None = None,
    watchly_ip: str | None = None,
) -> list[Suggestion]:
    """The checks worth adding for a resource. A server's go to its private
    IP; sending them to its public IP is the caller's choice."""
    if info.kind is ResourceKind.AUTO_SCALING_GROUP:
        return _suggest_for_group(info)
    if info.kind is ResourceKind.DATABASE:
        return _suggest_for_database(info)
    if info.kind is ResourceKind.SERVER:
        if security_groups is not None and watchly_groups:
            groups = [security_groups[g] for g in info.aws_detail.get("security_groups", []) if g in security_groups]
            icmp, ports = _ports_open_to(groups, watchly_groups, watchly_ip)
        else:
            icmp, ports = True, [22]
        suggestions = []
        if icmp:
            suggestions.append(Suggestion(check_type=InfraCheckType.PING, name="ping", settings={}))
        for port in ports:
            suggestions.append(
                Suggestion(check_type=InfraCheckType.TCP, name=f"tcp {port}", settings={"port": port})
            )
        if not suggestions:
            suggestions.append(Suggestion(check_type=InfraCheckType.PING, name="ping", settings={}))
        # Asks CloudWatch, so it works whatever the security groups allow.
        suggestions.append(Suggestion(check_type=InfraCheckType.EC2_METRICS, name="server metrics", settings={}))
        return suggestions

    suggestions = []
    listeners = info.aws_detail.get("listeners") or []
    groups = info.aws_detail.get("target_groups") or []
    if info.aws_detail.get("type") == "network":
        listener = next((item for item in listeners if item.get("protocol") in TCP_LISTENERS), None)
        if listener and listener.get("port"):
            suggestions.append(
                Suggestion(
                    check_type=InfraCheckType.TCP,
                    name=f"tcp {listener['port']}",
                    settings={"port": listener["port"], "tls": listener.get("protocol") == "TLS"},
                )
            )
    elif listeners:
        first = listeners[0]
        scheme = "https" if first.get("protocol") == "HTTPS" else "http"
        path = next((g.get("health_check_path") for g in groups if g.get("health_check_path")), None) or "/"
        suggestions.append(
            Suggestion(
                check_type=InfraCheckType.HTTP,
                name=f"GET {path}",
                settings={"scheme": scheme, "port": first.get("port"), "path": path},
            )
        )
    for group in groups:
        suggestions.append(
            Suggestion(
                check_type=InfraCheckType.TARGET_HEALTH,
                name=f"targets of {group.get('name')}",
                settings={"target_group_arn": group["arn"]},
            )
        )
    return suggestions


def _suggest_for_group(info: AwsResourceInfo) -> list[Suggestion]:
    """The group's own health, then its instances' health API, as its target
    groups' health checks call it, on every instance; one `target_health`
    per target group. Without a target group, a ping of every instance."""
    suggestions = [Suggestion(check_type=InfraCheckType.GROUP_HEALTH, name="group health", settings={})]
    groups = info.aws_detail.get("target_groups") or []
    seen: set[tuple] = set()
    for group in groups:
        protocol = (group.get("health_check_protocol") or "").upper()
        port = group.get("health_check_port")
        if not port:
            continue
        if protocol in {"HTTP", "HTTPS"}:
            path = group.get("health_check_path") or "/"
            matcher = str(group.get("health_check_matcher") or "")
            settings = {"scheme": protocol.lower(), "port": port, "path": path}
            if matcher.isdigit():
                settings["expected_status"] = int(matcher)
            key = (protocol, port, path)
            name = f"GET {path} on each instance"
            check_type = InfraCheckType.HTTP
        elif protocol in {"TCP", "TLS"}:
            settings = {"port": port, "tls": protocol == "TLS"}
            key = (protocol, port)
            name = f"tcp {port} on each instance"
            check_type = InfraCheckType.TCP
        else:
            continue
        if key not in seen:
            seen.add(key)
            suggestions.append(Suggestion(check_type=check_type, name=name, settings=settings))
    if not seen:
        suggestions.append(Suggestion(check_type=InfraCheckType.PING, name="ping each instance", settings={}))
    for group in groups:
        if group.get("arn"):
            suggestions.append(
                Suggestion(
                    check_type=InfraCheckType.TARGET_HEALTH,
                    name=f"targets of {group.get('name')}",
                    settings={"target_group_arn": group["arn"]},
                )
            )
    return suggestions


def _suggest_for_database(info: AwsResourceInfo) -> list[Suggestion]:
    """What RDS says of it, its port from inside the VPC (a connection opened
    and closed, no login), and its CloudWatch metrics."""
    suggestions = [Suggestion(check_type=InfraCheckType.DB_STATUS, name="database status", settings={})]
    port = info.aws_detail.get("port")
    if port:
        suggestions.append(Suggestion(check_type=InfraCheckType.TCP, name=f"tcp {port}", settings={"port": port}))
    suggestions.append(Suggestion(check_type=InfraCheckType.DB_METRICS, name="database metrics", settings={}))
    return suggestions


async def security_groups_of(region: client.Region, items: list[AwsResourceInfo]) -> dict[str, dict] | None:
    """The servers' security groups, to see which ports they open to Watchly;
    None when they cannot be read."""
    ids = sorted({g for i in items if i.kind is ResourceKind.SERVER for g in i.aws_detail.get("security_groups", [])})
    if not ids:
        return {}
    try:
        groups = await client.paginate(
            "ec2", "describe_security_groups", region, "SecurityGroups", GroupIds=ids
        )
    except AwsError as exc:
        logger.info("Could not read security groups for suggestions: %s", exc)
        return None
    return {g["GroupId"]: g for g in groups}


#: (vpc id) -> (read at, result), so the add form's steps reuse one listing.
_cache: dict[int, tuple[float, object]] = {}


def cached(vpc_id: int, ttl: int) -> object | None:
    entry = _cache.get(vpc_id)
    if entry and time.monotonic() - entry[0] < ttl:
        return entry[1]
    return None


def remember(vpc_id: int, value: object) -> None:
    _cache[vpc_id] = (time.monotonic(), value)
