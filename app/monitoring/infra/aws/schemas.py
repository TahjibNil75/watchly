"""Request and response shapes of /monitoring/infra/aws."""

import enum
import re
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.monitoring.infra.aws.edge_security import grade as grade_edge_security
from app.monitoring.infra.aws.credentials import AccountAuth, AccountFields, AccountInput
from app.monitoring.infra.aws.models import (
    CheckHealth,
    InfraCheckType,
    ResourceKind,
    ResourceState,
)
from app.monitoring.projects.schemas import ProjectMemberRead
from app.monitoring.websites.models import WebsiteEnvironment
from app.monitoring.websites.schemas import MaintenanceWindowRead, StatsRange

#: What a ping waits for each reply, by default; every other check, 10 s.
PING_TIMEOUT_SECONDS = 2
_VPC_ID = re.compile(r"^vpc-[A-Za-z0-9-]{1,64}$")


# --- check settings, one model per type ---------------------------------------


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PathSettings(_Settings):
    """What every connecting check shares: which address it goes to and,
    for an Auto Scaling group, how many of its instances must pass."""

    use_public_ip: bool = Field(
        default=False,
        description=(
            "A server's or Auto Scaling group's check only: go to the instances' public "
            "IPs, over the internet, instead of their private IPs inside the VPC."
        ),
    )
    min_healthy_instances: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description=(
            "An Auto Scaling group's check only: it runs on every instance in service, "
            "and is down when fewer than this many pass (null: when none does)."
        ),
    )


class PingSettings(_PathSettings):
    count: int = Field(default=3, ge=1, le=20, description="Echo requests per check.")
    packet_loss_threshold_percent: int | None = Field(
        default=20,
        ge=1,
        le=100,
        description="Losing at least this share while the host answers is a problem; null turns it off.",
    )


class TcpSettings(_PathSettings):
    port: int = Field(ge=1, le=65535, description="For a load balancer, one of its listeners' ports.")
    tls: bool = Field(default=False, description="Also complete a TLS handshake.")
    expect_banner: str | None = Field(
        default=None, max_length=255, description="Text the server's first bytes must contain."
    )


class HttpSettings(_PathSettings):
    scheme: Literal["http", "https"] = "http"
    port: int | None = Field(default=None, ge=1, le=65535, description="80 or 443 by default.")
    path: str = Field(default="/health", max_length=1024)
    host_header: str | None = Field(
        default=None, max_length=255, description="For load balancer rules that route by host."
    )
    expected_status: int = Field(default=200, ge=100, le=599)
    must_contain: str | None = Field(default=None, max_length=255)
    slow_threshold_ms: int | None = Field(
        default=3000, ge=1, description="Slower than this is a problem; null turns it off."
    )
    verify_tls: bool = Field(
        default=False,
        description=(
            "Verify the certificate. Off by default: a load balancer's certificate "
            "names your domain, not the DNS name or IP address the check connects to."
        ),
    )

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with /")
        return value


class TargetHealthSettings(_Settings):
    target_group_arn: str = Field(min_length=20, max_length=2048)
    min_healthy_targets: int = Field(default=1, ge=0, le=1000)

    @field_validator("target_group_arn")
    @classmethod
    def _arn(cls, value: str) -> str:
        if not value.startswith("arn:") or ":targetgroup/" not in value:
            raise ValueError("target_group_arn must be a target group ARN")
        return value


class GroupHealthSettings(_Settings):
    min_healthy_instances: int = Field(
        default=1,
        ge=0,
        le=1000,
        description=(
            "Down when fewer instances are in service and healthy, as the group sees "
            "them (never more than its desired capacity)."
        ),
    )


class DbStatusSettings(_Settings):
    """Nothing to set: down unless RDS says the database is available, or
    busy with something it serves through (a backup, a modification)."""


