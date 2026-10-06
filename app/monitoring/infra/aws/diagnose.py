"""Why something is or is not reachable: a step-by-step dry run of one check,
a test of a whole VPC, where Watchly itself runs and what its role may do,
and what each AWS account's credentials may do.

Nothing here is saved, apart from a VPC's last test result.
"""

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.monitoring import egress
from app.monitoring.infra.aws import client, hints, probes
from app.monitoring.infra.aws.capacity import EIP_QUOTA_CODE
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.models import (
    AwsAccount,
    AwsCheck,
    AwsResource,
    AwsVpc,
    InfraCheckType,
    ResourceKind,
)
from app.monitoring.infra.aws.monitor import build_target
from app.monitoring.infra.aws.probes import db_metrics
from app.monitoring.infra.aws.service import is_watchly_vpc, vpc_cidrs
from app.monitoring.infra.aws.probes.base import (
    ProbeFailure,
    Trace,
    classify_connect_error,
    ms_since,
    resolve_and_vet,
    split_host_port,
)
from app.monitoring.infra.aws.schemas import (
    AccountTestResult,
    CallerRead,
    DiagnoseResult,
    DiagnoseStep,
    GuardRead,
    ImdsRead,
    PermissionRead,
    SecurityGroupRead,
    SelfInstance,
    SelfRead,
    TestStep,
    VpcTestResult,
)

#: Resources a VPC test connects to, at most.
REACH_SAMPLE = 5


async def diagnose(
    check: AwsCheck, resource: AwsResource, vpc: AwsVpc, *, with_hints: bool = False
) -> DiagnoseResult:
    """Run `check` once, without retries, and report every step. `check` and
    `resource` may be unsaved. With `with_hints`, a failure also says where in
    AWS to look (security groups and the like), which shows the network's
    layout: the caller decides who may see it. Raises IcmpUnavailableError for
    a ping that cannot be sent from this server."""
    target = build_target(check, resource, vpc)
    result = await probes.probe_once(target)
    failed_step = next((step.step for step in result.steps if step.ok is False), None)
    return DiagnoseResult(
        ok=result.ok,
        failed_step=failed_step,
        summary=result.summary,
        steps=[
            DiagnoseStep(
                step=step.step,
                ok=step.ok,
                skipped=step.skipped,
                time_ms=step.time_ms,
                error_type=step.error_type,
                detail=step.detail,
            )
            for step in result.steps
        ],
        hints=await hints.hints_for(check, resource, vpc, result, target) if with_hints else [],
    )


def _reach_port(resource: AwsResource) -> int | None:
    """Which port a VPC test knocks on for a resource."""
    if resource.kind is ResourceKind.LOAD_BALANCER:
        listeners = (resource.aws_detail or {}).get("listeners") or []
        return listeners[0].get("port") if listeners else 80
    if resource.kind is ResourceKind.DATABASE:
        return (resource.aws_detail or {}).get("port")
    for check in resource.checks:
        if check.settings.get("use_public_ip"):
            continue
        if check.check_type is InfraCheckType.TCP and check.settings.get("port"):
            return check.settings["port"]
        if check.check_type is InfraCheckType.HTTP:
            return check.settings.get("port") or (443 if check.settings.get("scheme") == "https" else 80)
    return 22


def _reach_address(resource: AwsResource) -> str | None:
    """Where a VPC test knocks: the resource's address, or for an Auto
    Scaling group the private IP of an instance in service, as last synced."""
    if resource.kind is not ResourceKind.AUTO_SCALING_GROUP:
        return resource.address
    members = (resource.aws_detail or {}).get("instances") or []
    return next(
        (m["private_ip"] for m in members if m.get("lifecycle_state") == "InService" and m.get("private_ip")),
        None,
    )


async def _knock(resource: AwsResource, vpc: AwsVpc) -> tuple[bool, str]:
    """Open (and close) one TCP connection to the resource; no login."""
    host, _ = split_host_port(_reach_address(resource) or "")
    port = _reach_port(resource)
    if not host or not port:
        return False, f"{resource.name}: no address or port"
    trace = Trace(["resolve", "policy"])
    try:
        addresses, _ = await resolve_and_vet(host, egress.vpc_scope(vpc.name, vpc.cidrs), 5, trace)
    except ProbeFailure as exc:
        return False, f"{resource.name}: {exc.message}"
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(addresses[0], port), 5)
    except (OSError, TimeoutError) as exc:
        error_type, _ = classify_connect_error(exc)
        what = {"connect_timeout": "timed out", "connect_refused": "refused the connection"}.get(error_type, error_type)
        return False, f"{resource.name} {addresses[0]}:{port} {what}"
    writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), 2)
    return True, f"{resource.name} {addresses[0]}:{port} answered"


