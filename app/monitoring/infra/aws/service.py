"""VPCs, resources and checks: CRUD and who may see what.

No HTTP concerns and no probing. Visibility follows websites: admin and
DevOps see everything; everyone else sees the resources of the projects they
own or belong to (and any they are an extra recipient of), and the VPCs
granted to those projects. Something the caller cannot see reads as not found.
"""

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete, func, literal_column, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.core.instance_metadata import InstanceMetadata
from app.core.permissions import can_manage_project, can_view_all_projects
from app.db.models.user import User
from app.monitoring import egress
from app.monitoring.infra.aws import client, discovery, topology
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.capacity import forget_capacity
from app.monitoring.infra.aws.deployments import stop_watching
from app.monitoring.infra.aws.models import (
    CHECKS_BY_KIND,
    AccountAuth,
    AwsAccount,
    AwsCheck,
    AwsCheckResult,
    AwsDeployment,
    AwsEvent,
    AwsMaintenanceWindow,
    AwsResource,
    AwsVpc,
    CheckHealth,
    InfraCheckType,
    ResourceKind,
    ResourceState,
    ResourceStatus,
    maintenance_in_effect,
    recipient_resource_ids,
    resource_state_sql,
)
from app.monitoring.infra.aws.probes.target_health import target_group_name
from app.monitoring.infra.aws.credentials import AccountInput
from app.monitoring.infra.aws.schemas import (
    AccountBrief,
    AccountCreate,
    AccountUpdate,
    AvailableVpc,
    AvailableVpcs,
    CheckCreate,
    CheckUpdate,
    DiscoveredItem,
    DiscoveryResult,
    ImportRequest,
    MonitoredBy,
    ProjectBrief,
    ResourceCreate,
    ResourceSort,
    ResourceUpdate,
    StateCounts,
    Topology,
    VpcCreate,
    VpcUpdate,
    default_timeout,
    validate_settings,
)
from app.monitoring.projects.models import Project, ProjectMonitors, visible_project_ids
from app.monitoring.projects.service import (
    ProjectForbiddenError,
    ProjectNotFoundError,
    ProjectService,
    resolve_users,
)
from app.monitoring.websites.schemas import MAX_MAINTENANCE_MINUTES, MaintenanceCreate


@dataclass(frozen=True, slots=True)
class ScalingChange:
    """Instances that joined or left an Auto Scaling group between two syncs."""

    resource: AwsResource
    added: tuple[dict, ...]
    removed: tuple[str, ...]


class InfraError(Exception):
    """Base class; each subclass maps to one HTTP status."""


class InfraNotFoundError(InfraError):
    def __init__(self, what: str, ident: object) -> None:
        super().__init__(f"No {what} with id {ident}.")


class InfraForbiddenError(InfraError):
    pass


class InfraConflictError(InfraError):
    pass


class InfraInvalidError(InfraError):
    pass


class InfraAwsError(InfraError):
    """AWS could not be asked (502)."""


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def is_manager(actor: User) -> bool:
    """Admin and DevOps: who runs VPCs."""
    return can_view_all_projects(actor.role)


def _visible_project_filter(actor: User, column):
    return column.in_(visible_project_ids(actor.id))


def visible_vpc_ids(actor: User):
    """Subquery of the VPCs in the AWS accounts of projects `actor` can see."""
    return (
        select(AwsVpc.id)
        .join(AwsAccount, AwsAccount.id == AwsVpc.account_id)
        .where(_visible_project_filter(actor, AwsAccount.project_id))
    )


def manageable_project_ids(actor: User):
    """Subquery of the projects a project manager may manage: their own."""
    return select(Project.id).where(Project.owner_id == actor.id)


def default_check_name(check_type: InfraCheckType, settings_: dict, kind: ResourceKind | None = None) -> str:
    each = " on each instance" if kind is ResourceKind.AUTO_SCALING_GROUP else ""
    match check_type:
        case InfraCheckType.PING:
            return "ping each instance" if each else "ping"
        case InfraCheckType.TCP:
            return (f"tcp {settings_['port']}" if settings_.get("port") else "tcp") + each
        case InfraCheckType.HTTP:
            return f"GET {settings_.get('path', '/')}{each}"
        case InfraCheckType.TARGET_HEALTH:
            return f"targets of {target_group_name(settings_['target_group_arn'])}"
        case InfraCheckType.GROUP_HEALTH:
            return "group health"
        case InfraCheckType.DB_STATUS:
            return "database status"
        case InfraCheckType.DB_METRICS:
            return "database metrics"
        case InfraCheckType.EC2_METRICS:
            return "server metrics"
    return check_type.value


def is_watchly_vpc(identity: InstanceMetadata | None, account: AwsAccount, aws_vpc_id: str) -> bool:
    """Whether Watchly's own instance runs in this VPC of this account."""
    if identity is None or identity.vpc_id != aws_vpc_id:
        return False
    return not (identity.account_id and account.aws_account_id and identity.account_id != account.aws_account_id)


#: Account fields that change which credentials it is reached with.
CREDENTIAL_FIELDS = frozenset({"auth_type", "access_key_id", "secret_access_key", "role_arn", "external_id"})


def new_account_row(payload: AccountInput, actor: User) -> AwsAccount:
    """An unsaved account, its secret encrypted."""
    return AwsAccount(
        name=payload.name.strip(),
        description=payload.description,
        auth_type=payload.auth_type,
        access_key_id=payload.access_key_id,
        secret_access_key=encrypt_secret(payload.secret_access_key) if payload.secret_access_key else None,
        role_arn=payload.role_arn,
        external_id=payload.external_id,
        default_region=payload.default_region,
        environment=payload.environment,
        watch_deployments=bool(payload.watch_deployments),
        watch_capacity=bool(payload.watch_capacity),
        created_by_id=actor.id,
    )


async def verify_with_aws(account: AwsAccount) -> None:
    """Ask STS who the account's credentials are, and record its 12-digit
    id. GetCallerIdentity needs no permission, so a refusal means the
    credentials (or the role's trust policy) are wrong."""
    credentials = client.Account.of(account)
    region = await client.default_region(credentials) or "us-east-1"
    try:
        caller = await client.call("sts", "get_caller_identity", client.Region(credentials, region))
    except AwsError as exc:
        raise InfraInvalidError(f"AWS refused the credentials of {account.name}: {exc}") from exc
    account.aws_account_id = caller.get("Account")
    account.verified_at = datetime.now(UTC)
    account.last_error = None


async def new_project_accounts(inputs: list[AccountInput], actor: User) -> list[AwsAccount]:
    """A new infrastructure project's accounts, each tried with AWS; nothing
    is saved. The same AWS account may not be given twice."""
    rows: list[AwsAccount] = []
    for payload in inputs:
        row = new_account_row(payload, actor)
        await verify_with_aws(row)
        twin = next((r for r in rows if r.aws_account_id == row.aws_account_id), None)
        if twin is not None:
            raise InfraConflictError(
                f"{twin.name} and {row.name} are the same AWS account, {row.aws_account_id}."
            )
        rows.append(row)
    return rows


def assert_vpc_of(vpc: AwsVpc, project: Project) -> None:
    """A resource lives in a VPC of one of its own project's AWS accounts."""
    if vpc.account.project_id != project.id:
        raise InfraInvalidError(
            f"{vpc.name} is in an AWS account of project {vpc.account.project.name}, not of {project.name}: "
            "add the account to this project, or add the resource there."
        )