class DbMetricsSettings(_Settings):
    """Thresholds on the database's CloudWatch metrics, each a problem while
    it is crossed; null turns one off. Read with `GetMetricData`, which AWS
    bills per metric read: 5 to 10 per run, and one more an hour for the
    storage forecast."""

    cpu_percent_max: float | None = Field(
        default=90, gt=0, le=100, description="CPU above this share is the `high_cpu` problem."
    )
    free_storage_percent_min: float | None = Field(
        default=10,
        gt=0,
        lt=100,
        description=(
            "Free storage below this share of what it may use is `low_storage`: its allocated "
            "storage, or with storage autoscaling its maximum. Not for Aurora, whose storage "
            "grows by itself."
        ),
    )
    storage_full_days_min: int | None = Field(
        default=14,
        ge=1,
        le=365,
        description=(
            "Free storage running out within this many days, at the rate it shrank over the last "
            "7 days (since storage was last added), is `storage_filling`. Counts storage "
            "autoscaling in. Not for Aurora."
        ),
    )
    freeable_memory_mb_min: int | None = Field(
        default=None, ge=1, description="Freeable memory below this is `low_memory`."
    )
    connections_max: int | None = Field(
        default=None, ge=1, description="More open connections than this is `many_connections`."
    )
    connections_percent_max: float | None = Field(
        default=80,
        gt=0,
        le=100,
        description=(
            "Open connections above this share of max_connections is `connections_near_limit`. "
            "max_connections is read from its parameter group (a formula there is an estimate), "
            "or given below."
        ),
    )
    max_connections: int | None = Field(
        default=None,
        ge=1,
        description="Its max_connections, when the parameter group cannot tell it or tells it wrong.",
    )
    iops_percent_max: float | None = Field(
        default=90,
        gt=0,
        le=100,
        description="Read and write IOPS above this share of its provisioned IOPS (io1, io2, gp3) is `iops_saturated`.",
    )
    burst_balance_percent_min: float | None = Field(
        default=20,
        gt=0,
        lt=100,
        description="A gp2 database's burst balance below this is `low_burst_balance`.",
    )
    cpu_credits_percent_min: float | None = Field(
        default=10,
        gt=0,
        lt=100,
        description=(
            "A burstable (db.t*) class's CPU credits below this share of what it can bank, or "
            "spending surplus credits, is `low_cpu_credits`."
        ),
    )
    replica_lag_seconds_max: int | None = Field(
        default=60,
        ge=1,
        description="A read replica's lag above this is `replica_lag`; ignored on a database that is no replica.",
    )


class Ec2MetricsSettings(_Settings):
    """Thresholds on the server's CloudWatch metrics, each a problem while it
    is crossed; null turns one off. Read with `GetMetricData`, which AWS bills
    per metric read: 1 to 3 for the instance, 2 for its EBS burst, 1 or 2 per
    volume and 1 per disk, a run."""

    cpu_percent_max: float | None = Field(
        default=90, gt=0, le=100, description="CPU above this share is the `high_cpu` problem."
    )
    cpu_credits_percent_min: float | None = Field(
        default=10,
        gt=0,
        lt=100,
        description=(
            "A burstable (T) instance's CPU credits below this share of what it can bank, or "
            "spending surplus credits in unlimited mode, is `low_cpu_credits`."
        ),
    )
    burst_balance_percent_min: float | None = Field(
        default=20,
        gt=0,
        lt=100,
        description=(
            "A gp2, st1 or sc1 volume's burst balance, or the instance's own EBS burst balance, "
            "below this is `low_burst_balance`."
        ),
    )
    iops_percent_max: float | None = Field(
        default=90,
        gt=0,
        le=100,
        description="An io1, io2 or gp3 volume's IOPS above this share of its provisioned IOPS is `iops_saturated`.",
    )
    disk_used_percent_max: float | None = Field(
        default=90,
        gt=0,
        le=100,
        description=(
            "A filesystem fuller than this is `low_disk_space`. Needs the CloudWatch agent on the "
            "instance, publishing disk_used_percent with its InstanceId."
        ),
    )


class ScaleHealthCheck(_Settings):
    """The endpoint probed on an Auto Scaling group's new instances."""

    scheme: Literal["http", "https"] = "http"
    port: int | None = Field(default=None, ge=1, le=65535, description="80 or 443 by default.")
    path: str = Field(default="/health", max_length=1024)
    expected_status: int = Field(default=200, ge=100, le=599)
    use_public_ip: bool = Field(default=False, description="Go to the instance's public IP instead of its private one.")
    verify_tls: bool = False

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with /")
        return value


SETTINGS_MODELS: dict[InfraCheckType, type[_Settings]] = {
    InfraCheckType.PING: PingSettings,
    InfraCheckType.TCP: TcpSettings,
    InfraCheckType.HTTP: HttpSettings,
    InfraCheckType.TARGET_HEALTH: TargetHealthSettings,
    InfraCheckType.GROUP_HEALTH: GroupHealthSettings,
    InfraCheckType.DB_STATUS: DbStatusSettings,
    InfraCheckType.DB_METRICS: DbMetricsSettings,
    InfraCheckType.EC2_METRICS: Ec2MetricsSettings,
}