async def test_vpc(session: AsyncSession, vpc: AwsVpc) -> VpcTestResult:
    """Placement, AWS access, and reach to a sample of the VPC's resources;
    stored as the VPC's last test."""
    steps: list[TestStep] = []
    identity = await client.identity()
    if identity is None:
        steps.append(
            TestStep(
                step="placement",
                ok=False,
                detail="Watchly is not running on EC2: it reaches this VPC only if this "
                "server's network routes to it (a VPN, say).",
            )
        )
    elif is_watchly_vpc(identity, vpc.account, vpc.aws_vpc_id):
        steps.append(
            TestStep(
                step="placement",
                ok=True,
                detail=f"Watchly runs in {vpc.aws_vpc_id}, {identity.subnet_id}, {identity.private_ip}",
            )
        )
    else:
        steps.append(
            TestStep(
                step="placement",
                ok=True,
                detail=f"Watchly runs in {identity.vpc_id}; {vpc.aws_vpc_id} ({vpc.account.name}) is reached "
                "through peering or a transit gateway, if routes allow.",
            )
        )

    started = time.perf_counter()
    try:
        response = await client.call("ec2", "describe_vpcs", client.Region.of(vpc), VpcIds=[vpc.aws_vpc_id])
        found = response.get("Vpcs") or []
        detail = f"AWS no longer has {vpc.aws_vpc_id}"
        if found:
            # The ranges may have grown since: probes may reach what AWS lists now.
            cidrs = vpc_cidrs(found[0])
            if cidrs and cidrs != list(vpc.cidrs):
                vpc.cidrs = cidrs
            detail = f"DescribeVpcs answered for {vpc.account.name}; ranges {', '.join(vpc.cidrs)}"
        steps.append(TestStep(step="aws", ok=bool(found), time_ms=ms_since(started), detail=detail))
    except AwsError as exc:
        steps.append(
            TestStep(step="aws", ok=False, time_ms=ms_since(started), detail=f"DescribeVpcs in {vpc.account.name}: {exc}")
        )

    # An internet-facing load balancer is reached over the internet, which
    # says nothing about reaching into the VPC.
    resources = [
        resource
        for resource in await session.scalars(
            select(AwsResource)
            .where(AwsResource.vpc_id == vpc.id, AwsResource.missing_since.is_(None))
            .order_by(AwsResource.id)
        )
        if _reach_address(resource) and (resource.aws_detail or {}).get("scheme") != "internet-facing"
    ]
    sample, subnets = [], set()
    for resource in resources:
        subnet = (resource.aws_detail or {}).get("subnet")
        if subnet not in subnets or len(resources) <= REACH_SAMPLE:
            sample.append(resource)
            subnets.add(subnet)
        if len(sample) >= REACH_SAMPLE:
            break
    if not sample:
        steps.append(TestStep(step="reach", ok=True, detail="No resources to connect to yet."))
    else:
        knocks = await asyncio.gather(*(_knock(resource, vpc) for resource in sample))
        answered = sum(1 for ok, _ in knocks if ok)
        failures = [text for ok, text in knocks if not ok]
        detail = f"{answered} of {len(sample)} resources answered"
        if failures:
            detail += "; " + "; ".join(failures)
        steps.append(TestStep(step="reach", ok=not failures, detail=detail))

    result = VpcTestResult(ok=all(step.ok for step in steps), tested_at=datetime.now(UTC), steps=steps)
    vpc.last_test = result.model_dump(mode="json")
    vpc.last_test_at = result.tested_at
    await session.commit()
    return result


# --- where Watchly runs -------------------------------------------------------


async def _try(call) -> tuple[bool | None, str | None, object]:
    try:
        return True, None, await call
    except AwsError as exc:
        return False, exc.code if exc.access_denied else str(exc), None


def _first(response, key: str, field: str):
    items = (response or {}).get(key) or []
    return items[0].get(field) if items else None


def _can_ping() -> bool:
    from icmplib import ICMPv4Socket

    try:
        sock = ICMPv4Socket(privileged=settings.PING_PRIVILEGED)
    except Exception:  # noqa: BLE001 - any failure means no pings from here
        return False
    sock.close()
    return True