@dataclass(slots=True)
class VpcView:
    """A VPC with what its read needs besides its own columns."""

    vpc: AwsVpc
    counts: StateCounts
    health: str


class InfraService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.projects = ProjectService(session)

    # ======================================================================
    # AWS accounts: each belongs to one infrastructure project
    # ======================================================================

    async def list_accounts(self, actor: User, project_id: int | None = None) -> list[tuple[AwsAccount, int]]:
        """The accounts of the projects `actor` may manage (every one, for
        admin and DevOps), with how many VPCs each holds."""
        filters = []
        if project_id is not None:
            await self.project_for_write(project_id, actor)
            filters.append(AwsAccount.project_id == project_id)
        if not is_manager(actor):
            filters.append(AwsAccount.project_id.in_(manageable_project_ids(actor)))
        counts = dict(
            (await self.session.execute(select(AwsVpc.account_id, func.count()).group_by(AwsVpc.account_id))).all()
        )
        rows = await self.session.scalars(
            select(AwsAccount).where(*filters).order_by(AwsAccount.project_id, func.lower(AwsAccount.name))
        )
        return [(account, counts.get(account.id, 0)) for account in rows]

    async def get_account(self, account_id: int, actor: User) -> AwsAccount:
        """An account whose project `actor` may manage. One whose project they
        cannot see is not found."""
        account = await self.session.get(AwsAccount, account_id)
        if account is None:
            raise InfraNotFoundError("AWS account", account_id)
        try:
            await self.projects.get_for_write(account.project_id, actor)
        except ProjectNotFoundError as exc:
            raise InfraNotFoundError("AWS account", account_id) from exc
        except ProjectForbiddenError as exc:
            raise InfraForbiddenError(str(exc)) from exc
        return account

    async def vpc_count(self, account_id: int) -> int:
        return await self.session.scalar(select(func.count(AwsVpc.id)).where(AwsVpc.account_id == account_id)) or 0

    async def _infra_project(self, project_id: int, actor: User) -> Project:
        project = await self.project_for_write(project_id, actor)
        if project.monitors is not ProjectMonitors.INFRASTRUCTURE:
            raise InfraInvalidError(
                f"{project.name} monitors websites: AWS accounts belong to infrastructure projects."
            )
        return project

    async def _assert_account_name_free(self, project_id: int, name: str, except_id: int | None = None) -> None:
        taken = await self.session.scalar(
            select(AwsAccount.id).where(
                AwsAccount.project_id == project_id,
                func.lower(AwsAccount.name) == name.lower(),
                AwsAccount.id != (except_id or 0),
            )
        )
        if taken:
            raise InfraConflictError(f"This project already has an AWS account named {name!r}.")

    async def _verify(self, account: AwsAccount, *, previous_aws_id: str | None = None) -> None:
        """Try the credentials before they are saved. They must reach the
        AWS account its VPCs are in, and one AWS account is added to a project
        once."""
        await verify_with_aws(account)
        found = account.aws_account_id
        if previous_aws_id and found != previous_aws_id and await self.vpc_count(account.id or 0):
            raise InfraConflictError(
                f"These credentials reach AWS account {found}, but {account.name}'s VPCs are in "
                f"{previous_aws_id}: add them as an account of their own."
            )
        other = await self.session.scalar(
            select(AwsAccount).where(
                AwsAccount.project_id == account.project_id,
                AwsAccount.aws_account_id == found,
                AwsAccount.id != (account.id or 0),
            )
        )
        if other is not None:
            raise InfraConflictError(f"AWS account {found} is already in this project as {other.name}.")

    async def create_account(self, payload: AccountCreate, actor: User) -> AwsAccount:
        project = await self._infra_project(payload.project_id, actor)
        await self._assert_account_name_free(project.id, payload.name.strip())
        account = new_account_row(payload, actor)
        account.project_id = project.id
        await self._verify(account)
        account.project = project
        self.session.add(account)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise InfraConflictError("That account name is already taken in this project.") from exc
        await self.session.refresh(account)
        return account

    async def update_account(self, account_id: int, payload: AccountUpdate, actor: User) -> AwsAccount:
        account = await self.get_account(account_id, actor)
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("name") and changes["name"].strip() != account.name:
            await self._assert_account_name_free(account.project_id, changes["name"].strip(), except_id=account.id)
            account.name = changes["name"].strip()
        for key in ("description", "default_region", "environment"):
            if key in changes:
                setattr(account, key, changes[key])
        if changes.get("watch_deployments") is not None and changes["watch_deployments"] != account.watch_deployments:
            if not changes["watch_deployments"]:
                await stop_watching(self.session, account)
            account.watch_deployments = changes["watch_deployments"]
        if changes.get("watch_capacity") is not None and changes["watch_capacity"] != account.watch_capacity:
            # Off forgets what it found, so turning it back on starts afresh
            # rather than alerting on problems remembered from before.
            forget_capacity(account)
            account.watch_capacity = changes["watch_capacity"]

        if CREDENTIAL_FIELDS & changes.keys():
            auth = changes.get("auth_type") or account.auth_type
            if auth is AccountAuth.ACCESS_KEY:
                if "access_key_id" in changes and not changes.get("secret_access_key"):
                    raise InfraInvalidError("A new access_key_id needs its secret_access_key.")
                if changes.get("access_key_id"):
                    account.access_key_id = changes["access_key_id"]
                if changes.get("secret_access_key"):
                    account.secret_access_key = encrypt_secret(changes["secret_access_key"])
                if not (account.access_key_id and account.secret_access_key):
                    raise InfraInvalidError("An access_key account needs access_key_id and secret_access_key.")
            elif changes.get("access_key_id") or changes.get("secret_access_key"):
                raise InfraInvalidError("A default account uses Watchly's own credentials: leave the access key out.")
            else:
                account.access_key_id = account.secret_access_key = None
            account.auth_type = auth
            if "role_arn" in changes:
                account.role_arn = changes["role_arn"]
                if not account.role_arn:
                    account.external_id = None
            if "external_id" in changes:
                account.external_id = changes["external_id"]
            if account.external_id and not account.role_arn:
                raise InfraInvalidError("external_id goes with role_arn.")
            try:
                await self._verify(account, previous_aws_id=account.aws_account_id)
            except InfraError:
                await self.session.rollback()
                raise
        await self.session.commit()
        await self.session.refresh(account)
        return account

    async def delete_account(self, account_id: int, actor: User) -> None:
        account = await self.get_account(account_id, actor)
        names = list(
            await self.session.scalars(select(AwsVpc.name).where(AwsVpc.account_id == account_id).limit(20))
        )
        if names:
            raise InfraConflictError(
                f"{account.name} still holds VPCs ({', '.join(names)}): remove their resources and VPCs first."
            )
        others = await self.session.scalar(
            select(func.count(AwsAccount.id)).where(
                AwsAccount.project_id == account.project_id, AwsAccount.id != account.id
            )
        )
        if not others:
            raise InfraConflictError(
                "An infrastructure project keeps at least one AWS account: add another before removing this one."
            )
        await self.session.delete(account)
        await self.session.commit()

    # ======================================================================
    # VPCs
    # ======================================================================

    async def get_vpc(self, vpc_id: int) -> AwsVpc:
        vpc = await self.session.get(AwsVpc, vpc_id)
        if vpc is None:
            raise InfraNotFoundError("VPC", vpc_id)
        return vpc

    async def get_visible_vpc(self, vpc_id: int, actor: User) -> AwsVpc:
        """A VPC of a project `actor` can see."""
        vpc = await self.get_vpc(vpc_id)
        if is_manager(actor) or vpc.account.project.includes_user(actor.id):
            return vpc
        raise InfraNotFoundError("VPC", vpc_id)

    async def get_vpc_for_write(self, vpc_id: int, actor: User) -> AwsVpc:
        """A VPC of a project `actor` may manage."""
        vpc = await self.get_visible_vpc(vpc_id, actor)
        if not can_manage_project(actor.role, actor.id, vpc.account.project.owner_id):
            raise InfraForbiddenError("Requires admin/DevOps, or ownership of the project.")
        return vpc

    async def _state_counts(self, actor: User, *filters) -> dict[int, StateCounts]:
        """Resource counts per VPC, over what `actor` can see."""
        state = resource_state_sql()
        rows = await self.session.execute(
            select(AwsResource.vpc_id, state.label("state"), func.count())
            .where(*self._visible_resource_filters(actor), *filters)
            .group_by(AwsResource.vpc_id, state)
        )
        counts: dict[int, StateCounts] = {}
        for vpc_id, state_value, n in rows:
            entry = counts.setdefault(vpc_id, StateCounts())
            setattr(entry, state_value, getattr(entry, state_value) + n)
            entry.total += n
        return counts

    async def _tested(self, vpc_ids: list[int]) -> set[int]:
        """VPCs with at least one checked resource."""
        if not vpc_ids:
            return set()
        rows = await self.session.scalars(
            select(AwsResource.vpc_id)
            .where(AwsResource.vpc_id.in_(vpc_ids), AwsResource.last_checked_at.is_not(None))
            .distinct()
        )
        return set(rows)

    async def vpc_views(self, vpcs: list[AwsVpc], actor: User) -> list[VpcView]:
        ids = [vpc.id for vpc in vpcs]
        counts = await self._state_counts(actor, AwsResource.vpc_id.in_(ids)) if ids else {}
        tested = await self._tested(ids)
        return [
            VpcView(
                vpc=vpc,
                counts=counts.get(vpc.id, StateCounts()),
                health=(
                    "unreachable"
                    if vpc.unreachable_since
                    else "reachable"
                    if vpc.id in tested
                    else "untested"
                ),
            )
            for vpc in vpcs
        ]

    async def list_vpcs(
        self, actor: User, *, limit: int = 100, offset: int = 0, health: str | None = None
    ) -> tuple[list[VpcView], int]:
        filters = [] if is_manager(actor) else [AwsVpc.id.in_(visible_vpc_ids(actor))]
        rows = list(
            await self.session.scalars(select(AwsVpc).where(*filters).order_by(func.lower(AwsVpc.name)))
        )
        views = await self.vpc_views(rows, actor)
        if health:
            views = [view for view in views if view.health == health]
        return views[offset : offset + limit], len(views)

    async def _account_region(self, account: AwsAccount, region: str | None) -> client.Region:
        credentials = client.Account.of(account)
        name = region or await client.default_region(credentials)
        if not name:
            raise InfraInvalidError(
                f"No AWS region: give `region`, set {account.name}'s default region or AWS_REGION, "
                "or run Watchly on EC2."
            )
        return client.Region(credentials, name)

    async def available_vpcs(self, account_id: int, region: str | None, actor: User) -> AvailableVpcs:
        account = await self.get_account(account_id, actor)
        where = await self._account_region(account, region)
        try:
            vpcs = await client.paginate("ec2", "describe_vpcs", where, "Vpcs")
        except AwsError as exc:
            raise InfraAwsError(f"DescribeVpcs in {account.name}: {exc}") from exc
        identity = await client.identity()
        registered = {
            row.aws_vpc_id: row.id
            for row in await self.session.scalars(
                select(AwsVpc).where(AwsVpc.account_id == account.id, AwsVpc.region == where.name)
            )
        }
        return AvailableVpcs(
            account=AccountBrief.model_validate(account),
            region=where.name,
            vpcs=[
                AvailableVpc(
                    aws_vpc_id=vpc["VpcId"],
                    name=client.tags(vpc.get("Tags")).get("Name"),
                    cidrs=vpc_cidrs(vpc),
                    is_default=bool(vpc.get("IsDefault")),
                    is_watchly_vpc=is_watchly_vpc(identity, account, vpc["VpcId"]),
                    registered_as=registered.get(vpc["VpcId"]),
                )
                for vpc in vpcs
            ],
        )

    async def create_vpc(self, payload: VpcCreate, actor: User) -> AwsVpc:
        """Register a VPC of one of a project's accounts, for that project:
        done when someone first adds a resource from it."""
        account = await self.get_account(payload.account_id, actor)
        where = await self._account_region(account, payload.region)
        region = where.name
        existing = await self.session.scalar(
            select(AwsVpc).where(
                AwsVpc.account_id == account.id, AwsVpc.region == region, AwsVpc.aws_vpc_id == payload.aws_vpc_id
            )
        )
        if existing is not None:
            raise InfraConflictError(f"{payload.aws_vpc_id} is already registered as {existing.name}.")
        try:
            response = await client.call("ec2", "describe_vpcs", where, VpcIds=[payload.aws_vpc_id])
        except AwsError as exc:
            if exc.code.startswith("InvalidVpcID"):
                raise InfraInvalidError(f"No VPC {payload.aws_vpc_id} in {account.name}, {region}.") from exc
            raise InfraAwsError(f"DescribeVpcs in {account.name}: {exc}") from exc
        found = response.get("Vpcs") or []
        if not found:
            raise InfraInvalidError(f"No VPC {payload.aws_vpc_id} in {account.name}, {region}.")
        aws_vpc = found[0]
        cidrs = vpc_cidrs(aws_vpc)
        public = [c for c in cidrs if not egress.is_private(str(ipaddress.ip_network(c).network_address))]
        if public:
            raise InfraInvalidError(f"{', '.join(public)} is not a private range.")
        name = (payload.name or client.tags(aws_vpc.get("Tags")).get("Name") or payload.aws_vpc_id).strip()
        if await self._vpc_name_taken(account.id, name):
            if payload.name:
                raise InfraConflictError(f"{account.name} already has a VPC named {name!r}.")
            # Named after its tag, which two VPCs may share.
            name = f"{name} ({payload.aws_vpc_id})"
        identity = await client.identity()
        vpc = AwsVpc(
            name=name,
            description=payload.description,
            account=account,
            region=region,
            aws_vpc_id=payload.aws_vpc_id,
            cidrs=cidrs,
            is_watchly_vpc=is_watchly_vpc(identity, account, payload.aws_vpc_id),
            created_by_id=actor.id,
        )
        self.session.add(vpc)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise InfraConflictError("That VPC or name is already registered.") from exc
        await self.session.refresh(vpc)
        return vpc

    async def _vpc_name_taken(self, account_id: int, name: str) -> bool:
        return bool(
            await self.session.scalar(select(AwsVpc.id).where(AwsVpc.account_id == account_id, AwsVpc.name == name))
        )

    async def update_vpc(self, vpc_id: int, payload: VpcUpdate, actor: User) -> AwsVpc:
        vpc = await self.get_vpc_for_write(vpc_id, actor)
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("name") and changes["name"] != vpc.name:
            if await self._vpc_name_taken(vpc.account_id, changes["name"].strip()):
                raise InfraConflictError(f"{vpc.account.name} already has a VPC named {changes['name']!r}.")
            vpc.name = changes["name"].strip()
        if "description" in changes:
            vpc.description = changes["description"]
        await self.session.commit()
        await self.session.refresh(vpc)
        return vpc

    async def _resource_names(self, *filters) -> list[str]:
        rows = await self.session.scalars(select(AwsResource.name).where(*filters).limit(20))
        return list(rows)

    async def delete_vpc(self, vpc_id: int, actor: User, *, with_resources: bool = False) -> None:
        """Forget a VPC. With `with_resources`, its monitored resources go too,
        with their checks, history and events (the foreign keys cascade);
        without, a VPC that still holds any is refused."""
        vpc = await self.get_vpc_for_write(vpc_id, actor)
        if with_resources:
            await self.session.execute(delete(AwsResource).where(AwsResource.vpc_id == vpc_id))
        else:
            names = await self._resource_names(AwsResource.vpc_id == vpc_id)
            if names:
                raise InfraConflictError(
                    f"{vpc.name} still holds resources ({', '.join(names)}): delete them first, "
                    "or delete the VPC with with_resources=true."
                )
        await self.session.delete(vpc)
        await self.session.commit()

    async def refresh_vpc_cidrs(self, vpc: AwsVpc) -> list[str]:
        """Read the VPC's ranges again; raises AwsError."""
        response = await client.call("ec2", "describe_vpcs", client.Region.of(vpc), VpcIds=[vpc.aws_vpc_id])
        found = response.get("Vpcs") or []
        if found:
            cidrs = vpc_cidrs(found[0])
            if cidrs and cidrs != list(vpc.cidrs):
                vpc.cidrs = cidrs
        return list(vpc.cidrs)

    # ======================================================================
    # Resources
    # ======================================================================

    @staticmethod
    def _visible_resource_filters(actor: User) -> list:
        if is_manager(actor):
            return []
        return [
            or_(
                _visible_project_filter(actor, AwsResource.project_id),
                AwsResource.id.in_(recipient_resource_ids(actor.id)),
            )
        ]

    async def get_resource(self, resource_id: int) -> AwsResource:
        resource = await self.session.get(AwsResource, resource_id)
        if resource is None:
            raise InfraNotFoundError("resource", resource_id)
        return resource

    async def reload(self, resource: AwsResource) -> AwsResource:
        """Read the resource, and everything its read shows, afresh. A plain
        refresh leaves its relationships unloaded, and a lazy load is not
        allowed under async SQLAlchemy. Raises NoResultFound once deleted."""
        return (
            await self.session.scalars(
                select(AwsResource)
                .where(AwsResource.id == resource.id)
                .options(
                    selectinload(AwsResource.checks),
                    selectinload(AwsResource.recipients),
                    selectinload(AwsResource.maintenance_windows),
                    selectinload(AwsResource.project),
                    selectinload(AwsResource.vpc),
                )
                .execution_options(populate_existing=True)
            )
        ).one()

    async def get_visible_resource(self, resource_id: int, actor: User) -> AwsResource:
        resource = await self.get_resource(resource_id)
        if is_manager(actor) or resource.project.includes_user(actor.id):
            return resource
        if any(user.id == actor.id for user in resource.recipients):
            return resource
        raise InfraNotFoundError("resource", resource_id)

    async def get_resource_for_write(self, resource_id: int, actor: User) -> AwsResource:
        """A resource the actor may change: rights over its project. One they
        cannot see is not found; one they can see but not manage, forbidden."""
        resource = await self.get_visible_resource(resource_id, actor)
        if not can_manage_project(actor.role, actor.id, resource.project.owner_id):
            raise InfraForbiddenError("Requires admin/DevOps, or ownership of the project.")
        return resource

    async def project_for_write(self, project_id: int, actor: User) -> Project:
        try:
            return await self.projects.get_for_write(project_id, actor)
        except ProjectNotFoundError as exc:
            raise InfraNotFoundError("project", project_id) from exc
        except ProjectForbiddenError as exc:
            raise InfraForbiddenError(str(exc)) from exc

    def _resource_filters(
        self,
        actor: User,
        *,
        vpc_id: int | None = None,
        project_id: int | None = None,
        kind: ResourceKind | None = None,
        environment=None,
        q: str | None = None,
    ) -> list:
        filters = self._visible_resource_filters(actor)
        if vpc_id is not None:
            filters.append(AwsResource.vpc_id == vpc_id)
        if project_id is not None:
            filters.append(AwsResource.project_id == project_id)
        if kind is not None:
            filters.append(AwsResource.kind == kind)
        if environment is not None:
            filters.append(AwsResource.environment == environment)
        if q and q.strip():
            term = q.strip()
            filters.append(
                or_(
                    AwsResource.name.icontains(term, autoescape=True),
                    AwsResource.aws_id.icontains(term, autoescape=True),
                    AwsResource.address.icontains(term, autoescape=True),
                )
            )
        return filters

    async def list_resources(
        self,
        actor: User,
        *,
        limit: int = 50,
        offset: int = 0,
        state: ResourceState | None = None,
        sort: ResourceSort = ResourceSort.STATE,
        **scope,
    ) -> tuple[list[AwsResource], int]:
        filters = self._resource_filters(actor, **scope)
        state_sql = resource_state_sql()
        if state is not None:
            filters.append(state_sql == state.value)
        order = [func.lower(AwsResource.name), AwsResource.id]
        if sort is ResourceSort.STATE:
            rank = case(
                (state_sql == ResourceState.DOWN.value, 0),
                (state_sql == ResourceState.DEGRADED.value, 1),
                (state_sql == ResourceState.MISSING.value, 2),
                else_=3,
            )
            order.insert(0, rank)
        total = await self.session.scalar(select(func.count()).select_from(AwsResource).where(*filters))
        rows = await self.session.scalars(
            select(AwsResource).where(*filters).order_by(*order).limit(limit).offset(offset)
        )
        return list(rows), total or 0

    async def summary(self, actor: User, **scope) -> StateCounts:
        state = resource_state_sql()
        rows = await self.session.execute(
            select(state, func.count())
            .select_from(AwsResource)
            .where(*self._resource_filters(actor, **scope))
            .group_by(state)
        )
        counts = StateCounts()
        for state_value, n in rows:
            setattr(counts, state_value, n)
            counts.total += n
        return counts

    async def _lookup_in_aws(self, vpc: AwsVpc, kind: ResourceKind, aws_id: str) -> discovery.AwsResourceInfo:
        try:
            info = await discovery.describe_one(client.Region.of(vpc), vpc.aws_vpc_id, kind, aws_id)
        except AwsError as exc:
            raise InfraAwsError(f"{exc.operation}: {exc}") from exc
        if info is None:
            noun = {ResourceKind.SERVER: "EC2 instance",
                    ResourceKind.LOAD_BALANCER: "Application or Network Load Balancer",
                    ResourceKind.AUTO_SCALING_GROUP: "EC2 Auto Scaling group",
                    ResourceKind.DATABASE: "RDS DB instance"}[kind]
            raise InfraInvalidError(f"No {noun} {aws_id} in {vpc.name} ({vpc.aws_vpc_id}).")
        return info

    async def _assert_not_monitored(self, vpc_id: int, aws_id: str, actor: User) -> None:
        other = await self.session.scalar(
            select(AwsResource).where(AwsResource.vpc_id == vpc_id, AwsResource.aws_id == aws_id)
        )
        if other is None:
            return
        visible = is_manager(actor) or other.project.includes_user(actor.id)
        where = f" by project {other.project.name}" if visible else " by another project"
        raise InfraConflictError(f"{other.name} is already monitored{where}.")

    async def _vpc_for_project(self, vpc_id: int, project: Project, actor: User) -> AwsVpc:
        vpc = await self.get_visible_vpc(vpc_id, actor)
        assert_vpc_of(vpc, project)
        return vpc

    async def create_resource(self, payload: ResourceCreate, actor: User) -> AwsResource:
        project = await self.project_for_write(payload.project_id, actor)
        vpc = await self._vpc_for_project(payload.vpc_id, project, actor)
        info = await self._lookup_in_aws(vpc, payload.kind, payload.aws_id.strip())
        await self._assert_not_monitored(vpc.id, info.aws_id, actor)
        resource = await self._new_resource(
            vpc, project, info, actor,
            name=payload.name,
            environment=payload.environment,
            max_down_alerts=payload.max_down_alerts,
            is_enabled=payload.is_enabled,
            checks=payload.checks,
        )
        if resource.kind is ResourceKind.AUTO_SCALING_GROUP:
            resource.notify_scale_out = payload.notify_scale_out
            resource.notify_scale_in = payload.notify_scale_in
            resource.scale_health_check = (
                payload.scale_health_check.model_dump() if payload.scale_health_check else None
            )
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise InfraConflictError(f"{info.name} is already monitored.") from exc
        await self.reload(resource)
        return resource

    async def _new_resource(
        self,
        vpc: AwsVpc,
        project: Project,
        info: discovery.AwsResourceInfo,
        actor: User,
        *,
        name: str | None,
        environment,
        checks: list[CheckCreate] | None,
        max_down_alerts: int = 4,
        is_enabled: bool = True,
    ) -> AwsResource:
        """Build a resource and its checks in the session, validated; the
        caller commits."""
        resource = AwsResource(
            project=project,
            vpc=vpc,
            kind=info.kind,
            aws_id=info.aws_id,
            aws_arn=info.aws_arn,
            name=(name or info.name).strip(),
            environment=environment if environment is not None else vpc.account.environment,
            address=info.address,
            aws_state=info.aws_state,
            aws_detail=info.aws_detail,
            synced_at=datetime.now(UTC),
            max_down_alerts=max_down_alerts,
            is_enabled=is_enabled,
            scaling_members=discovery.in_service_ids(info.aws_detail) if info.kind is ResourceKind.AUTO_SCALING_GROUP else None,
            created_by_id=actor.id,
        )
        resource.checks = []
        wanted = checks
        if wanted is None:
            wanted = [
                CheckCreate(check_type=s.check_type, name=s.name, settings=s.settings)
                for s in discovery.suggest(info)
            ]
        if not wanted:
            raise InfraInvalidError("A resource needs at least one check.")
        for check in wanted:
            resource.checks.append(await self._build_check(resource, check))
        self.session.add(resource)
        return resource

    async def update_resource(self, resource: AwsResource, payload: ResourceUpdate, actor: User) -> AwsResource:
        changes = payload.model_dump(exclude_unset=True)
        for field in ("name", "environment", "max_down_alerts", "is_enabled"):
            if field in changes and (changes[field] is not None or field == "environment"):
                setattr(resource, field, changes[field].strip() if field == "name" else changes[field])
        if resource.kind is ResourceKind.AUTO_SCALING_GROUP:
            for field in ("notify_scale_out", "notify_scale_in"):
                if changes.get(field) is not None:
                    setattr(resource, field, changes[field])
            if "scale_health_check" in changes:
                resource.scale_health_check = changes["scale_health_check"]
        elif any(changes.get(f) for f in ("notify_scale_out", "notify_scale_in", "scale_health_check")):
            raise InfraInvalidError("Scaling notifications are for Auto Scaling groups only.")
        if changes.get("is_enabled") is False:
            # Paused: its outage is over as far as anyone is told.
            resource.status = ResourceStatus.UNKNOWN
            resource.down_since = None
            resource.degraded_since = None
            resource.down_alerts_sent = 0
            resource.down_alert_held = False
            for check in resource.checks:
                check.health = CheckHealth.UNKNOWN
                check.consecutive_failures = 0
        await self.session.commit()
        await self.reload(resource)
        return resource

    async def delete_resource(self, resource: AwsResource) -> None:
        await self.session.delete(resource)
        await self.session.commit()

    async def add_recipients(self, resource: AwsResource, user_ids: list[int]) -> AwsResource:
        existing = {user.id for user in resource.recipients}
        for user in await resolve_users(self.session, user_ids):
            if user.id not in existing:
                resource.recipients.append(user)
        await self.session.commit()
        await self.reload(resource)
        return resource

    async def remove_recipient(self, resource: AwsResource, user_id: int) -> AwsResource:
        resource.recipients = [u for u in resource.recipients if u.id != user_id]
        await self.session.commit()
        await self.reload(resource)
        return resource

    # -- checks --------------------------------------------------------------

    async def _build_check(
        self,
        resource: AwsResource,
        payload: CheckCreate,
        existing: AwsCheck | None = None,
    ) -> AwsCheck:
        """A check for `resource`, validated against its kind, public IP,
        listeners and target groups. With `existing`, those fields are updated
        instead."""
        check_type = payload.check_type
        if check_type not in CHECKS_BY_KIND[resource.kind]:
            allowed = ", ".join(sorted(t.value for t in CHECKS_BY_KIND[resource.kind]))
            raise InfraInvalidError(f"A {resource.kind.value} takes {allowed} checks, not {check_type.value}.")
        settings_ = payload.settings
        detail = resource.aws_detail or {}
        if settings_.get("use_public_ip"):
            if resource.kind is ResourceKind.LOAD_BALANCER:
                raise InfraInvalidError(
                    "Only a server's or an Auto Scaling group's checks choose public IPs: an "
                    "internet-facing load balancer is reached over the internet already."
                )
            if resource.kind is ResourceKind.DATABASE:
                raise InfraInvalidError("A database is checked at its endpoint, inside the VPC.")
            # An instance stopped without an Elastic IP loses its public one;
            # its check then fails, but stays editable. A group's instances
            # come and go, so each run judges the ones it has.
            if (
                resource.kind is ResourceKind.SERVER
                and not detail.get("public_ip")
                and (existing is None or existing.settings != settings_)
            ):
                raise InfraInvalidError(f"{resource.name} has no public IP: check its private IP instead.")
        if settings_.get("min_healthy_instances") is not None and resource.kind is not ResourceKind.AUTO_SCALING_GROUP:
            raise InfraInvalidError("min_healthy_instances is for an Auto Scaling group's checks.")
        if check_type is InfraCheckType.TCP and resource.kind is ResourceKind.LOAD_BALANCER:
            ports = sorted({row.get("port") for row in detail.get("listeners", []) if row.get("port")})
            if ports and settings_["port"] not in ports:
                listed = ", ".join(map(str, ports))
                raise InfraInvalidError(f"{resource.name} listens on {listed}, not {settings_['port']}.")
        if check_type is InfraCheckType.TARGET_HEALTH:
            known = [g.get("arn") for g in detail.get("target_groups", [])]
            if known and settings_["target_group_arn"] not in known:
                raise InfraInvalidError(f"That target group is not one of {resource.name}'s.")
        others = [c for c in resource.checks if c is not existing]
        if existing is None and len(others) >= settings.INFRA_MAX_CHECKS_PER_RESOURCE:
            raise InfraInvalidError(
                f"A resource has at most {settings.INFRA_MAX_CHECKS_PER_RESOURCE} checks."
            )
        if any(c.check_type is check_type and c.settings == settings_ for c in others):
            raise InfraConflictError("The same check already exists on this resource.")

        check = existing or AwsCheck(check_type=check_type)
        check.name = (payload.name or default_check_name(check_type, settings_, resource.kind)).strip()
        check.settings = settings_
        check.check_interval_seconds = payload.check_interval_seconds
        check.timeout_seconds = payload.timeout_seconds or default_timeout(check_type)
        check.retries_on_failure = payload.retries_on_failure
        check.is_enabled = payload.is_enabled
        return check

    def get_check(self, resource: AwsResource, check_id: int) -> AwsCheck:
        check = next((c for c in resource.checks if c.id == check_id), None)
        if check is None:
            raise InfraNotFoundError("check on this resource", check_id)
        return check

    async def add_check(self, resource: AwsResource, payload: CheckCreate) -> AwsResource:
        check = await self._build_check(resource, payload)
        resource.checks.append(check)
        await self.session.commit()
        await self.reload(resource)
        return resource

    async def update_check(self, resource: AwsResource, check_id: int, payload: CheckUpdate) -> AwsResource:
        check = self.get_check(resource, check_id)
        changes = payload.model_dump(exclude_unset=True)
        try:
            merged = CheckCreate(
                check_type=check.check_type,
                name=changes.get("name") or check.name,
                settings=(
                    validate_settings(check.check_type, changes["settings"])
                    if changes.get("settings") is not None
                    else check.settings
                ),
                check_interval_seconds=changes.get("check_interval_seconds") or check.check_interval_seconds,
                timeout_seconds=changes.get("timeout_seconds") or check.timeout_seconds,
                retries_on_failure=(
                    changes["retries_on_failure"]
                    if changes.get("retries_on_failure") is not None
                    else check.retries_on_failure
                ),
                is_enabled=changes["is_enabled"] if changes.get("is_enabled") is not None else check.is_enabled,
            )
        except ValueError as exc:
            raise InfraInvalidError(str(exc)) from exc
        settings_changed = merged.settings != check.settings
        await self._build_check(resource, merged, existing=check)
        if settings_changed or not check.is_enabled:
            # What it learned was about other settings.
            check.health = CheckHealth.UNKNOWN
            check.consecutive_failures = 0
            check.problems = {}
        await self.session.commit()
        await self.reload(resource)
        return resource

    async def delete_check(self, resource: AwsResource, check_id: int) -> AwsResource:
        check = self.get_check(resource, check_id)
        if len(resource.checks) <= 1:
            raise InfraConflictError("This is the resource's last check: delete the resource instead.")
        resource.checks.remove(check)
        await self.session.delete(check)
        await self.session.commit()
        await self.reload(resource)
        return resource

    # -- maintenance ---------------------------------------------------------

    async def in_maintenance(self, resource_id: int, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return bool(
            await self.session.scalar(
                select(AwsResource.id).where(AwsResource.id == resource_id, maintenance_in_effect(now))
            )
        )

    async def schedule_maintenance(
        self, resource: AwsResource, payload: MaintenanceCreate, created_by_id: int | None
    ) -> AwsResource:
        now = datetime.now(UTC)
        starts_at = max(payload.starts_at or now, now)
        ends_at = payload.ends_at or starts_at + timedelta(minutes=payload.duration_minutes)
        if ends_at <= starts_at:
            raise InfraInvalidError("ends_at must be later than starts_at, and in the future.")
        if ends_at - starts_at > timedelta(minutes=MAX_MAINTENANCE_MINUTES):
            raise InfraInvalidError(
                f"Maintenance lasts at most {MAX_MAINTENANCE_MINUTES // (24 * 60)} days: pause the resource for longer."
            )
        clash = await self.session.scalar(
            select(AwsMaintenanceWindow)
            .where(
                AwsMaintenanceWindow.resource_id == resource.id,
                AwsMaintenanceWindow.starts_at < ends_at,
                AwsMaintenanceWindow.ends_at > starts_at,
            )
            .limit(1)
        )
        if clash is not None:
            raise InfraConflictError(
                f"The resource already has maintenance from {_utc(clash.starts_at)} to "
                f"{_utc(clash.ends_at)}: end or cancel that one first."
            )
        self.session.add(
            AwsMaintenanceWindow(
                resource_id=resource.id,
                starts_at=starts_at,
                ends_at=ends_at,
                reason=payload.reason,
                created_by_id=created_by_id,
            )
        )
        await self.session.commit()
        await self.reload(resource)
        return resource

    async def end_maintenance(self, resource: AwsResource) -> AwsResource:
        """End every window in effect: one of its own, and one a deployment
        opened over it, which the deployment then no longer renews."""
        now = datetime.now(UTC)
        windows = await self.session.scalars(
            select(AwsMaintenanceWindow).where(
                AwsMaintenanceWindow.resource_id == resource.id,
                AwsMaintenanceWindow.starts_at <= now,
                AwsMaintenanceWindow.ends_at > now,
            )
        )
        for window in windows:
            if window.starts_at == now:
                await self.session.delete(window)
            else:
                window.ends_at = now
        await self.session.commit()
        await self.reload(resource)
        return resource

    async def cancel_maintenance(self, resource: AwsResource, window_id: int) -> AwsResource:
        now = datetime.now(UTC)
        window = await self.session.get(AwsMaintenanceWindow, window_id)
        if window is None or window.resource_id != resource.id or window.ends_at <= now:
            raise InfraNotFoundError("upcoming maintenance window on this resource", window_id)
        if window.starts_at <= now:
            raise InfraConflictError("That maintenance has already started: end it instead.")
        await self.session.delete(window)
        await self.session.commit()
        await self.reload(resource)
        return resource

    # -- history and feed ----------------------------------------------------

    async def results(
        self,
        resource: AwsResource,
        *,
        check_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
        since: datetime | None = None,
    ) -> tuple[list[AwsCheckResult], int]:
        filters = [AwsCheckResult.resource_id == resource.id]
        if check_id is not None:
            filters.append(AwsCheckResult.check_id == check_id)
        if since is not None:
            filters.append(AwsCheckResult.checked_at >= since)
        total = await self.session.scalar(select(func.count()).select_from(AwsCheckResult).where(*filters))
        rows = await self.session.scalars(
            select(AwsCheckResult)
            .where(*filters)
            .order_by(AwsCheckResult.checked_at.desc(), AwsCheckResult.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), total or 0

    async def events(
        self, actor: User, *, after_id: int | None = None, limit: int = 20
    ) -> tuple[list[AwsEvent], int]:
        filters = []
        if not is_manager(actor):
            filters.append(
                or_(
                    _visible_project_filter(actor, AwsEvent.project_id),
                    AwsEvent.resource_id.in_(recipient_resource_ids(actor.id)),
                )
            )
        if after_id is not None:
            filters.append(AwsEvent.id > after_id)
        total = await self.session.scalar(select(func.count(AwsEvent.id)).where(*filters))
        rows = await self.session.scalars(
            select(AwsEvent)
            .options(selectinload(AwsEvent.resource), selectinload(AwsEvent.vpc))
            .where(*filters)
            .order_by(AwsEvent.id.desc())
            .limit(limit)
        )
        return list(rows), total or 0

    async def purge_old(self, now: datetime | None = None) -> int:
        """Events, ended maintenance windows and ended deployments past
        CHECK_RETENTION_DAYS."""
        before = (now or datetime.now(UTC)) - timedelta(days=settings.CHECK_RETENTION_DAYS)
        events = await self.session.execute(delete(AwsEvent).where(AwsEvent.occurred_at < before))
        windows = await self.session.execute(
            delete(AwsMaintenanceWindow).where(AwsMaintenanceWindow.ends_at < before)
        )
        ended = await self.session.execute(delete(AwsDeployment).where(AwsDeployment.finished_at < before))
        await self.session.commit()
        return (events.rowcount or 0) + (windows.rowcount or 0) + (ended.rowcount or 0)

    async def deployments(
        self,
        actor: User,
        *,
        project_id: int | None = None,
        active: bool | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[AwsDeployment], int]:
        """Deployments in the accounts of the projects `actor` can see, newest
        first."""
        filters = []
        if not is_manager(actor):
            filters.append(_visible_project_filter(actor, AwsAccount.project_id))
        if project_id is not None:
            filters.append(AwsAccount.project_id == project_id)
        if active is not None:
            filters.append(AwsDeployment.finished_at.is_(None) if active else AwsDeployment.finished_at.is_not(None))
        base = select(AwsDeployment).join(AwsAccount, AwsAccount.id == AwsDeployment.account_id).where(*filters)
        total = await self.session.scalar(select(func.count()).select_from(base.subquery()))
        rows = await self.session.scalars(
            base.order_by(AwsDeployment.started_at.desc(), AwsDeployment.id.desc()).limit(limit).offset(offset)
        )
        return list(rows), total or 0

    # -- discovery and import ------------------------------------------------

    async def _listing(self, vpc: AwsVpc, *, refresh: bool = False) -> tuple:
        """(discovered_at, listing, suggestions by key), from the cache when
        it is fresh enough."""
        cached = None if refresh else discovery.cached(vpc.id, settings.AWS_DISCOVERY_CACHE_SECONDS)
        if cached is None:
            listing = await discovery.list_vpc(client.Region.of(vpc), vpc.aws_vpc_id, count_targets=True)
            if not listing.listed_kinds:
                reasons = "; ".join(f"{s.call}: {s.reason}" for s in listing.skipped[:3])
                raise InfraAwsError(f"AWS listed nothing in {vpc.aws_vpc_id}: {reasons}")
            identity = await client.identity()
            groups = await discovery.security_groups_of(client.Region.of(vpc), listing.items)
            watchly_groups = {g for g, _ in identity.security_groups} if identity else set()
            suggestions = {
                info.key: discovery.suggest(
                    info,
                    security_groups=groups,
                    watchly_groups=watchly_groups,
                    watchly_ip=identity.private_ip if identity else None,
                )
                for info in listing.items
            }
            cached = (datetime.now(UTC), listing, suggestions)
            discovery.remember(vpc.id, cached)
        return cached

    async def discover(self, vpc: AwsVpc, actor: User, *, refresh: bool = False) -> DiscoveryResult:
        discovered_at, listing, suggestions = await self._listing(vpc, refresh=refresh)
        monitored = await self.session.execute(
            select(AwsResource.id, AwsResource.aws_id, AwsResource.project_id).where(AwsResource.vpc_id == vpc.id)
        )
        visible_projects = (
            None
            if is_manager(actor)
            else set(await self.session.scalars(visible_project_ids(actor.id)))
        )
        project_names = dict(
            (await self.session.execute(select(Project.id, Project.name))).all()
        )
        by_aws_id: dict[str, list[MonitoredBy]] = {}
        for resource_id, aws_id, project_id in monitored:
            visible = visible_projects is None or project_id in visible_projects
            by_aws_id.setdefault(aws_id, []).append(
                MonitoredBy(
                    resource_id=resource_id,
                    project=ProjectBrief(id=project_id, name=project_names.get(project_id, "")) if visible else None,
                )
            )
        return DiscoveryResult(
            discovered_at=discovered_at,
            items=[
                DiscoveredItem(
                    key=info.key,
                    kind=info.kind,
                    aws_id=info.aws_id,
                    aws_arn=info.aws_arn,
                    name=info.name,
                    detail=info.summary,
                    address=info.address,
                    public=info.public,
                    public_ip=info.public_ip,
                    instances=(
                        info.aws_detail.get("instances", [])
                        if info.kind is ResourceKind.AUTO_SCALING_GROUP
                        else []
                    ),
                    subnet=info.subnet,
                    aws_state=info.aws_state,
                    tags=info.tags,
                    auto_scaling_group=info.auto_scaling_group,
                    target_groups=info.target_groups,
                    monitored_by=by_aws_id.get(info.aws_id, []),
                    suggested=suggestions.get(info.key, []),
                )
                for info in listing.items
            ],
            skipped=listing.skipped,
        )

    async def topology(self, vpc: AwsVpc, actor: User, *, refresh: bool = False) -> Topology:
        """The VPC map: AWS's listing, laid over the resources `actor` can see.
        When AWS lists nothing, the map holds the monitored resources only."""
        listing, aws_error = None, None
        try:
            _, listing, _ = await self._listing(vpc, refresh=refresh)
        except (InfraAwsError, AwsError) as exc:
            aws_error = str(exc)
        resources, _ = await self.list_resources(actor, vpc_id=vpc.id, limit=1000, sort=ResourceSort.NAME)
        identity = await client.identity()
        return await topology.build(
            vpc,
            resources,
            listing,
            aws_error,
            identity,
            is_watchly_vpc(identity, vpc.account, vpc.aws_vpc_id),
            refresh=refresh,
        )

    async def import_resources(self, vpc: AwsVpc, payload: ImportRequest, actor: User) -> list[AwsResource]:
        """All or nothing: every item is validated first, then created in one
        transaction."""
        project = await self.project_for_write(payload.project_id, actor)
        assert_vpc_of(vpc, project)
        _, listing, suggestions = await self._listing(vpc)
        infos = {info.key: info for info in listing.items}
        monitored = set(
            await self.session.scalars(select(AwsResource.aws_id).where(AwsResource.vpc_id == vpc.id))
        )

        problems, conflicts, created = [], [], []
        for item in payload.items:
            info = infos.get(item.key)
            if info is None:
                problems.append(f"{item.key}: not found in {vpc.name}; discover again")
                continue
            if info.aws_id in monitored:
                conflicts.append(f"{item.key} ({info.name})")
                continue
            checks = item.checks
            if checks is None:
                checks = [
                    CheckCreate(check_type=s.check_type, name=s.name, settings=s.settings)
                    for s in suggestions.get(item.key, [])
                ]
            try:
                created.append(
                    await self._new_resource(
                        vpc, project, info, actor,
                        name=item.name, environment=payload.environment, checks=checks,
                    )
                )
            except (InfraInvalidError, InfraConflictError) as exc:
                problems.append(f"{item.key}: {exc}")
        if conflicts:
            await self.session.rollback()
            raise InfraConflictError(f"Already monitored: {', '.join(conflicts)}.")
        if problems:
            await self.session.rollback()
            raise InfraInvalidError("; ".join(problems))
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise InfraConflictError("One of these is already monitored; discover again.") from exc
        for resource in created:
            await self.reload(resource)
        return created

    # -- the monitoring loop's reads -----------------------------------------

    async def due_resources(self, now: datetime | None = None) -> list[tuple[AwsResource, list[AwsCheck]]]:
        """Resources with at least one check due, and those checks. Paused,
        missing and in-maintenance resources wait."""
        now = now or datetime.now(UTC)
        next_due = AwsCheck.last_checked_at + (
            literal_column("interval '1 second'") * AwsCheck.check_interval_seconds
        )
        due_ids = select(AwsCheck.id).join(AwsResource, AwsResource.id == AwsCheck.resource_id).where(
            AwsCheck.is_enabled.is_(True),
            AwsResource.is_enabled.is_(True),
            AwsResource.missing_since.is_(None),
            ~maintenance_in_effect(now),
            or_(AwsCheck.last_checked_at.is_(None), next_due <= now),
        )
        due = set(await self.session.scalars(due_ids))
        if not due:
            return []
        resource_ids = select(AwsCheck.resource_id).where(AwsCheck.id.in_(due))
        resources = await self.session.scalars(
            select(AwsResource).where(AwsResource.id.in_(resource_ids)).order_by(AwsResource.id)
        )
        return [(r, [c for c in r.checks if c.id in due]) for r in resources]

    async def vpcs_due_for_sync(self, now: datetime | None = None) -> list[AwsVpc]:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(seconds=settings.AWS_SYNC_INTERVAL_SECONDS)
        has_resources = select(AwsResource.vpc_id).distinct()
        rows = await self.session.scalars(
            select(AwsVpc).where(
                AwsVpc.id.in_(has_resources),
                or_(AwsVpc.synced_at.is_(None), AwsVpc.synced_at <= cutoff),
            )
        )
        return list(rows)

    async def sync_vpc(self, vpc: AwsVpc) -> list[ScalingChange]:
        """Read the VPC's resources from AWS again: addresses, states,
        details, and which are gone. Returns what changed in the Auto Scaling
        groups' membership; the first sync of a group only records it."""
        now = datetime.now(UTC)
        try:
            await self.refresh_vpc_cidrs(vpc)
        except AwsError:
            pass
        listing = await discovery.list_vpc(client.Region.of(vpc), vpc.aws_vpc_id)
        found = {(info.kind, info.aws_id): info for info in listing.items}
        resources = await self.session.scalars(select(AwsResource).where(AwsResource.vpc_id == vpc.id))
        changes: list[ScalingChange] = []
        for resource in resources:
            info = found.get((resource.kind, resource.aws_id))
            if info is not None and resource.kind is ResourceKind.AUTO_SCALING_GROUP:
                now_ids = discovery.in_service_ids(info.aws_detail)
                before = resource.scaling_members
                resource.scaling_members = now_ids
                if before is not None and set(before) != set(now_ids):
                    joined = set(now_ids) - set(before)
                    changes.append(
                        ScalingChange(
                            resource=resource,
                            added=tuple(
                                m for m in info.aws_detail.get("instances", []) if m.get("id") in joined
                            ),
                            removed=tuple(sorted(set(before) - set(now_ids))),
                        )
                    )
            if info is not None:
                resource.address = info.address
                resource.aws_state = info.aws_state
                resource.aws_detail = info.aws_detail
                resource.aws_arn = info.aws_arn or resource.aws_arn
                resource.synced_at = now
                resource.missing_since = None
            elif resource.kind in listing.listed_kinds and resource.missing_since is None:
                resource.missing_since = now
        vpc.synced_at = now
        await self.session.commit()
        return changes

    # -- overview ------------------------------------------------------------

    async def overview(self, actor: User, **scope) -> dict:
        state = resource_state_sql()
        filters = self._resource_filters(actor, **scope)
        counts = await self.summary(actor, **scope)
        kind_rows = await self.session.execute(
            select(
                AwsResource.kind,
                func.count(),
                func.count().filter(state == ResourceState.DOWN.value),
                func.count().filter(state == ResourceState.DEGRADED.value),
            )
            .where(*filters)
            .group_by(AwsResource.kind)
        )
        by_kind = [
            {"kind": kind, "total": total, "down": down, "degraded": degraded}
            for kind, total, down, degraded in kind_rows
        ]
        by_kind.sort(key=lambda row: list(ResourceKind).index(row["kind"]))

        views, _ = await self.list_vpcs(actor, limit=1000)
        if scope.get("vpc_id") is not None:
            views = [v for v in views if v.vpc.id == scope["vpc_id"]]

        rank = case(
            (state == ResourceState.DOWN.value, 0),
            (state == ResourceState.DEGRADED.value, 1),
            else_=2,
        )
        since = func.coalesce(AwsResource.down_since, AwsResource.degraded_since, AwsResource.missing_since)
        attention = await self.session.execute(
            select(AwsResource, state)
            .where(
                *filters,
                state.in_(
                    [ResourceState.DOWN.value, ResourceState.DEGRADED.value, ResourceState.MISSING.value]
                ),
            )
            .order_by(rank, since.asc().nulls_last())
            .limit(20)
        )
        return {
            "counts": counts,
            "views": views,
            "by_kind": by_kind,
            "attention": [(resource, ResourceState(value)) for resource, value in attention],
        }


def attention_summary(resource: AwsResource, state: ResourceState) -> tuple[datetime | None, str]:
    """When it began, and one line on what is wrong."""
    if state is ResourceState.MISSING:
        return resource.missing_since, "AWS no longer lists it; its checks are stopped."
    if state is ResourceState.DOWN:
        lines = [
            f"{check.name}: {(check.last_result or {}).get('summary', 'failed')}"
            for check in resource.checks
            if check.is_enabled and check.health is CheckHealth.DOWN
        ]
        return resource.down_since, "; ".join(lines) or "Down."
    problems = resource.problems
    return resource.degraded_since, "; ".join(p["detail"] or p["kind"] for p in problems) or "Degraded."


def vpc_cidrs(aws_vpc: dict) -> list[str]:
    """The VPC's associated IPv4 ranges. IPv6 ranges are global addresses,
    which infrastructure probes do not reach."""
    cidrs = [
        assoc["CidrBlock"]
        for assoc in aws_vpc.get("CidrBlockAssociationSet", [])
        if (assoc.get("CidrBlockState") or {}).get("State", "associated") == "associated"
        and assoc.get("CidrBlock")
    ]
    if not cidrs and aws_vpc.get("CidrBlock"):
        cidrs = [aws_vpc["CidrBlock"]]
    return list(dict.fromkeys(cidrs))


__all__ = [
    "InfraAwsError",
    "InfraConflictError",
    "InfraError",
    "InfraForbiddenError",
    "InfraInvalidError",
    "InfraNotFoundError",
    "InfraService",
    "VpcView",
    "attention_summary",
    "is_manager",
]
