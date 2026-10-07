"""Request and response bodies for Docker monitoring.

The `Ingest*` models are the agent's wire format, schema v1, as documented in
the agent's repository (docs/protocol.md). They ignore fields they do not
know, so a newer agent still talks to this server, and cut over-long strings
instead of refusing them: one odd container name must not block a host.
"""

import fnmatch
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from app.monitoring.docker.models import HostStatus
from app.monitoring.websites.schemas import StatsRange


def _cut(limit: int):
    def cut(value: object) -> object:
        return value[:limit] if isinstance(value, str) else value

    return BeforeValidator(cut)


Str32 = Annotated[str, _cut(32)]
Str64 = Annotated[str, _cut(64)]
Str255 = Annotated[str, _cut(255)]
Str1024 = Annotated[str, _cut(1024)]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class IngestAgent(_Wire):
    version: Str64 = ""
    started_at: datetime | None = None
    interval_s: int | None = None
    mem_bytes: int | None = Field(default=None, ge=0)


class IngestHost(_Wire):
    hostname: Str255 = ""
    os: Str255 = ""
    kernel: Str255 = ""
    arch: Str32 = ""
    docker_version: Str64 = ""
    api_version: Str32 = ""
    cpus: int | None = Field(default=None, ge=0)
    mem_total_bytes: int | None = Field(default=None, ge=0)
    containers_total: int | None = Field(default=None, ge=0)
    containers_running: int | None = Field(default=None, ge=0)


class IngestMetrics(_Wire):
    cpu_pct: float | None = None
    mem_used_bytes: int = Field(default=0, ge=0)
    mem_limit_bytes: int = Field(default=0, ge=0)
    mem_pct: float | None = None
    net_rx_bps: float | None = None
    net_tx_bps: float | None = None
    blk_read_bps: float | None = None
    blk_write_bps: float | None = None
    pids: int = Field(default=0, ge=0)


class IngestContainer(_Wire):
    id: Str64
    name: Str255 = Field(min_length=1)
    image: Str1024 = ""
    image_id: Str255 = ""
    state: Str32 = Field(min_length=1)
    status: Str255 = ""
    health: Str32 = "none"
    restart_count: int = Field(default=0, ge=0)
    exit_code: int | None = None
    oom_killed: bool = False
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    labels: dict[str, Str255] | None = None
    metrics: IngestMetrics | None = None


class IngestEvent(_Wire):
    id: Str64 = Field(min_length=1)
    time: datetime
    action: Str32 = Field(min_length=1)
    container_id: Str64 = ""
    container_name: Str255 = ""
    image: Str1024 = ""
    exit_code: int | None = None
    health: Str32 = ""
    signal: Str32 = ""


class Ingest(_Wire):
    """One push from an agent."""

    schema_: Literal[1] = Field(alias="schema")
    kind: Literal["heartbeat", "event"]
    sent_at: datetime | None = None
    agent: IngestAgent = Field(default_factory=IngestAgent)
    host: IngestHost | None = None
    docker_error: Str1024 = ""
    #: Null when Docker could not be reached.
    containers: list[IngestContainer] | None = Field(default=None, max_length=5000)
    events: list[IngestEvent] = Field(default_factory=list, max_length=1000)
    dropped_events: int = Field(default=0, ge=0)


class AgentConfig(BaseModel):
    interval_s: int
    ignore: list[str]


class IngestResponse(BaseModel):
    config: AgentConfig


# --- API ---------------------------------------------------------------------


def _check_patterns(patterns: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(p.strip() for p in patterns if p.strip()))
    for pattern in cleaned:
        if len(pattern) > 255:
            raise ValueError("an ignore pattern may be at most 255 characters")
        try:
            fnmatch.translate(pattern)
        except Exception as exc:  # pragma: no cover - translate rarely raises
            raise ValueError(f"{pattern!r} is not a valid pattern") from exc
    return cleaned


class HostSettings(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    interval_seconds: int = Field(
        default=30, ge=10, le=300, description="How often the agent sends a heartbeat, 10-300 seconds."
    )
    ignore_patterns: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Container name globs the agent skips, e.g. `buildkit_*`.",
    )

    _patterns = field_validator("ignore_patterns")(_check_patterns)


class HostCreate(HostSettings):
    project_id: int
    name: str = Field(min_length=1, max_length=255)


class HostUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    interval_seconds: int | None = Field(default=None, ge=10, le=300)
    ignore_patterns: list[str] | None = Field(default=None, max_length=50)

    @field_validator("ignore_patterns")
    @classmethod
    def _patterns(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _check_patterns(value)


class ContainerCounts(BaseModel):
    total: int = 0
    running: int = 0
    down: int = 0
    unhealthy: int = 0
    stopped: int = 0


class ProjectBrief(BaseModel):
    id: int
    name: str


class HostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project: ProjectBrief
    name: str
    description: str | None
    status: HostStatus
    down_since: datetime | None
    last_seen_at: datetime | None
    interval_seconds: int
    ignore_patterns: list[str]
    token_hint: str
    agent_version: str | None
    hostname: str | None
    os: str | None
    kernel: str | None
    arch: str | None
    docker_version: str | None
    cpus: int | None
    mem_total_bytes: int | None
    containers_total: int | None
    containers_running: int | None
    agent_mem_bytes: int | None
    docker_error: str | None
    dropped_events: int
    created_at: datetime
    counts: ContainerCounts = Field(default_factory=ContainerCounts)
    can_manage: bool = False


class HostCreated(BaseModel):
    host: HostRead
    #: Shown once; only its hash is stored.
    token: str


class TokenRotated(BaseModel):
    token: str
    token_hint: str


class HostList(BaseModel):
    items: list[HostRead]


class HostBrief(BaseModel):
    id: int
    name: str
    status: HostStatus


class ContainerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    host: HostBrief
    project: ProjectBrief
    name: str
    docker_id: str
    image: str
    image_id: str | None
    state: str
    status_text: str | None
    health: str
    restart_count: int
    exit_code: int | None
    oom_killed: bool
    docker_created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    compose_project: str | None
    compose_service: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    removed_at: datetime | None
    down_since: datetime | None
    muted: bool
    #: Open problems: kind -> {since, detail}.
    problems: dict[str, dict]
    metrics: dict | None
    metrics_at: datetime | None
    #: The state as Watchly sees it: `running`, `down`, `unhealthy`,
    #: `stopped` (never seen running), `restarting`, `paused`, `removed`, or
    #: `unknown` while its host is not reporting.
    condition: str
    can_manage: bool = False


class ContainerList(BaseModel):
    items: list[ContainerRead]
    counts: ContainerCounts


class ContainerUpdate(BaseModel):
    muted: bool


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    host_id: int
    host_name: str
    container_id: int | None
    container_name: str | None
    source: str
    kind: str
    occurred_at: datetime
    summary: str
    exit_code: int | None
    downtime_seconds: int | None


class EventList(BaseModel):
    items: list[EventRead]


class StatsPoint(BaseModel):
    at: datetime
    cpu_avg: float | None
    cpu_max: float | None
    mem_avg: float | None
    mem_max: int | None
    mem_pct_max: float | None
    net_rx_avg: float | None
    net_tx_avg: float | None


class ContainerStats(BaseModel):
    range: StatsRange
    bucket_seconds: int
    #: Host CPUs, to read cpu as a share of the host (100 = one core).
    cpus: int | None
    mem_limit_bytes: int | None
    points: list[StatsPoint]