def validate_settings(check_type: InfraCheckType, raw: dict | None) -> dict:
    """`raw` checked against the type's model, defaults filled in."""
    return SETTINGS_MODELS[check_type].model_validate(raw or {}).model_dump()


def default_timeout(check_type: InfraCheckType) -> int:
    return PING_TIMEOUT_SECONDS if check_type is InfraCheckType.PING else 10


# --- small shared shapes ------------------------------------------------------


class ProjectBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    owner_id: int | None = Field(default=None, description="Decides, with the role, who may manage it.")


class VpcBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class StateCounts(BaseModel):
    """Resources per state; they add up to `total`."""

    total: int = 0
    healthy: int = 0
    degraded: int = 0
    down: int = 0
    unknown: int = 0
    maintenance: int = 0
    paused: int = 0
    missing: int = 0


# --- AWS accounts -------------------------------------------------------------


class AccountCreate(AccountInput):
    project_id: int = Field(ge=1, description="The infrastructure project the account belongs to.")


class AccountUpdate(AccountFields):
    """Only what is sent changes. A new access key is sent as a pair; the
    secret alone may be sent again to replace it."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    auth_type: AccountAuth | None = None


class UnattachedAddress(BaseModel):
    public_ip: str
    allocation_id: str | None
    name: str | None = Field(description="Its Name tag.")


class CapacityRegion(BaseModel):
    """One region of an account, as the last capacity look found it."""

    region: str
    elastic_ips: int = Field(description="Elastic IPs it holds there.")
    elastic_ip_quota: int | None = Field(description="How many it may hold there, from Service Quotas.")
    quota_source: str | None = Field(
        description=(
            "`applied` (its own), `default` (AWS's default for every account) or `assumed` "
            "(5, when neither could be read)."
        )
    )
    unattached: list[UnattachedAddress] = Field(description="Its Elastic IPs attached to nothing, which AWS bills.")


class AccountRead(BaseModel):
    id: int
    project: ProjectBrief
    name: str
    description: str | None
    auth_type: AccountAuth
    access_key_id: str | None
    secret_access_key_hint: str | None = Field(description="`…WXYZ`; the secret itself is never returned.")
    role_arn: str | None
    external_id: str | None
    default_region: str | None
    environment: WebsiteEnvironment | None
    aws_account_id: str | None = Field(description="As STS last said.")
    verified_at: datetime | None
    last_error: str | None
    watch_deployments: bool = Field(description="Whether its CodeDeploy deployments are announced and silence their resources.")
    deployments_checked_at: datetime | None = Field(description="When CodeDeploy was last asked.")
    deployments_error: str | None = Field(description="Why CodeDeploy could not be asked, per region.")
    watch_capacity: bool = Field(
        description="Whether its Elastic IPs left attached to nothing, and its Elastic IP quota, are watched."
    )
    capacity_checked_at: datetime | None = Field(description="When its capacity was last looked at.")
    capacity_error: str | None = Field(description="Why it could not be looked at, per region.")
    capacity: list[CapacityRegion] = Field(description="What the last look found, per region.")
    vpc_count: int
    created_by_id: int | None
    created_at: datetime


class AccountList(BaseModel):
    items: list[AccountRead]
    total: int


class AccountBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    aws_account_id: str | None


# --- VPCs ---------------------------------------------------------------------


class VpcCreate(BaseModel):
    account_id: int = Field(description="The AWS account the VPC is in; its credentials are used for it.")
    aws_vpc_id: str = Field(examples=["vpc-0a1b2c3d4e5f60718"])
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    region: str | None = Field(default=None, max_length=32, description="The account's default region by default.")

    @field_validator("aws_vpc_id")
    @classmethod
    def _vpc_id(cls, value: str) -> str:
        value = value.strip()
        if not _VPC_ID.match(value):
            raise ValueError("aws_vpc_id must look like vpc-0a1b2c3d4e5f60718")
        return value


class VpcUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class TestStep(BaseModel):
    step: str
    ok: bool
    time_ms: int | None = None
    detail: str


class VpcTestResult(BaseModel):
    ok: bool
    tested_at: datetime
    steps: list[TestStep]


class VpcRead(BaseModel):
    id: int
    name: str
    description: str | None
    account: AccountBrief
    region: str
    aws_vpc_id: str
    cidrs: list[str]
    is_watchly_vpc: bool
    project: ProjectBrief = Field(description="The project whose AWS account it is in.")
    health: Literal["reachable", "unreachable", "untested"]
    unreachable_since: datetime | None
    synced_at: datetime | None
    last_tested_at: datetime | None
    last_test: VpcTestResult | None
    counts: StateCounts
    created_by_id: int | None
    created_at: datetime


class VpcList(BaseModel):
    items: list[VpcRead]
    total: int


class AvailableVpc(BaseModel):
    aws_vpc_id: str
    name: str | None
    cidrs: list[str]
    is_default: bool
    is_watchly_vpc: bool
    registered_as: int | None = Field(description="Watchly's id for it, once registered.")


class AvailableVpcs(BaseModel):
    account: AccountBrief
    region: str
    vpcs: list[AvailableVpc]


# --- checks -------------------------------------------------------------------


class CheckCreate(BaseModel):
    check_type: InfraCheckType
    name: str | None = Field(default=None, min_length=1, max_length=255)
    settings: dict = Field(default_factory=dict, description="The type's own settings.")
    check_interval_seconds: int = Field(default=300, ge=30, le=86_400)
    timeout_seconds: int | None = Field(
        default=None, ge=1, le=120, description=f"Ping {PING_TIMEOUT_SECONDS} s, else 10 s by default."
    )
    retries_on_failure: int = Field(default=1, ge=0, le=3)
    is_enabled: bool = True

    @model_validator(mode="after")
    def _settings(self) -> Self:
        self.settings = validate_settings(self.check_type, self.settings)
        if self.timeout_seconds is None:
            self.timeout_seconds = default_timeout(self.check_type)
        return self


class CheckUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    settings: dict | None = Field(default=None, description="Replaces them; validated for the check's type.")
    check_interval_seconds: int | None = Field(default=None, ge=30, le=86_400)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    retries_on_failure: int | None = Field(default=None, ge=0, le=3)
    is_enabled: bool | None = None


class ProblemRead(BaseModel):
    kind: str = Field(
        description=(
            "`slow_response`, `packet_loss`, `targets_unhealthy`, `instances_failing`, "
            "`instances_unhealthy`, `capacity_short`, `db_busy`, `replication_broken`, "
            "`high_cpu`, `low_storage`, `storage_filling`, `low_memory`, `many_connections`, "
            "`connections_near_limit`, `iops_saturated`, `low_burst_balance`, `low_cpu_credits`, "
            "`low_disk_space` or `replica_lag`."
        )
    )
    check_id: int
    since: datetime
    detail: str | None = None


class CheckRead(BaseModel):
    id: int
    check_type: InfraCheckType
    name: str
    is_enabled: bool
    check_interval_seconds: int
    timeout_seconds: int
    retries_on_failure: int
    settings: dict
    health: CheckHealth
    consecutive_failures: int
    last_checked_at: datetime | None
    last_result: dict | None
    snapshot: dict | None
    problems: list[ProblemRead]

    @classmethod
    def of(cls, check) -> "CheckRead":
        return cls(
            id=check.id,
            check_type=check.check_type,
            name=check.name,
            is_enabled=check.is_enabled,
            check_interval_seconds=check.check_interval_seconds,
            timeout_seconds=check.timeout_seconds,
            retries_on_failure=check.retries_on_failure,
            settings=check.settings,
            health=check.health,
            consecutive_failures=check.consecutive_failures,
            last_checked_at=check.last_checked_at,
            last_result=check.last_result,
            snapshot=check.snapshot,
            problems=[
                ProblemRead(kind=kind, check_id=check.id, since=entry["since"], detail=entry.get("detail"))
                for kind, entry in (check.problems or {}).items()
                if entry.get("since")
            ],
        )


# --- resources ----------------------------------------------------------------


class ResourceCreate(BaseModel):
    project_id: int
    vpc_id: int
    kind: ResourceKind
    aws_id: str = Field(
        min_length=1,
        max_length=512,
        description=(
            "Instance id, load balancer ARN or name, Auto Scaling group name, or RDS DB "
            "instance identifier."
        ),
    )
    name: str | None = Field(default=None, min_length=1, max_length=255)
    environment: WebsiteEnvironment | None = None
    max_down_alerts: int = Field(default=4, ge=1, le=50)
    is_enabled: bool = True
    notify_scale_out: bool = Field(default=False, description="Auto Scaling group only: notify when instances join.")
    notify_scale_in: bool = Field(default=False, description="Auto Scaling group only: notify when instances leave.")
    scale_health_check: ScaleHealthCheck | None = Field(
        default=None,
        description="Auto Scaling group only: probed on each new instance; the scale-out notification says if it answered.",
    )
    checks: list[CheckCreate] | None = Field(
        default=None,
        max_length=50,
        description="Null adds the suggested checks.",
    )


class ResourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    environment: WebsiteEnvironment | None = None
    max_down_alerts: int | None = Field(default=None, ge=1, le=50)
    is_enabled: bool | None = None
    notify_scale_out: bool | None = Field(default=None, description="Auto Scaling group only.")
    notify_scale_in: bool | None = Field(default=None, description="Auto Scaling group only.")
    scale_health_check: ScaleHealthCheck | None = Field(
        default=None, description="Auto Scaling group only; send null to remove it."
    )


class ResourceRecipientsUpdate(BaseModel):
    recipient_ids: list[int] = Field(min_length=1, max_length=100)


class EdgeItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str = Field(description="e.g. `acl`, `core`, `rate`, `tls`.")
    group: Literal["waf", "alb"]
    label: str
    status: Literal["ok", "weak", "missing", "unknown", "info"]
    value: str | None
    note: str | None


class EdgeExtraRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    label: str
    value: str


class EdgeSecurityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    acl_name: str | None = Field(description="The Web ACL in front of the ALB; null when there is none.")
    acl_arn: str | None
    score: int = Field(description="How many of the graded items are ok.")
    total: int = Field(description="Items that are ok, weak or missing; unknown and info ones are left out.")
    grade: str = Field(description="`A` to `F` by the share that is ok; `?` when nothing could be graded.")
    items: list[EdgeItemRead]
    extras: list[EdgeExtraRead] = Field(
        default_factory=list, description="Further rules and settings, shown, not graded."
    )


class ResourceRead(BaseModel):
    id: int
    project: ProjectBrief
    vpc: VpcBrief
    kind: ResourceKind
    name: str
    environment: WebsiteEnvironment | None
    aws_id: str
    aws_arn: str | None
    address: str | None
    aws_state: str | None
    aws_detail: dict
    edge_security: EdgeSecurityRead | None = Field(
        default=None,
        description=(
            "ALB only: its AWS WAF Web ACL and hardening settings, graded; null for "
            "other kinds, and until the first sync that read them."
        ),
    )
    synced_at: datetime | None
    missing_since: datetime | None
    is_enabled: bool
    state: ResourceState
    down_since: datetime | None
    degraded_since: datetime | None
    last_checked_at: datetime | None
    problems: list[ProblemRead]
    max_down_alerts: int
    notify_scale_out: bool
    notify_scale_in: bool
    scale_health_check: ScaleHealthCheck | None
    checks: list[CheckRead]
    maintenance: MaintenanceWindowRead | None
    upcoming_maintenance: list[MaintenanceWindowRead]
    extra_recipients: list[ProjectMemberRead]
    created_by_id: int | None
    created_at: datetime

    @classmethod
    def of(cls, resource) -> "ResourceRead":
        return cls(
            id=resource.id,
            project=ProjectBrief.model_validate(resource.project),
            vpc=VpcBrief.model_validate(resource.vpc),
            kind=resource.kind,
            name=resource.name,
            environment=resource.environment,
            aws_id=resource.aws_id,
            aws_arn=resource.aws_arn,
            address=resource.address,
            aws_state=resource.aws_state,
            aws_detail=resource.aws_detail or {},
            edge_security=(
                EdgeSecurityRead.model_validate(report)
                if (report := grade_edge_security(resource.aws_detail))
                else None
            ),
            synced_at=resource.synced_at,
            missing_since=resource.missing_since,
            is_enabled=resource.is_enabled,
            state=resource.state,
            down_since=resource.down_since,
            degraded_since=resource.degraded_since,
            last_checked_at=resource.last_checked_at,
            problems=[ProblemRead(**problem) for problem in resource.problems],
            max_down_alerts=resource.max_down_alerts,
            notify_scale_out=resource.notify_scale_out,
            notify_scale_in=resource.notify_scale_in,
            scale_health_check=(
                ScaleHealthCheck.model_validate(resource.scale_health_check) if resource.scale_health_check else None
            ),
            checks=[CheckRead.of(check) for check in resource.checks],
            maintenance=(
                MaintenanceWindowRead.model_validate(resource.maintenance)
                if resource.maintenance
                else None
            ),
            upcoming_maintenance=[
                MaintenanceWindowRead.model_validate(w) for w in resource.upcoming_maintenance
            ],
            extra_recipients=[ProjectMemberRead.model_validate(u) for u in resource.recipients],
            created_by_id=resource.created_by_id,
            created_at=resource.created_at,
        )


class ResourceList(BaseModel):
    items: list[ResourceRead]
    total: int
    limit: int
    offset: int


class ResourceSort(str, enum.Enum):
    NAME = "name"
    #: Down first, then degraded and missing, then the rest by name.
    STATE = "state"


class CheckResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    check_id: int
    checked_at: datetime
    ok: bool
    response_time_ms: int | None
    dns_ms: int | None
    connect_ms: int | None
    tls_ms: int | None
    summary: str
    error: str | None
    error_type: str | None
    address: str | None
    metric: float | None
    detail: dict | None


class CheckResultList(BaseModel):
    items: list[CheckResultRead]
    total: int
    limit: int
    offset: int


class CheckNowResponse(BaseModel):
    resource: ResourceRead
    results: list[CheckResultRead]
    alert_sent: str | None = Field(
        default=None, description="The notification the run raised, if any."
    )


# --- stats --------------------------------------------------------------------


class CheckStatsFigures(BaseModel):
    checks: int
    up_checks: int
    uptime_percent: float | None
    avg_response_ms: int | None
    max_response_ms: int | None
    metric_min: float | None = None
    metric_max: float | None = None


class CheckStatsBucket(CheckStatsFigures):
    start: datetime


class CheckStats(CheckStatsFigures):
    check_id: int
    check_type: InfraCheckType
    name: str
    metric: str | None = Field(
        description=(
            "What `metric_*` measures: `packet_loss_percent`, `healthy_targets`, "
            "`healthy_instances`, `cpu_percent`, or null."
        )
    )
    series: list[CheckStatsBucket]


class ResourceStats(BaseModel):
    range: StatsRange
    start: datetime
    end: datetime
    bucket_seconds: int
    checks: list[CheckStats]


# --- targets ------------------------------------------------------------------


class TargetSpan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from")
    to: datetime | None
    state: str


class TargetRead(BaseModel):
    id: str
    name: str | None = None
    address: str | None = None
    port: int | None = None
    az: str | None = None
    state: str
    since: datetime | None = None
    reason: str | None = None
    description: str | None = None
    timeline: list[TargetSpan] = Field(default_factory=list)


class TargetGroupTargets(BaseModel):
    check_id: int
    name: str
    arn: str
    health_check: str | None = None
    targets: list[TargetRead]


class ResourceTargets(BaseModel):
    target_groups: list[TargetGroupTargets]


# --- events -------------------------------------------------------------------


class EventResource(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: ResourceKind


class EventRead(BaseModel):
    id: int
    kind: str = Field(
        description=(
            "`infra_down`, `infra_recovered`, `infra_degraded`, `vpc_unreachable`, "
            "`vpc_recovered`, `asg_scaled_out`, `asg_scaled_in`, `deploy_started`, "
            "`deploy_finished` or `account_capacity`."
        )
    )
    occurred_at: datetime
    summary: str
    downtime_seconds: int | None
    project_id: int
    vpc: VpcBrief | None = Field(
        description="Null for a deployment or an account's capacity, which are about a whole AWS account."
    )
    resource: EventResource | None


class EventList(BaseModel):
    items: list[EventRead]
    total: int
    limit: int


# --- deployments --------------------------------------------------------------


class DeploymentResource(BaseModel):
    id: int
    name: str
    kind: ResourceKind


class DeploymentRead(BaseModel):
    id: int
    project: ProjectBrief
    account: AccountBrief
    region: str
    deployment_id: str = Field(description="CodeDeploy's id, `d-XXXXXXXXX`.")
    application_name: str
    group_name: str
    status: str = Field(
        description=(
            "CodeDeploy's: `Created`, `Queued`, `InProgress`, `Baking`, `Ready`, `Succeeded`, "
            "`Failed`, `Stopped`; `Unwatched` when its account stopped watching first."
        )
    )
    environment: WebsiteEnvironment | None
    creator: str | None
    description: str | None
    revision: str | None
    resources: list[DeploymentResource] = Field(description="The monitored resources it silences.")
    fallback: str | None = Field(
        description="Set when every resource of its region was silenced instead, and why."
    )
    started_at: datetime
    finished_at: datetime | None
    error: str | None
    is_active: bool


class DeploymentList(BaseModel):
    items: list[DeploymentRead]
    total: int
    limit: int
    offset: int


# --- discovery and import -----------------------------------------------------


class Suggestion(BaseModel):
    check_type: InfraCheckType
    name: str
    settings: dict


class TargetGroupBrief(BaseModel):
    arn: str
    name: str
    targets: int | None = None
    health_check: str | None = None


class MonitoredBy(BaseModel):
    resource_id: int
    project: ProjectBrief | None = Field(description="Null when the caller cannot see that project.")


class DiscoveredItem(BaseModel):
    key: str
    kind: ResourceKind
    aws_id: str
    aws_arn: str | None = None
    name: str
    detail: str
    address: str | None
    #: An instance with a public IP, an internet-facing load balancer, or a
    #: group with an instance that has a public IP.
    public: bool = False
    public_ip: str | None = None
    subnet: str | None = None
    aws_state: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    auto_scaling_group: str | None = None
    #: For an Auto Scaling group: its instances now.
    instances: list[dict] = Field(default_factory=list)
    target_groups: list[TargetGroupBrief] = Field(default_factory=list)
    monitored_by: list[MonitoredBy] = Field(default_factory=list)
    suggested: list[Suggestion]


class Skipped(BaseModel):
    service: str
    call: str
    reason: str


class DiscoveryResult(BaseModel):
    discovered_at: datetime
    items: list[DiscoveredItem]
    skipped: list[Skipped]


class DiscoverRequest(BaseModel):
    refresh: bool = False


# --- the VPC map ----------------------------------------------------------------

#: What AWS itself says of a node: a target's or an Auto Scaling group member's
#: health, rolled up for a target group. `transition` is a target registering or
#: draining, or an instance launching; `empty` a target group with no target.
AwsHealth = Literal["healthy", "degraded", "unhealthy", "transition", "unknown", "empty"]
TopologyNodeKind = Literal[
    "internet",
    "watchly",
    "aws_api",
    "load_balancer",
    "target_group",
    "auto_scaling_group",
    "instance",
    "ip_target",
    "database",
]


class TopologySubnet(BaseModel):
    id: str
    label: str
    cidr: str | None
    az: str | None
    public: bool | None = Field(description="From its route table; null when that could not be read and no guess fits.")


class TopologyNode(BaseModel):
    id: str = Field(
        description=(
            "`lb:<arn>`, `tg:<arn>`, `asg:<name>`, `i:<instance id>`, `db:<identifier>`, "
            "`ip:<address>`, or `internet`, `watchly`, `aws_api`."
        )
    )
    kind: TopologyNodeKind
    label: str
    subnet: str | None = Field(default=None, description="The subnet id it sits in; null for what spans subnets or is outside.")
    address: str | None = None
    public_ip: str | None = None
    aws_id: str | None = None
    aws_state: str | None = None
    detail: str | None = Field(default=None, description="One line: type, scheme, capacity; a target group's target count.")
    health_check: str | None = Field(
        default=None, description="A target group's health check, e.g. `HTTP /health on 80, 200`."
    )
    public: bool = False
    #: The discovery key that adds it (`ec2:i-...`, `elb:<name>`, `asg:<name>`,
    #: `rds:<identifier>`),
    #: for what is not monitored yet.
    key: str | None = None
    resource_id: int | None = Field(default=None, description="The monitored resource, when the caller can see it.")
    state: ResourceState | None = Field(default=None, description="The monitored resource's state.")
    aws_health: AwsHealth | None = None


class TopologyEdge(BaseModel):
    source: str
    target: str
    kind: Literal["internet", "forwards", "targets", "registers", "launches", "probes", "api"]
    state: Literal["ok", "degraded", "failing", "unknown", "off"] = "unknown"
    #: For `probes`: inside the VPC, or over the internet to a public address.
    via: Literal["vpc", "internet"] | None = None
    label: str | None = None
    detail: str | None = None


class Topology(BaseModel):
    vpc_id: int
    read_at: datetime
    watchly: Literal["in_vpc", "outside", "not_on_ec2"]
    subnets: list[TopologySubnet]
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]
    skipped: list[Skipped] = Field(default_factory=list, description="AWS listings refused; the map is drawn without them.")
    aws_error: str | None = Field(default=None, description="AWS listed nothing: only monitored resources are drawn.")


class ImportItem(BaseModel):
    key: str = Field(min_length=1, max_length=600)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    checks: list[CheckCreate] | None = Field(
        default=None, max_length=50, description="Null adds the suggested checks."
    )


class ImportRequest(BaseModel):
    project_id: int
    environment: WebsiteEnvironment | None = None
    items: list[ImportItem] = Field(min_length=1, max_length=100)


class ImportResult(BaseModel):
    created: list[ResourceRead]


# --- diagnose, overview, self -------------------------------------------------


class DiagnoseRequest(BaseModel):
    """An existing check (`resource_id` and `check_id`), or an unsaved one for
    the add form (`vpc_id`, `project_id`, `kind`, `aws_id` and `check`)."""

    resource_id: int | None = None
    check_id: int | None = None
    vpc_id: int | None = None
    project_id: int | None = None
    kind: ResourceKind | None = None
    aws_id: str | None = Field(default=None, max_length=512)
    check: CheckCreate | None = None

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        saved = self.resource_id is not None and self.check_id is not None
        unsaved = None not in (self.vpc_id, self.project_id, self.kind, self.aws_id, self.check)
        if saved == unsaved:
            raise ValueError(
                "give resource_id and check_id, or vpc_id, project_id, kind, aws_id and check"
            )
        return self


class DiagnoseStep(BaseModel):
    step: str
    ok: bool | None = None
    skipped: bool = False
    time_ms: int | None = None
    error_type: str | None = None
    detail: str | None = None


class DiagnoseHint(BaseModel):
    kind: str
    resource: str
    detail: str
    fix: str


class DiagnoseResult(BaseModel):
    ok: bool
    failed_step: str | None
    summary: str
    steps: list[DiagnoseStep]
    hints: list[DiagnoseHint] = Field(default_factory=list)


class KindCounts(BaseModel):
    kind: ResourceKind
    total: int
    down: int
    degraded: int


class AttentionResource(BaseModel):
    id: int
    name: str
    kind: ResourceKind
    vpc: VpcBrief


class AttentionItem(BaseModel):
    resource: AttentionResource
    state: ResourceState
    since: datetime | None
    summary: str


class OverviewVpc(BaseModel):
    id: int
    name: str
    health: Literal["reachable", "unreachable", "untested"]
    cidrs: list[str]
    counts: StateCounts
    #: What the overview's map draws: Watchly → account → VPC.
    region: str | None = None
    aws_vpc_id: str | None = None
    is_watchly_vpc: bool = False
    unreachable_since: datetime | None = None
    account: AccountBrief | None = None
    project: ProjectBrief | None = None


class Overview(BaseModel):
    counts: StateCounts
    vpcs: list[OverviewVpc]
    by_kind: list[KindCounts]
    attention: list[AttentionItem]


class SecurityGroupRead(BaseModel):
    id: str
    name: str


class SelfInstance(BaseModel):
    id: str
    type: str | None
    region: str | None
    az: str | None
    vpc_id: str | None
    subnet_id: str | None
    private_ip: str | None
    public_ip: str | None
    security_groups: list[SecurityGroupRead]
    iam_role: str | None
    account_id: str | None


class ImdsRead(BaseModel):
    tokens_required: bool
    hop_limit: int | None = None


class GuardRead(BaseModel):
    metadata_blocked: bool
    website_private_targets: str
    deny_extra: list[str]
    own_addresses: list[str]


class PermissionRead(BaseModel):
    action: str
    ok: bool | None = Field(description="Null when it could not be tested.")
    detail: str | None = None


class CallerRead(BaseModel):
    account: str | None
    arn: str | None


class AccountTestResult(BaseModel):
    """What the account's credentials may do, in one of its regions."""

    ok: bool = Field(description="The credentials work and every permission was granted.")
    tested_at: datetime
    region: str | None
    caller: CallerRead | None
    caller_error: str | None = None
    permissions: list[PermissionRead]


class SelfRead(BaseModel):
    instance: SelfInstance | None
    region: str | None
    caller: CallerRead | None
    caller_error: str | None = None
    imds: ImdsRead | None
    guard: GuardRead
    permissions: list[PermissionRead]
    probes: dict[str, bool]
