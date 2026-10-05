"""/monitoring/infra/aws: EC2 servers and load balancers, apart from websites.

Every endpoint answers 404 while INFRA_AWS_ENABLED is off. An infrastructure
project's AWS accounts, and the VPCs registered from them, are run by whoever
may manage the project; resources follow the project rules websites follow, plus
"the VPC is granted to the project".
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_roles
from app.core.config import settings
from app.core.permissions import PROJECT_CREATORS, ROLE_MANAGERS
from app.core.rate_limit import RATE_LIMITED, _count
from app.db.models.user import User
from app.db.session import get_db
from app.monitoring.exports import csv_response, infra_stats_csv
from app.monitoring.infra.aws import client, diagnose as diagnose_module
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.history import InfraHistoryService
from app.monitoring.infra.aws.models import (
    AwsCheckResult,
    AwsResource,
    InfraCheckType,
    ResourceKind,
    ResourceState,
)
from app.monitoring.infra.aws.monitor import InfraMonitor
from app.monitoring.infra.aws.probes.target_health import target_group_name
from app.monitoring.infra.aws.schemas import (
    AccountBrief,
    AccountCreate,
    AccountList,
    AccountRead,
    AccountTestResult,
    AccountUpdate,
    AttentionItem,
    AttentionResource,
    AvailableVpcs,
    CheckCreate,
    CheckNowResponse,
    CheckResultList,
    CheckResultRead,
    CheckUpdate,
    DeploymentList,
    DeploymentRead,
    DeploymentResource,
    DiagnoseRequest,
    DiagnoseResult,
    DiscoverRequest,
    DiscoveryResult,
    EventList,
    EventRead,
    EventResource,
    ImportRequest,
    ImportResult,
    KindCounts,
    Overview,
    OverviewVpc,
    ProjectBrief,
    ResourceCreate,
    ResourceList,
    ResourceRead,
    ResourceRecipientsUpdate,
    ResourceSort,
    ResourceStats,
    ResourceTargets,
    ResourceUpdate,
    SelfRead,
    StateCounts,
    TargetGroupTargets,
    TargetRead,
    TargetSpan,
    Topology,
    VpcBrief,
    VpcCreate,
    VpcList,
    VpcRead,
    VpcTestResult,
    VpcUpdate,
)
from app.monitoring.infra.aws.service import (
    InfraAwsError,
    InfraConflictError,
    InfraError,
    InfraForbiddenError,
    InfraInvalidError,
    InfraNotFoundError,
    InfraService,
    VpcView,
    attention_summary,
)
from app.monitoring.projects.service import UnknownMembersError
from app.monitoring.websites.models import WebsiteEnvironment
from app.monitoring.websites.pinger import IcmpUnavailableError
from app.monitoring.websites.schemas import MaintenanceCreate, StatsFormat, StatsRange


def require_enabled() -> None:
    if not settings.INFRA_AWS_ENABLED:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Infrastructure monitoring is off: set INFRA_AWS_ENABLED=true.",
        )


router = APIRouter(
    prefix="/monitoring/infra/aws",
    tags=["monitoring: infrastructure (AWS)"],
    dependencies=[Depends(require_enabled)],
)

require_manager = require_roles(*ROLE_MANAGERS)
require_project_creator = require_roles(*PROJECT_CREATORS)

NOT_FOUND = {404: {"description": "No such thing, or not one the caller can see"}}


def get_service(session: AsyncSession = Depends(get_db)) -> InfraService:
    return InfraService(session)


def get_monitor(session: AsyncSession = Depends(get_db)) -> InfraMonitor:
    return InfraMonitor(session)


async def diagnose_rate_limit(actor: User = Depends(get_current_user)) -> None:
    """Diagnose runs and VPC tests open connections into a VPC: per user,
    RATE_LIMIT_INFRA_DIAGNOSE of them."""
    if settings.RATE_LIMIT_ENABLED:
        await _count("infra_diagnose", f"user:{actor.id}", settings.RATE_LIMIT_INFRA_DIAGNOSE)


@contextmanager
def infra_errors() -> Iterator[None]:
    """Service errors as HTTP answers."""
    try:
        yield
    except InfraNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InfraForbiddenError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except InfraConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (InfraInvalidError, UnknownMembersError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except InfraAwsError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except AwsError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"{exc.operation}: {exc}") from exc
    except InfraError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def account_read(account, vpc_count: int) -> AccountRead:
    return AccountRead(
        id=account.id,
        project=ProjectBrief.model_validate(account.project),
        name=account.name,
        description=account.description,
        auth_type=account.auth_type,
        access_key_id=account.access_key_id,
        secret_access_key_hint=account.secret_access_key_hint,
        role_arn=account.role_arn,
        external_id=account.external_id,
        default_region=account.default_region,
        environment=account.environment,
        aws_account_id=account.aws_account_id,
        verified_at=account.verified_at,
        last_error=account.last_error,
        watch_deployments=account.watch_deployments,
        deployments_checked_at=account.deployments_checked_at,
        deployments_error=account.deployments_error,
        vpc_count=vpc_count,
        created_by_id=account.created_by_id,
        created_at=account.created_at,
    )


def vpc_read(view: VpcView) -> VpcRead:
    vpc = view.vpc
    return VpcRead(
        id=vpc.id,
        name=vpc.name,
        description=vpc.description,
        account=AccountBrief.model_validate(vpc.account),
        region=vpc.region,
        aws_vpc_id=vpc.aws_vpc_id,
        cidrs=list(vpc.cidrs),
        is_watchly_vpc=vpc.is_watchly_vpc,
        project=ProjectBrief.model_validate(vpc.project),
        health=view.health,
        unreachable_since=vpc.unreachable_since,
        synced_at=vpc.synced_at,
        last_tested_at=vpc.last_test_at,
        last_test=vpc.last_test,
        counts=view.counts,
        created_by_id=vpc.created_by_id,
        created_at=vpc.created_at,
    )


async def _vpc_read(service: InfraService, vpc, actor: User) -> VpcRead:
    return vpc_read((await service.vpc_views([vpc], actor))[0])


# ==========================================================================
# AWS accounts
# ==========================================================================

ACCOUNT_REFUSED = {
    409: {"description": "Name taken in the project, or that AWS account is already in it"},
    422: {"description": "AWS refused the credentials, they are incomplete, or the project monitors websites"},
}
MANAGES_PROJECT = {403: {"description": "Requires admin/DevOps, or ownership of the project"}}


@router.get(
    "/accounts",
    response_model=AccountList,
    summary="AWS accounts of infrastructure projects",
    responses=MANAGES_PROJECT,
)
async def list_accounts(
    project_id: int | None = Query(None, ge=1, description="Only this project's."),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> AccountList:
    """The accounts of the projects the caller may manage: every project for
    admin and DevOps, their own for a project manager."""
    with infra_errors():
        rows = await service.list_accounts(actor, project_id)
    return AccountList(items=[account_read(account, n) for account, n in rows], total=len(rows))


@router.post(
    "/accounts",
    response_model=AccountRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an AWS account to an infrastructure project",
    responses={**NOT_FOUND, **MANAGES_PROJECT, **ACCOUNT_REFUSED},
)
async def create_account(
    payload: AccountCreate,
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> AccountRead:
    """`access_key`: an IAM user's access key, stored encrypted and never
    returned. `default`: Watchly's own credentials (the instance role). Either
    may assume `role_arn`, the usual way to reach another account from the
    instance role. The credentials are tried (STS GetCallerIdentity) before
    they are saved. A project's first accounts come with it: see
    `POST /monitoring/projects`."""
    with infra_errors():
        account = await service.create_account(payload, actor)
    return account_read(account, 0)


@router.get(
    "/accounts/{account_id}",
    response_model=AccountRead,
    summary="One AWS account",
    responses={**NOT_FOUND, **MANAGES_PROJECT},
)
async def read_account(
    account_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> AccountRead:
    with infra_errors():
        account = await service.get_account(account_id, actor)
    return account_read(account, await service.vpc_count(account.id))


@router.patch(
    "/accounts/{account_id}",
    response_model=AccountRead,
    summary="Change an AWS account or its credentials",
    responses={**NOT_FOUND, **MANAGES_PROJECT, **ACCOUNT_REFUSED},
)
async def update_account(
    payload: AccountUpdate,
    account_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> AccountRead:
    """New credentials are tried before they are saved, and must reach the
    same AWS account as before while it holds VPCs."""
    with infra_errors():
        account = await service.update_account(account_id, payload, actor)
    return account_read(account, await service.vpc_count(account.id))


@router.delete(
    "/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an AWS account from its project",
    responses={**NOT_FOUND, **MANAGES_PROJECT, 409: {"description": "It holds VPCs, or it is the project's last"}},
)
async def delete_account(
    account_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> Response:
    """An infrastructure project keeps at least one account."""
    with infra_errors():
        await service.delete_account(account_id, actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/accounts/{account_id}/test",
    response_model=AccountTestResult,
    summary="What an account's credentials may do",
    dependencies=[Depends(diagnose_rate_limit)],
    responses={**NOT_FOUND, **MANAGES_PROJECT, **RATE_LIMITED},
)
async def test_account(
    account_id: int = Path(ge=1),
    region: str | None = Query(None, max_length=32, description="The account's default region by default."),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> AccountTestResult:
    """Who the credentials are, and each Describe* permission tried with the
    smallest read. Recorded as the account's last verification."""
    with infra_errors():
        account = await service.get_account(account_id, actor)
    result = await diagnose_module.test_account(account, region)
    await service.session.commit()
    return result