async def _caller_and_permissions(
    region: client.Region, *, deployments: bool = False, capacity: bool = False
) -> tuple[CallerRead | None, str | None, list[PermissionRead]]:
    """Who the credentials are, and which of the Describe* calls Watchly
    makes they may make, in one region; with `deployments`, the CodeDeploy
    reads too, and with `capacity`, the Elastic IP and Service Quotas ones."""
    caller, caller_error = None, None
    ok, error, response = await _try(client.call("sts", "get_caller_identity", region))
    if ok:
        caller = CallerRead(account=response.get("Account"), arn=response.get("Arn"))
    else:
        caller_error = error

    permissions: list[PermissionRead] = []
    tests = {
        "ec2:DescribeVpcs": client.call("ec2", "describe_vpcs", region, MaxResults=5),
        "ec2:DescribeSubnets": client.call("ec2", "describe_subnets", region, MaxResults=5),
        "ec2:DescribeRouteTables": client.call("ec2", "describe_route_tables", region, MaxResults=5),
        "ec2:DescribeInstances": client.call("ec2", "describe_instances", region, MaxResults=5),
        "ec2:DescribeSecurityGroups": client.call("ec2", "describe_security_groups", region, MaxResults=5),
        "ec2:DescribeNetworkInterfaces": client.call(
            "ec2", "describe_network_interfaces", region, MaxResults=5
        ),
        "elasticloadbalancing:DescribeLoadBalancers": client.call(
            "elbv2", "describe_load_balancers", region, PageSize=1
        ),
        "autoscaling:DescribeAutoScalingGroups": client.call(
            "autoscaling", "describe_auto_scaling_groups", region, MaxRecords=1
        ),
        "autoscaling:DescribeScalingActivities": client.call(
            "autoscaling", "describe_scaling_activities", region, MaxRecords=1
        ),
        "elasticloadbalancing:DescribeTargetGroups": client.call(
            "elbv2", "describe_target_groups", region, PageSize=1
        ),
        "rds:DescribeDBInstances": client.call("rds", "describe_db_instances", region, MaxRecords=20),
        "cloudwatch:GetMetricData": client.call(
            "cloudwatch",
            "get_metric_data",
            region,
            MetricDataQueries=[db_metrics.metric_query("probe", "CPUUtilization", "watchly-permission-test")],
            StartTime=datetime.now(UTC) - timedelta(minutes=5),
            EndTime=datetime.now(UTC),
        ),
        # What ec2_metrics and db_metrics measure against: volumes' types and
        # IOPS, an RDS class's memory, the agent's disks.
        "ec2:DescribeVolumes": client.call("ec2", "describe_volumes", region, MaxResults=5),
        "ec2:DescribeInstanceTypes": client.call(
            "ec2", "describe_instance_types", region, InstanceTypes=["t3.micro"]
        ),
        "cloudwatch:ListMetrics": client.call(
            "cloudwatch", "list_metrics", region, Namespace="CWAgent", MetricName="disk_used_percent"
        ),
    }
    if capacity:
        tests["ec2:DescribeAddresses"] = client.call("ec2", "describe_addresses", region)
        tests["servicequotas:GetServiceQuota"] = client.call(
            "service-quotas", "get_service_quota", region, ServiceCode="ec2", QuotaCode=EIP_QUOTA_CODE
        )
    outcomes = dict(zip(tests, await asyncio.gather(*(_try(call) for call in tests.values()))))
    for action, (ok, error, _) in outcomes.items():
        if action == "servicequotas:GetServiceQuota" and error and "NoSuchResource" in error:
            # Allowed: the account has no quota of its own, so AWS's default applies.
            ok, error = True, None
        permissions.append(PermissionRead(action=action, ok=ok, detail=error))

    groups = _first(outcomes["rds:DescribeDBInstances"][2], "DBInstances", "DBParameterGroups") or []
    group_name = groups[0].get("DBParameterGroupName") if groups else None
    if group_name is None:
        permissions.append(PermissionRead(action="rds:DescribeDBParameters", ok=None, detail="nothing to try it on"))
    else:
        ok, error = True, None
        try:
            await client.call("rds", "describe_db_parameters", region, DBParameterGroupName=group_name, MaxRecords=20)
        except AwsError as exc:
            # AWS authorizes before it looks: anything but a refusal (the
            # group gone since) means the call is allowed.
            if exc.access_denied:
                ok, error = False, exc.code
        permissions.append(PermissionRead(action="rds:DescribeDBParameters", ok=ok, detail=error))

    lb_arn = _first(outcomes["elasticloadbalancing:DescribeLoadBalancers"][2], "LoadBalancers", "LoadBalancerArn")
    tg_arn = _first(outcomes["elasticloadbalancing:DescribeTargetGroups"][2], "TargetGroups", "TargetGroupArn")
    for action, arn, call in (
        ("elasticloadbalancing:DescribeListeners", lb_arn,
         lambda: client.call("elbv2", "describe_listeners", region, LoadBalancerArn=lb_arn)),
        ("elasticloadbalancing:DescribeTargetHealth", tg_arn,
         lambda: client.call("elbv2", "describe_target_health", region, TargetGroupArn=tg_arn)),
    ):
        if arn is None:
            permissions.append(PermissionRead(action=action, ok=None, detail="nothing to try it on"))
            continue
        ok, error, _ = await _try(call())
        permissions.append(PermissionRead(action=action, ok=ok, detail=error))
    if deployments:
        permissions.extend(await _deployment_permissions(region))
    return caller, caller_error, permissions


