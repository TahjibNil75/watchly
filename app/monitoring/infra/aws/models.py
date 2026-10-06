"""AWS resources and their checks, apart from websites.

An infrastructure project has one or more AWS accounts, each holding the
credentials Watchly reaches it with. A VPC is registered by its id, in one
account, when a resource is first added from it. A resource (an EC2
instance in a public or private subnet, an Application or Network Load
Balancer, internal or internet-facing, an EC2 Auto Scaling group, or an RDS
database) belongs to one project and one VPC; Watchly reads its address and
state from AWS. A resource has 1-10 checks,
each with its own interval, and the resource, not the check, is what goes down
and alerts: see `monitor.py`.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    Exists,
    Float,
    ForeignKey,
    Index,
    Integer,
    Select,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    case,
    desc,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.crypto import decrypt_secret, mask_secret
from app.db.base import Base, TimestampMixin
from app.db.models.user import User
from app.monitoring.infra.aws.credentials import AccountAuth
from app.monitoring.projects.models import Project
from app.monitoring.websites.models import WebsiteEnvironment, website_environment_enum


def _enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=False,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class ResourceKind(str, enum.Enum):
    """What a resource is in AWS. Fixed once it is added."""

    #: An EC2 instance, in a public or a private subnet; its address is its
    #: private IP, and a check may go to its public IP instead.
    SERVER = "server"
    #: An Application or Network Load Balancer, internal or internet-facing;
    #: its address is its DNS name.
    LOAD_BALANCER = "load_balancer"
    #: An EC2 Auto Scaling group, by name. It has no address of its own: its
    #: `ping`, `tcp` and `http` checks run against every instance in service,
    #: read from AWS at each run, since the group replaces them.
    AUTO_SCALING_GROUP = "auto_scaling_group"
    #: An RDS DB instance, Aurora's included, by its identifier; its address
    #: is its endpoint. Watched without logging in: its port, and what RDS and
    #: CloudWatch say of it.
    DATABASE = "database"


class InfraCheckType(str, enum.Enum):
    """How a check probes its resource. Fixed once it is created."""

    PING = "ping"
    TCP = "tcp"
    HTTP = "http"
    #: Read the load balancer's own health-check verdict for one target group.
    TARGET_HEALTH = "target_health"
    #: Read an Auto Scaling group's own view: instances in service and
    #: healthy, against its desired capacity.
    GROUP_HEALTH = "group_health"
    #: Read a database's status from RDS: available, stopped, storage full...
    DB_STATUS = "db_status"
    #: Read a database's CloudWatch metrics (CPU, free storage and memory,
    #: connections, replica lag, IOPS, burst balance, and where its storage
    #: is heading) against thresholds.
    DB_METRICS = "db_metrics"
    #: Read a server's CloudWatch metrics (CPU, a burstable instance's CPU
    #: credits, its EBS volumes' burst balance and IOPS, and its disks'
    #: use, when the CloudWatch agent reports it) against thresholds.
    EC2_METRICS = "ec2_metrics"


#: Which checks each kind of resource takes.
CHECKS_BY_KIND: dict[ResourceKind, frozenset[InfraCheckType]] = {
    ResourceKind.SERVER: frozenset(
        {InfraCheckType.PING, InfraCheckType.TCP, InfraCheckType.HTTP, InfraCheckType.EC2_METRICS}
    ),
    ResourceKind.LOAD_BALANCER: frozenset(
        {InfraCheckType.HTTP, InfraCheckType.TCP, InfraCheckType.TARGET_HEALTH}
    ),
    ResourceKind.AUTO_SCALING_GROUP: frozenset(
        {
            InfraCheckType.HTTP,
            InfraCheckType.TCP,
            InfraCheckType.PING,
            InfraCheckType.GROUP_HEALTH,
            InfraCheckType.TARGET_HEALTH,
        }
    ),
    ResourceKind.DATABASE: frozenset(
        {InfraCheckType.DB_STATUS, InfraCheckType.TCP, InfraCheckType.DB_METRICS}
    ),
}

#: Checks that open a connection, and so (when they go into the VPC) tell
#: whether Watchly can reach it at all. `target_health`, `group_health`,
#: `db_status`, `db_metrics` and `ec2_metrics` ask the AWS API instead.
NETWORK_CHECKS = frozenset({InfraCheckType.PING, InfraCheckType.TCP, InfraCheckType.HTTP})


def is_public_path(kind: ResourceKind, aws_detail: dict | None, settings: dict | None) -> bool:
    """Whether a check reaches its resource over the internet rather than
    inside the VPC: an internet-facing load balancer, or a server's (or an
    Auto Scaling group's) check sent to the instances' public IPs. A
    database is always checked inside the VPC."""
    if kind is ResourceKind.DATABASE:
        return False
    if kind is ResourceKind.LOAD_BALANCER:
        return (aws_detail or {}).get("scheme") == "internet-facing"
    return bool((settings or {}).get("use_public_ip"))


class ResourceStatus(str, enum.Enum):
    """The outage state machine, as for websites."""

    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"


class CheckHealth(str, enum.Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    #: Passed, but a problem has been open for INFRA_PROBLEM_CHECKS checks.
    DEGRADED = "degraded"
    #: Failed, after its retries.
    DOWN = "down"


class ResourceState(str, enum.Enum):
    """Exactly one per resource, so counts add up. Derived, not stored: see
    `AwsResource.state` and `resource_state_sql`."""

    PAUSED = "paused"
    MAINTENANCE = "maintenance"
    MISSING = "missing"
    DOWN = "down"
    DEGRADED = "degraded"
    HEALTHY = "healthy"
    UNKNOWN = "unknown"


account_auth_enum = _enum(AccountAuth, "aws_account_auth")
resource_kind_enum = _enum(ResourceKind, "aws_resource_kind")
infra_check_type_enum = _enum(InfraCheckType, "aws_check_type")
resource_status_enum = _enum(ResourceStatus, "aws_resource_status")
check_health_enum = _enum(CheckHealth, "aws_check_health")


#: Users alerted about one resource, on top of its project's recipients.
aws_resource_recipients = Table(
    "aws_resource_recipients",
    Base.metadata,
    Column("resource_id", ForeignKey("aws_resources.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("added_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


def recipient_resource_ids(user_id: int) -> Select:
    """Subquery of the resource ids `user_id` is an extra recipient of; like a
    site recipient, they can see that resource without its project."""
    return select(aws_resource_recipients.c.resource_id).where(
        aws_resource_recipients.c.user_id == user_id
    )


class AwsAccount(Base, TimestampMixin):
    """An AWS account of one infrastructure project, and how Watchly reaches
    it. Every call about a VPC's resources is made with its account's
    credentials; see `client.Account`."""

    __tablename__ = "aws_accounts"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_aws_accounts_project_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_type: Mapped[AccountAuth] = mapped_column(account_auth_enum, nullable=False)
    #: With `access_key` only.
    access_key_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Encrypted at rest (`app.core.crypto`). Never returned by the API.
    secret_access_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: A role in the account to assume with the credentials above.
    role_arn: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(1224), nullable=True)
    #: Where its VPCs are listed by default; blank, Watchly's own region.
    default_region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: What its resources are when they are added without one.
    environment: Mapped[WebsiteEnvironment | None] = mapped_column(
        website_environment_enum, nullable=True
    )
    #: The 12-digit account id, as STS last said.
    aws_account_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    #: The last time its credentials were tried, and what went wrong if they
    #: did not work.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Ask CodeDeploy what is being deployed: see `deployments.py`. Off
    #: unless asked, as it needs codedeploy:* read permissions.
    watch_deployments: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: The last time CodeDeploy was asked, and what went wrong if it was not
    #: answered.
    deployments_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deployments_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Look at what the account holds rather than what runs in it: Elastic
    #: IPs attached to nothing, and how close it is to its Elastic IP quota.
    #: See `capacity.py`. Off unless asked, as it needs ec2:DescribeAddresses
    #: and servicequotas:GetServiceQuota.
    watch_capacity: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    capacity_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    capacity_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: What the last look found, per region (`{"regions": {name: {...}}}`),
    #: and its open problems, as a check keeps them (`{"problems": {key:
    #: {streak, since, detail, last_alert_at, alerted}}}`).
    capacity: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Joined, like a VPC's account: who may see and change it.
    project: Mapped[Project] = relationship(Project, lazy="joined", innerjoin=True)

    @property
    def secret_access_key_hint(self) -> str | None:
        return mask_secret(decrypt_secret(self.secret_access_key))

    def __repr__(self) -> str:
        return f"<AwsAccount {self.name!r} {self.aws_account_id}>"


class AwsVpc(Base, TimestampMixin):
    """A VPC Watchly may probe. Its CIDRs come from AWS, not from a person:
    infrastructure probes may reach those ranges, and public addresses only
    for a resource that is public (see `is_public_path`)."""

    __tablename__ = "aws_vpcs"
    __table_args__ = (
        UniqueConstraint("account_id", "region", "aws_vpc_id", name="uq_aws_vpcs_vpc"),
        UniqueConstraint("account_id", "name", name="uq_aws_vpcs_account_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Whose credentials every call about it uses, and so whose project it
    #: is. The service refuses to remove an account that still holds VPCs;
    #: the cascade is for deleting the whole project.
    account_id: Mapped[int] = mapped_column(
        ForeignKey("aws_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    aws_vpc_id: Mapped[str] = mapped_column(String(32), nullable=False)
    #: As AWS last listed them.
    cidrs: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    #: Whether Watchly runs in this VPC; any other is reached through peering
    #: or a transit gateway.
    is_watchly_vpc: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Set while most of its checks fail to connect: see `monitor.py`.
    unreachable_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When its resources were last read from AWS.
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: `{"ok": ..., "steps": [...]}` of the last `POST /test`.
    last_test: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Joined: every AWS call about the VPC needs it (`client.Region.of`),
    #: and its project decides who may see the VPC. Joined, not lazy: read
    #: from the async monitoring loop, where a lazy load would raise
    #: MissingGreenlet.
    account: Mapped[AwsAccount] = relationship(AwsAccount, lazy="joined", innerjoin=True)

    @property
    def project(self) -> Project:
        return self.account.project

    def __repr__(self) -> str:
        return f"<AwsVpc {self.name!r} {self.aws_vpc_id} {self.cidrs}>"


class AwsResource(Base, TimestampMixin):
    """An AWS resource, and the live state of its current outage."""

    __tablename__ = "aws_resources"
    __table_args__ = (UniqueConstraint("vpc_id", "aws_id", name="uq_aws_resources_aws_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Cascades only when the whole project goes; the service refuses to
    #: remove a VPC that still holds resources.
    vpc_id: Mapped[int] = mapped_column(
        ForeignKey("aws_vpcs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[ResourceKind] = mapped_column(resource_kind_enum, nullable=False)
    #: Instance id, load balancer ARN, Auto Scaling group name, or DB instance
    #: identifier.
    aws_id: Mapped[str] = mapped_column(String(512), nullable=False)
    aws_arn: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    environment: Mapped[WebsiteEnvironment | None] = mapped_column(
        website_environment_enum, nullable=True
    )

    # --- read from AWS by the sync ------------------------------------------
    #: Private IP, or DNS name (a load balancer's, a database's endpoint);
    #: none for an Auto Scaling group.
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: `running`, `stopped`, `active`, `3 of 3 in service`, ...
    aws_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Instance type, public IP, scheme and listeners, capacity and member
    #: instances, AZ, subnet, security groups, tags.
    aws_detail: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Since when AWS no longer has it; its checks stop meanwhile.
    missing_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    max_down_alerts: Mapped[int] = mapped_column(
        Integer, default=4, server_default=text("4"), nullable=False
    )
    #: An Auto Scaling group's opt-in notifications when instances join or
    #: leave it; off unless the user asks.
    notify_scale_out: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    notify_scale_in: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: Optional endpoint (`schemas.ScaleHealthCheck`) probed on each new
    #: instance; the scale-out notification says whether it answered.
    scale_health_check: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: Ids of the instances in service at the last sync; null before the first.
    scaling_members: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # --- live state -----------------------------------------------------------
    status: Mapped[ResourceStatus] = mapped_column(
        resource_status_enum,
        default=ResourceStatus.UNKNOWN,
        server_default=text("'unknown'::aws_resource_status"),
        nullable=False,
        index=True,
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    down_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The earliest open problem of any check, while the resource is up.
    degraded_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    down_alerts_sent: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: The down alert was held because its VPC was unreachable; it goes out if
    #: the resource is still down when the VPC answers again.
    down_alert_held: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    slack_thread_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    slack_thread_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    telegram_thread_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_thread_chat: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    project: Mapped[Project] = relationship(lazy="selectin")
    vpc: Mapped[AwsVpc] = relationship(lazy="selectin")
    recipients: Mapped[list[User]] = relationship(
        "User", secondary=aws_resource_recipients, lazy="selectin", order_by=User.id
    )
    checks: Mapped[list["AwsCheck"]] = relationship(
        back_populates="resource",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="AwsCheck.id",
    )
    #: Windows in effect or still to come, soonest first; as for websites.
    maintenance_windows: Mapped[list["AwsMaintenanceWindow"]] = relationship(
        "AwsMaintenanceWindow",
        primaryjoin=lambda: and_(
            AwsResource.id == AwsMaintenanceWindow.resource_id,
            AwsMaintenanceWindow.ends_at > func.now(),
        ),
        lazy="selectin",
        viewonly=True,
        order_by=lambda: AwsMaintenanceWindow.starts_at,
    )

    def maintenance_at(self, moment: datetime) -> "AwsMaintenanceWindow | None":
        return next(
            (w for w in self.maintenance_windows if w.starts_at <= moment < w.ends_at), None
        )

    @property
    def maintenance(self) -> "AwsMaintenanceWindow | None":
        return self.maintenance_at(datetime.now(UTC))

    @property
    def upcoming_maintenance(self) -> list["AwsMaintenanceWindow"]:
        now = datetime.now(UTC)
        return [w for w in self.maintenance_windows if w.starts_at > now]

    @property
    def state(self) -> ResourceState:
        """The in-memory twin of `resource_state_sql`."""
        if not self.is_enabled:
            return ResourceState.PAUSED
        if self.maintenance is not None:
            return ResourceState.MAINTENANCE
        if self.missing_since is not None:
            return ResourceState.MISSING
        if self.status is ResourceStatus.DOWN:
            return ResourceState.DOWN
        if self.status is ResourceStatus.UP:
            return ResourceState.DEGRADED if self.degraded_since else ResourceState.HEALTHY
        return ResourceState.UNKNOWN

    @property
    def problems(self) -> list[dict]:
        """Every open problem of its enabled checks, oldest first."""
        rows = []
        for check in self.checks:
            if not check.is_enabled:
                continue
            for kind, entry in (check.problems or {}).items():
                if entry.get("since"):
                    rows.append(
                        {
                            "kind": kind,
                            "check_id": check.id,
                            "since": entry["since"],
                            "detail": entry.get("detail"),
                        }
                    )
        return sorted(rows, key=lambda row: row["since"])

    @property
    def extra_recipients(self) -> list[User]:
        return self.recipients

    @property
    def alert_recipients(self) -> list[str]:
        """Who is emailed: the project's recipients, then the resource's own."""
        own = [user.email for user in self.recipients if user.is_active]
        return [*self.project.recipient_emails, *own]

    def __repr__(self) -> str:
        return f"<AwsResource {self.kind.value} {self.name!r} status={self.status.value!r}>"


class AwsCheck(Base, TimestampMixin):
    """One way of probing a resource, with its own interval and health."""

    __tablename__ = "aws_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("aws_resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    check_type: Mapped[InfraCheckType] = mapped_column(infra_check_type_enum, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The type's own settings, as `schemas.SETTINGS_MODELS` validates them.
    settings: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    check_interval_seconds: Mapped[int] = mapped_column(
        Integer, default=300, server_default=text("300"), nullable=False
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, default=10, server_default=text("10"), nullable=False
    )
    retries_on_failure: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    # --- live state -----------------------------------------------------------
    health: Mapped[CheckHealth] = mapped_column(
        check_health_enum,
        default=CheckHealth.UNKNOWN,
        server_default=text("'unknown'::aws_check_health"),
        nullable=False,
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    #: `{ok, response_time_ms, summary, error, error_type, connect_failure}`.
    last_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: The latest figures (round trip, status code, healthy targets...).
    snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: Per problem kind: `{streak, since, detail, last_alert_at}`. Open while
    #: `since` is set.
    problems: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    resource: Mapped[AwsResource] = relationship(back_populates="checks")

    def __repr__(self) -> str:
        return f"<AwsCheck {self.check_type.value} {self.name!r} {self.health.value}>"


class AwsCheckResult(Base):
    """One run of a check: the evidence behind every alert."""

    __tablename__ = "aws_check_results"
    __table_args__ = (Index("ix_aws_check_results_check_time", "check_id", desc("checked_at")),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    check_id: Mapped[int] = mapped_column(
        ForeignKey("aws_checks.id", ondelete="CASCADE"), nullable=False
    )
    #: Kept so a resource's results read without a join.
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("aws_resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dns_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connect_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tls_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The type's own figure, rolled up hourly: packet loss, healthy targets
    #: or instances.
    metric: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: The ping / tcp / http / targets group.
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<AwsCheckResult check={self.check_id} {'ok' if self.ok else 'failed'}>"


class AwsCheckHourly(Base):
    """One check's results in one UTC hour, summed up; kept after the raw
    results are purged."""

    __tablename__ = "aws_check_hourly"

    check_id: Mapped[int] = mapped_column(
        ForeignKey("aws_checks.id", ondelete="CASCADE"), primary_key=True
    )
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    checks: Mapped[int] = mapped_column(Integer, nullable=False)
    up_checks: Mapped[int] = mapped_column(Integer, nullable=False)
    timed_checks: Mapped[int] = mapped_column(Integer, nullable=False)
    sum_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metric_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    metric_max: Mapped[float | None] = mapped_column(Float, nullable=True)


class AwsEvent(Base):
    """The infrastructure feed: outages, recoveries, problems, VPCs lost and
    found, deployments. `project_id` scopes who sees it; a VPC event has one
    per project."""

    __tablename__ = "aws_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: None for a deployment, which is about a whole AWS account.
    vpc_id: Mapped[int | None] = mapped_column(
        ForeignKey("aws_vpcs.id", ondelete="CASCADE"), nullable=True
    )
    #: None for a VPC event.
    resource_id: Mapped[int | None] = mapped_column(
        ForeignKey("aws_resources.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    downtime_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    resource: Mapped[AwsResource | None] = relationship()
    vpc: Mapped[AwsVpc | None] = relationship()


class AwsDeployment(Base):
    """A CodeDeploy deployment in an account that watches them, as Watchly
    saw it start and end. While it runs, the resources it deploys to are in
    maintenance (`AwsMaintenanceWindow.deployment_id`): see `deployments.py`."""

    __tablename__ = "aws_deployments"
    __table_args__ = (
        UniqueConstraint("account_id", "region", "deployment_id", name="uq_aws_deployments_deployment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("aws_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    #: CodeDeploy's id, `d-XXXXXXXXX`.
    deployment_id: Mapped[str] = mapped_column(String(32), nullable=False)
    application_name: Mapped[str] = mapped_column(String(255), nullable=False)
    group_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: CodeDeploy's: `InProgress`, `Succeeded`, `Failed`, `Stopped`...
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Where it deploys to: its resources' environment, else its account's.
    environment: Mapped[WebsiteEnvironment | None] = mapped_column(
        website_environment_enum, nullable=True
    )
    #: Who started it, as CodeDeploy says: `user`, `CloudFormation`...
    creator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: What is deployed: `github owner/repo@abc1234`, `s3://bucket/key`...
    revision: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: The monitored resources it silences, as `[{id, name, kind}]`, kept
    #: past the windows and the resources themselves.
    resources: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    #: Why every resource of its region was silenced instead of its own,
    #: e.g. the deployment group could not be read.
    fallback: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: When CodeDeploy created it, and when it ended there.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The start message, which the end one replies to.
    slack_thread_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    slack_thread_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    telegram_thread_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_thread_chat: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: When Watchly first saw it; the silence is capped from here.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped[AwsAccount] = relationship(lazy="joined", innerjoin=True)

    @property
    def project(self) -> Project:
        return self.account.project

    @property
    def is_active(self) -> bool:
        return self.finished_at is None

    def __repr__(self) -> str:
        return f"<AwsDeployment {self.deployment_id} {self.application_name}/{self.group_name} {self.status}>"


class AwsMaintenanceWindow(Base):
    """As `MaintenanceWindow`, for a resource: no checks and no alerts."""

    __tablename__ = "aws_maintenance_windows"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_aws_maintenance_windows_order"),
        Index("ix_aws_maintenance_windows_resource_end", "resource_id", "ends_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("aws_resources.id", ondelete="CASCADE"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Set when a CodeDeploy deployment opened it rather than a person; it
    #: is renewed while the deployment runs and ended with it.
    deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("aws_deployments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def maintenance_in_effect(at) -> Exists:
    """SQL: the resource has a window covering `at`."""
    return (
        select(AwsMaintenanceWindow.id)
        .where(
            AwsMaintenanceWindow.resource_id == AwsResource.id,
            AwsMaintenanceWindow.starts_at <= at,
            AwsMaintenanceWindow.ends_at > at,
        )
        .exists()
    )


def resource_state_sql(at=None):
    """SQL twin of `AwsResource.state`, for filtering and counting."""
    at = at if at is not None else func.now()
    return case(
        (AwsResource.is_enabled.is_(False), ResourceState.PAUSED.value),
        (maintenance_in_effect(at), ResourceState.MAINTENANCE.value),
        (AwsResource.missing_since.is_not(None), ResourceState.MISSING.value),
        (AwsResource.status == ResourceStatus.DOWN, ResourceState.DOWN.value),
        (
            and_(AwsResource.status == ResourceStatus.UP, AwsResource.degraded_since.is_not(None)),
            ResourceState.DEGRADED.value,
        ),
        (AwsResource.status == ResourceStatus.UP, ResourceState.HEALTHY.value),
        else_=ResourceState.UNKNOWN.value,
    )