# ==========================================================================
# VPCs
# ==========================================================================


@router.get("/vpcs", response_model=VpcList, summary="Registered VPCs")
async def list_vpcs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    health: str | None = Query(None, pattern="^(reachable|unreachable|untested)$"),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> VpcList:
    """Admin and DevOps see every VPC; anyone else, those in the AWS accounts
    of projects they can see. Counts cover the resources the caller can see."""
    views, total = await service.list_vpcs(actor, limit=limit, offset=offset, health=health)
    return VpcList(items=[vpc_read(view) for view in views], total=total)


@router.get(
    "/vpcs/available",
    response_model=AvailableVpcs,
    summary="VPCs an AWS account has, for adding resources from one",
    responses={**MANAGES_PROJECT, 404: {"description": "No such account"}, 502: {"description": "AWS did not answer"}},
)
async def available_vpcs(
    account_id: int = Query(ge=1, description="The AWS account to list."),
    region: str | None = Query(None, max_length=32, description="The account's default region by default."),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> AvailableVpcs:
    """`registered_as` is Watchly's id for a VPC already registered from this
    account; register any other with `POST /vpcs` before discovering it."""
    with infra_errors():
        return await service.available_vpcs(account_id, region, actor)


@router.post(
    "/vpcs",
    response_model=VpcRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a VPC of a project's AWS account",
    responses={
        **MANAGES_PROJECT,
        409: {"description": "Already registered, or its name is taken"},
        404: {"description": "No such AWS account"},
        422: {"description": "No such VPC in the account's region, or a public range"},
        502: {"description": "AWS did not answer"},
    },
)
async def create_vpc(
    payload: VpcCreate,
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> VpcRead:
    """The VPC belongs to the account's project, and is read, synced and
    probed with the account's credentials. Watchly reads its IPv4 ranges from
    AWS: infrastructure probes may reach those, and public addresses only for
    a check over the internet. The dashboard registers a VPC when someone
    first adds a resource from it."""
    with infra_errors():
        vpc = await service.create_vpc(payload, actor)
    return await _vpc_read(service, vpc, actor)


@router.get("/vpcs/{vpc_id}", response_model=VpcRead, summary="One VPC", responses=NOT_FOUND)
async def read_vpc(
    vpc_id: int = Path(ge=1),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> VpcRead:
    with infra_errors():
        vpc = await service.get_visible_vpc(vpc_id, actor)
    return await _vpc_read(service, vpc, actor)


@router.patch(
    "/vpcs/{vpc_id}", response_model=VpcRead, summary="Rename a VPC", responses={**NOT_FOUND, **MANAGES_PROJECT}
)
async def update_vpc(
    payload: VpcUpdate,
    vpc_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> VpcRead:
    with infra_errors():
        vpc = await service.update_vpc(vpc_id, payload, actor)
    return await _vpc_read(service, vpc, actor)


@router.delete(
    "/vpcs/{vpc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a VPC, and with with_resources its monitored resources",
    responses={
        **NOT_FOUND,
        **MANAGES_PROJECT,
        409: {"description": "It still holds resources and with_resources is false"},
    },
)
async def delete_vpc(
    vpc_id: int = Path(ge=1),
    with_resources: bool = Query(
        False,
        description="Also stop monitoring every resource in it, deleting their checks and history. "
        "Nothing changes in AWS.",
    ),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> None:
    with infra_errors():
        await service.delete_vpc(vpc_id, actor, with_resources=with_resources)


@router.post(
    "/vpcs/{vpc_id}/test",
    response_model=VpcTestResult,
    summary="Test a VPC: placement, AWS access, reach",
    dependencies=[Depends(diagnose_rate_limit)],
    responses={**NOT_FOUND, **MANAGES_PROJECT, **RATE_LIMITED},
)
async def test_vpc(
    vpc_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> VpcTestResult:
    """Opens a TCP connection, with no login, to up to five of its resources,
    one per subnet where it can. Stored as the VPC's last test."""
    with infra_errors():
        vpc = await service.get_vpc_for_write(vpc_id, actor)
    return await diagnose_module.test_vpc(service.session, vpc)


async def _discoverable_vpc(service: InfraService, vpc_id: int, actor: User):
    """A VPC the caller may list: any, for admin and DevOps; for a project
    manager, one of a project they can see."""
    return await service.get_visible_vpc(vpc_id, actor)


@router.get(
    "/vpcs/{vpc_id}/topology",
    response_model=Topology,
    summary="The VPC map: subnets, resources and how they connect",
    responses=NOT_FOUND,
)
async def vpc_topology(
    vpc_id: int = Path(ge=1),
    refresh: bool = Query(False, description="Ask AWS again instead of using what was read in the last minutes."),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> Topology:
    """Subnets (public or private, from their route tables), load balancers →
    target groups → targets, Auto Scaling groups → instances, and Watchly's
    paths: its probes into the VPC or over the internet, and what it reads from
    the AWS API. Resources not monitored are included, with a `key` to add them
    by. Read from AWS, cached as discovery is; when AWS lists nothing, only the
    monitored resources are drawn and `aws_error` says why."""
    with infra_errors():
        vpc = await service.get_visible_vpc(vpc_id, actor)
        return await service.topology(vpc, actor, refresh=refresh)


@router.post(
    "/vpcs/{vpc_id}/discover",
    response_model=DiscoveryResult,
    summary="What is in a VPC, with suggested checks",
    responses={**NOT_FOUND, 502: {"description": "AWS did not answer"}},
)
async def discover(
    payload: DiscoverRequest | None = None,
    vpc_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> DiscoveryResult:
    """EC2 instances (public and private), ALBs and NLBs (internal and
    internet-facing) with their target groups, and Auto Scaling groups with
    their instances and their target groups' health checks. Cached for
    AWS_DISCOVERY_CACHE_SECONDS; send
    `{"refresh": true}` to ask AWS again. A listing the role may not make is
    in `skipped` and does not fail the call. Project managers may discover in
    the VPCs granted to their projects."""
    with infra_errors():
        vpc = await _discoverable_vpc(service, vpc_id, actor)
        return await service.discover(vpc, actor, refresh=bool(payload and payload.refresh))


@router.post(
    "/vpcs/{vpc_id}/import",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
    summary="Add discovered resources and their checks, all at once",
    responses={
        **NOT_FOUND,
        409: {"description": "Already monitored, with the items listed"},
        422: {"description": "Invalid items, listed"},
    },
)
async def import_resources(
    payload: ImportRequest,
    vpc_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ImportResult:
    """Each item is a discovered `key`, optionally a `name`, and its `checks`;
    without `checks` it gets the suggested ones. All or nothing."""
    with infra_errors():
        vpc = await _discoverable_vpc(service, vpc_id, actor)
        created = await service.import_resources(vpc, payload, actor)
    return ImportResult(created=[ResourceRead.of(r) for r in created])


# ==========================================================================
# Resources
# ==========================================================================


def _scope(vpc_id, project_id, kind, environment, q) -> dict:
    return {"vpc_id": vpc_id, "project_id": project_id, "kind": kind, "environment": environment, "q": q}


@router.get("/resources", response_model=ResourceList, summary="Monitored resources")
async def list_resources(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    vpc_id: int | None = Query(None, ge=1),
    project_id: int | None = Query(None, ge=1),
    kind: ResourceKind | None = Query(None),
    state: ResourceState | None = Query(None),
    environment: WebsiteEnvironment | None = Query(None),
    q: str | None = Query(None, max_length=200, description="Name, AWS id or address contains this."),
    sort: ResourceSort = Query(ResourceSort.STATE),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> ResourceList:
    """Scoped like websites: admin and DevOps see everything, anyone else the
    resources of their projects and any they are an extra recipient of.
    `sort=state` puts down first, then degraded and missing."""
    rows, total = await service.list_resources(
        actor, limit=limit, offset=offset, state=state, sort=sort,
        **_scope(vpc_id, project_id, kind, environment, q),
    )
    return ResourceList(items=[ResourceRead.of(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/resources/summary", response_model=StateCounts, summary="Count resources by state")
async def summarize_resources(
    vpc_id: int | None = Query(None, ge=1),
    project_id: int | None = Query(None, ge=1),
    kind: ResourceKind | None = Query(None),
    environment: WebsiteEnvironment | None = Query(None),
    q: str | None = Query(None, max_length=200),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> StateCounts:
    return await service.summary(actor, **_scope(vpc_id, project_id, kind, environment, q))


def event_read(event) -> EventRead:
    return EventRead(
        id=event.id,
        kind=event.kind,
        occurred_at=event.occurred_at,
        summary=event.summary,
        downtime_seconds=event.downtime_seconds,
        project_id=event.project_id,
        vpc=VpcBrief.model_validate(event.vpc) if event.vpc else None,
        resource=EventResource.model_validate(event.resource) if event.resource else None,
    )


@router.get("/resources/events", response_model=EventList, summary="The infrastructure feed")
async def list_events(
    after_id: int | None = Query(None, ge=0, description="Only events after this one."),
    limit: int = Query(20, ge=1, le=100),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> EventList:
    """Outages, recoveries, problems, VPCs lost and found, and deployments,
    newest first. Still-down reminders are not recorded."""
    events, total = await service.events(actor, after_id=after_id, limit=limit)
    return EventList(items=[event_read(e) for e in events], total=total, limit=limit)


def deployment_read(row) -> DeploymentRead:
    return DeploymentRead(
        id=row.id,
        project=ProjectBrief.model_validate(row.account.project),
        account=AccountBrief.model_validate(row.account),
        region=row.region,
        deployment_id=row.deployment_id,
        application_name=row.application_name,
        group_name=row.group_name,
        status=row.status,
        environment=row.environment,
        creator=row.creator,
        description=row.description,
        revision=row.revision,
        resources=[DeploymentResource(**item) for item in row.resources],
        fallback=row.fallback,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
        is_active=row.is_active,
    )


@router.get("/deployments", response_model=DeploymentList, summary="CodeDeploy deployments")
async def list_deployments(
    project_id: int | None = Query(None, ge=1),
    active: bool | None = Query(None, description="Only running (true) or ended (false) ones."),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> DeploymentList:
    """The deployments Watchly saw in the AWS accounts that watch them, of
    the projects the caller can see, newest first. While one runs, the
    resources in its `resources` are in maintenance."""
    rows, total = await service.deployments(
        actor, project_id=project_id, active=active, limit=limit, offset=offset
    )
    return DeploymentList(
        items=[deployment_read(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.post(
    "/resources",
    response_model=ResourceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a resource by its AWS id",
    responses={
        403: {"description": "No rights over the project"},
        409: {"description": "Already monitored"},
        422: {"description": "Not in that VPC, VPC not granted to the project, or an invalid check"},
        502: {"description": "AWS did not answer"},
    },
)
async def create_resource(
    payload: ResourceCreate,
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    """Watchly looks the resource up in AWS, checks it is in that VPC, and
    reads its address. Without `checks`, the suggested ones are added."""
    with infra_errors():
        resource = await service.create_resource(payload, actor)
    return ResourceRead.of(resource)


@router.get("/resources/{resource_id}", response_model=ResourceRead, summary="One resource", responses=NOT_FOUND)
async def read_resource(
    resource_id: int = Path(ge=1),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    with infra_errors():
        return ResourceRead.of(await service.get_visible_resource(resource_id, actor))


@router.patch("/resources/{resource_id}", response_model=ResourceRead, summary="Change a resource", responses=NOT_FOUND)
async def update_resource(
    payload: ResourceUpdate,
    resource_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    """`is_enabled: false` pauses it (and ends any outage quietly); a new
    `project_id` must be granted the same VPC."""
    with infra_errors():
        resource = await service.get_resource_for_write(resource_id, actor)
        return ResourceRead.of(await service.update_resource(resource, payload, actor))


@router.delete(
    "/resources/{resource_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop monitoring a resource",
    responses=NOT_FOUND,
)
async def delete_resource(
    resource_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> None:
    with infra_errors():
        resource = await service.get_resource_for_write(resource_id, actor)
        await service.delete_resource(resource)


@router.post(
    "/resources/{resource_id}/check",
    response_model=CheckNowResponse,
    summary="Run a resource's checks now",
    responses={
        **NOT_FOUND,
        409: {"description": "Paused, missing, or in maintenance"},
        503: {"description": "A ping, and this server may not send pings"},
    },
)
async def check_now(
    resource_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    monitor: InfraMonitor = Depends(get_monitor),
) -> CheckNowResponse:
    """Through the same state machine as a scheduled run, so it can raise and
    clear alerts."""
    with infra_errors():
        resource = await monitor.service.get_resource_for_write(resource_id, actor)
        if not resource.is_enabled:
            raise InfraConflictError("The resource is paused: resume it first.")
        if resource.missing_since is not None:
            raise InfraConflictError("AWS no longer lists this resource.")
        if resource.maintenance is not None:
            raise InfraConflictError("The resource is in maintenance.")
    try:
        outcome = await monitor.check_resource(resource)
    except IcmpUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return CheckNowResponse(
        resource=ResourceRead.of(outcome.resource),
        results=[CheckResultRead.model_validate(r) for r in outcome.results],
        alert_sent=outcome.alert.value if outcome.alert else None,
    )


@router.get("/resources/{resource_id}/results", response_model=CheckResultList, summary="Recent check results", responses=NOT_FOUND)
async def list_results(
    resource_id: int = Path(ge=1),
    check_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    since: datetime | None = Query(None),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> CheckResultList:
    """Newest first, with each type's detail group: `ping`, `tcp`, `http` or
    `targets`."""
    with infra_errors():
        resource = await service.get_visible_resource(resource_id, actor)
    rows, total = await service.results(resource, check_id=check_id, limit=limit, offset=offset, since=since)
    return CheckResultList(
        items=[CheckResultRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/resources/{resource_id}/stats",
    response_model=ResourceStats,
    summary="Uptime, response time and each check's own figure over a range",
    responses={**NOT_FOUND, 200: {"content": {"text/csv": {}}}},
)
async def resource_stats(
    resource_id: int = Path(ge=1),
    range_: StatsRange = Query(StatsRange.DAY, alias="range"),
    format_: StatsFormat = Query(StatsFormat.JSON, alias="format"),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> ResourceStats | Response:
    """From hourly rollups, one series per check. `metric` names what
    `metric_min`/`metric_max` measure for that check."""
    with infra_errors():
        resource = await service.get_visible_resource(resource_id, actor)
    stats = await InfraHistoryService(service.session).stats(list(resource.checks), range_, kind=resource.kind)
    if format_ is StatsFormat.CSV:
        return csv_response(infra_stats_csv(stats), f"watchly-resource-{resource_id}-{range_.value}.csv")
    return stats


async def _instance_names(region: client.Region, ids: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """Instance id -> (Name tag, private IP); empty when it cannot be read."""
    instance_ids = [i for i in ids if i and i.startswith("i-")]
    if not instance_ids:
        return {}
    try:
        response = await client.call("ec2", "describe_instances", region, InstanceIds=instance_ids)
    except AwsError:
        return {}
    names = {}
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            names[instance["InstanceId"]] = (
                client.tags(instance.get("Tags")).get("Name"),
                instance.get("PrivateIpAddress"),
            )
    return names


@router.get(
    "/resources/{resource_id}/targets",
    response_model=ResourceTargets,
    summary="A load balancer's or Auto Scaling group's targets, now and over 24 hours",
    responses={**NOT_FOUND, 409: {"description": "Neither a load balancer nor an Auto Scaling group"}},
)
async def resource_targets(
    resource_id: int = Path(ge=1),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> ResourceTargets:
    """Each target of each `target_health` check: its state now, and when it
    changed over the last 24 hours, read from the results."""
    with infra_errors():
        resource = await service.get_visible_resource(resource_id, actor)
        if resource.kind not in {ResourceKind.LOAD_BALANCER, ResourceKind.AUTO_SCALING_GROUP}:
            raise InfraConflictError("Only a load balancer or an Auto Scaling group has targets.")
    since = datetime.now(UTC) - timedelta(hours=24)
    health_checks = {
        g.get("arn"): g.get("health_check") for g in (resource.aws_detail or {}).get("target_groups", [])
    }
    groups = []
    all_ids: set[str] = set()
    for check in resource.checks:
        if check.check_type is not InfraCheckType.TARGET_HEALTH:
            continue
        results = list(
            await service.session.scalars(
                select(AwsCheckResult)
                .where(AwsCheckResult.check_id == check.id, AwsCheckResult.checked_at >= since)
                .order_by(AwsCheckResult.checked_at)
            )
        )
        spans: dict[str, list[TargetSpan]] = {}
        latest: dict[str, dict] = {}
        for result in results:
            for target in (result.detail or {}).get("targets", []):
                ident = target.get("id")
                if not ident:
                    continue
                latest[ident] = target
                timeline = spans.setdefault(ident, [])
                if not timeline or timeline[-1].state != target.get("state"):
                    if timeline:
                        timeline[-1].to = result.checked_at
                    timeline.append(TargetSpan(**{"from": result.checked_at, "to": None, "state": target.get("state", "unknown")}))
        all_ids.update(latest)
        arn = check.settings["target_group_arn"]
        groups.append((check, arn, latest, spans))

    names = await _instance_names(client.Region.of(resource.vpc), sorted(all_ids))
    return ResourceTargets(
        target_groups=[
            TargetGroupTargets(
                check_id=check.id,
                name=target_group_name(arn),
                arn=arn,
                health_check=health_checks.get(arn),
                targets=[
                    TargetRead(
                        id=ident,
                        name=names.get(ident, (None, None))[0],
                        address=names.get(ident, (None, None))[1],
                        port=target.get("port"),
                        az=target.get("az"),
                        state=target.get("state", "unknown"),
                        since=spans[ident][-1].from_ if spans.get(ident) else None,
                        reason=target.get("reason"),
                        description=target.get("description"),
                        timeline=spans.get(ident, []),
                    )
                    for ident, target in sorted(latest.items())
                ],
            )
            for check, arn, latest, spans in groups
        ]
    )


@router.post("/resources/{resource_id}/recipients", response_model=ResourceRead, summary="Add users alerted about this resource", responses=NOT_FOUND)
async def add_recipients(
    payload: ResourceRecipientsUpdate,
    resource_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    with infra_errors():
        resource = await service.get_resource_for_write(resource_id, actor)
        return ResourceRead.of(await service.add_recipients(resource, payload.recipient_ids))


@router.delete("/resources/{resource_id}/recipients/{user_id}", response_model=ResourceRead, summary="Stop alerting a user about this resource", responses=NOT_FOUND)
async def remove_recipient(
    resource_id: int = Path(ge=1),
    user_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    with infra_errors():
        resource = await service.get_resource_for_write(resource_id, actor)
        return ResourceRead.of(await service.remove_recipient(resource, user_id))


@router.post(
    "/resources/{resource_id}/maintenance",
    response_model=ResourceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start maintenance now, or schedule it",
    responses={**NOT_FOUND, 409: {"description": "Overlaps maintenance it already has"}},
)
async def schedule_maintenance(
    payload: MaintenanceCreate,
    resource_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    """As for websites: no checks and no alerts while it is in effect."""
    with infra_errors():
        resource = await service.get_resource_for_write(resource_id, actor)
        return ResourceRead.of(await service.schedule_maintenance(resource, payload, actor.id))


@router.post("/resources/{resource_id}/maintenance/end", response_model=ResourceRead, summary="End maintenance now", responses=NOT_FOUND)
async def end_maintenance(
    resource_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    with infra_errors():
        resource = await service.get_resource_for_write(resource_id, actor)
        return ResourceRead.of(await service.end_maintenance(resource))


@router.delete(
    "/resources/{resource_id}/maintenance/{window_id}",
    response_model=ResourceRead,
    summary="Cancel scheduled maintenance",
    responses={**NOT_FOUND, 409: {"description": "Already started: end it instead"}},
)
async def cancel_maintenance(
    resource_id: int = Path(ge=1),
    window_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    with infra_errors():
        resource = await service.get_resource_for_write(resource_id, actor)
        return ResourceRead.of(await service.cancel_maintenance(resource, window_id))


# -- checks ----------------------------------------------------------------


@router.post(
    "/resources/{resource_id}/checks",
    response_model=ResourceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a check to a resource",
    responses={**NOT_FOUND, 409: {"description": "The same check exists"}, 422: {"description": "Invalid for this resource"}},
)
async def add_check(
    payload: CheckCreate,
    resource_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    """A server takes ping, tcp and http (`use_public_ip` sends one to its
    public IP); a load balancer http, tcp (one of its listeners' ports) and
    target_health (one of its target groups); an Auto Scaling group ping, tcp
    and http, run on every instance in service (`min_healthy_instances`),
    group_health, and target_health."""
    with infra_errors():
        resource = await service.get_resource_for_write(resource_id, actor)
        return ResourceRead.of(await service.add_check(resource, payload))


@router.patch("/resources/{resource_id}/checks/{check_id}", response_model=ResourceRead, summary="Change a check", responses=NOT_FOUND)
async def update_check(
    payload: CheckUpdate,
    resource_id: int = Path(ge=1),
    check_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    """`settings` replaces them, validated for the check's type, which is fixed."""
    with infra_errors():
        resource = await service.get_resource_for_write(resource_id, actor)
        return ResourceRead.of(await service.update_check(resource, check_id, payload))


@router.delete(
    "/resources/{resource_id}/checks/{check_id}",
    response_model=ResourceRead,
    summary="Remove a check",
    responses={**NOT_FOUND, 409: {"description": "The resource's last check"}},
)
async def delete_check(
    resource_id: int = Path(ge=1),
    check_id: int = Path(ge=1),
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> ResourceRead:
    with infra_errors():
        resource = await service.get_resource_for_write(resource_id, actor)
        return ResourceRead.of(await service.delete_check(resource, check_id))


# ==========================================================================
# Diagnose, overview, self
# ==========================================================================


@router.post(
    "/diagnose",
    response_model=DiagnoseResult,
    summary="Dry-run a check, step by step",
    dependencies=[Depends(diagnose_rate_limit)],
    responses={**NOT_FOUND, **RATE_LIMITED, 503: {"description": "A ping, and this server may not send pings"}},
)
async def run_diagnose(
    payload: DiagnoseRequest,
    actor: User = Depends(require_project_creator),
    service: InfraService = Depends(get_service),
) -> DiagnoseResult:
    """resolve → policy → connect → TLS → response, each with its outcome;
    nothing is saved. A timeout means something dropped the packets
    (security group, NACL, route); "connection refused" means the host
    answered and nothing listens. For an Admin or DevOps caller a failure
    also comes with `hints`: which security group lacks which rule, or where
    else to look."""
    with infra_errors():
        if payload.resource_id is not None:
            resource = await service.get_resource_for_write(payload.resource_id, actor)
            check = service.get_check(resource, payload.check_id)
            vpc = resource.vpc
        else:
            project = await service.project_for_write(payload.project_id, actor)
            vpc = await service._vpc_for_project(payload.vpc_id, project, actor)
            info = await service._lookup_in_aws(vpc, payload.kind, payload.aws_id.strip())
            resource = AwsResource(
                kind=info.kind,
                name=info.name,
                aws_id=info.aws_id,
                address=info.address,
                aws_detail=info.aws_detail,
                vpc_id=vpc.id,
            )
            resource.checks = []
            check = await service._build_check(resource, payload.check)
    try:
        return await diagnose_module.diagnose(check, resource, vpc, with_hints=actor.role in ROLE_MANAGERS)
    except IcmpUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    finally:
        # Nothing a dry run touched is kept, unsaved objects included.
        await service.session.rollback()


@router.get("/overview", response_model=Overview, summary="The Infrastructure page's top half")
async def overview(
    vpc_id: int | None = Query(None, ge=1),
    project_id: int | None = Query(None, ge=1),
    environment: WebsiteEnvironment | None = Query(None),
    actor: User = Depends(get_current_user),
    service: InfraService = Depends(get_service),
) -> Overview:
    """Counts by state, per VPC and per kind, and what needs attention: down
    resources first, then degraded and missing ones, oldest first, at most 20."""
    data = await service.overview(
        actor, vpc_id=vpc_id, project_id=project_id, kind=None, environment=environment, q=None
    )
    attention = []
    for resource, state in data["attention"]:
        since, summary = attention_summary(resource, state)
        attention.append(
            AttentionItem(
                resource=AttentionResource(
                    id=resource.id, name=resource.name, kind=resource.kind, vpc=VpcBrief.model_validate(resource.vpc)
                ),
                state=state,
                since=since,
                summary=summary,
            )
        )
    return Overview(
        counts=data["counts"],
        vpcs=[
            OverviewVpc(
                id=v.vpc.id,
                name=v.vpc.name,
                health=v.health,
                cidrs=list(v.vpc.cidrs),
                counts=v.counts,
                region=v.vpc.region,
                aws_vpc_id=v.vpc.aws_vpc_id,
                is_watchly_vpc=v.vpc.is_watchly_vpc,
                unreachable_since=v.vpc.unreachable_since,
                account=AccountBrief.model_validate(v.vpc.account),
                project=ProjectBrief.model_validate(v.vpc.project),
            )
            for v in data["views"]
        ],
        by_kind=[KindCounts(**row) for row in data["by_kind"]],
        attention=attention,
    )


@router.get(
    "/self",
    response_model=SelfRead,
    summary="Where Watchly runs and what its own role may do",
    responses={403: {"description": "Requires admin or DevOps"}},
)
async def where_am_i(actor: User = Depends(require_manager)) -> SelfRead:
    """Read from the instance metadata; each permission is tried with the
    smallest read. `instance` is null off EC2. Each AWS account's credentials
    are tested with `POST /accounts/{id}/test`."""
    return await diagnose_module.self_report()


__all__ = ["router"]