async def _deployment_permissions(region: client.Region) -> list[PermissionRead]:
    """The three CodeDeploy reads, the last two on the account's latest
    deployment when it has one."""
    ok, error, response = await _try(client.call("codedeploy", "list_deployments", region))
    rows = [PermissionRead(action="codedeploy:ListDeployments", ok=ok, detail=error)]
    latest = ((response or {}).get("deployments") or [None])[0]
    if latest is None:
        for action in ("codedeploy:BatchGetDeployments", "codedeploy:GetDeploymentGroup"):
            rows.append(PermissionRead(action=action, ok=None, detail="no deployment to try it on"))
        return rows
    ok, error, response = await _try(
        client.call("codedeploy", "batch_get_deployments", region, deploymentIds=[latest])
    )
    rows.append(PermissionRead(action="codedeploy:BatchGetDeployments", ok=ok, detail=error))
    info = ((response or {}).get("deploymentsInfo") or [{}])[0]
    if not (info.get("applicationName") and info.get("deploymentGroupName")):
        rows.append(PermissionRead(action="codedeploy:GetDeploymentGroup", ok=None, detail="nothing to try it on"))
        return rows
    ok, error, _ = await _try(
        client.call(
            "codedeploy", "get_deployment_group", region,
            applicationName=info["applicationName"], deploymentGroupName=info["deploymentGroupName"],
        )
    )
    rows.append(PermissionRead(action="codedeploy:GetDeploymentGroup", ok=ok, detail=error))
    return rows


async def test_account(account: AwsAccount, region: str | None = None) -> AccountTestResult:
    """What the account's credentials may do; recorded on the account."""
    credentials = client.Account.of(account)
    name = region or await client.default_region(credentials)
    tested_at = datetime.now(UTC)
    if not name:
        caller, caller_error, permissions = None, "No AWS region: set the account's default region or AWS_REGION.", []
    else:
        caller, caller_error, permissions = await _caller_and_permissions(
            client.Region(credentials, name),
            deployments=account.watch_deployments,
            capacity=account.watch_capacity,
        )
    if caller is not None:
        account.aws_account_id = caller.account or account.aws_account_id
        account.verified_at = tested_at
    account.last_error = caller_error
    return AccountTestResult(
        ok=caller is not None and all(p.ok is not False for p in permissions),
        tested_at=tested_at,
        region=name,
        caller=caller,
        caller_error=caller_error,
        permissions=permissions,
    )


async def self_report() -> SelfRead:
    """Where Watchly runs, and what its own credentials (the instance role)
    may do. Each AWS account's are tested on their own: `test_account`."""
    identity = await client.identity(refresh=True)
    region = await client.default_region()

    caller, caller_error = None, None
    imds = None
    permissions: list[PermissionRead] = []
    if region:
        own = client.Region(client.OWN, region)
        caller, caller_error, permissions = await _caller_and_permissions(own)
        if identity is not None:
            ok, _, response = await _try(
                client.call("ec2", "describe_instances", own, InstanceIds=[identity.instance_id])
            )
            options = {}
            if ok:
                for reservation in response.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        options = instance.get("MetadataOptions") or {}
            imds = ImdsRead(
                tokens_required=options.get("HttpTokens", "required" if identity.tokens_required else "optional")
                == "required",
                hop_limit=options.get("HttpPutResponseHopLimit"),
            )
    else:
        caller_error = "No AWS region: set AWS_REGION, or run Watchly on EC2."

    probe_support = {
        "ping": _can_ping(),
        "tcp": True,
        "http": True,
        "target_health": True,
        "group_health": True,
        "db_status": True,
        "db_metrics": True,
        "ec2_metrics": True,
    }

    return SelfRead(
        instance=(
            SelfInstance(
                id=identity.instance_id,
                type=identity.instance_type,
                region=identity.region,
                az=identity.availability_zone,
                vpc_id=identity.vpc_id,
                subnet_id=identity.subnet_id,
                private_ip=identity.private_ip,
                public_ip=identity.public_ip,
                security_groups=[SecurityGroupRead(id=g, name=n) for g, n in identity.security_groups],
                iam_role=identity.iam_role,
                account_id=identity.account_id,
            )
            if identity
            else None
        ),
        region=region,
        caller=caller,
        caller_error=caller_error,
        imds=imds,
        guard=GuardRead(
            metadata_blocked=egress.refusal("169.254.169.254", egress.website_scope()) is not None,
            website_private_targets=settings.WEBSITE_PRIVATE_TARGETS,
            deny_extra=list(settings.EGRESS_DENY_CIDRS),
            own_addresses=egress.own_addresses(),
        ),
        permissions=permissions,
        probes=probe_support,
    )
