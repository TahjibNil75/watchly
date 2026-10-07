"""Docker hosts and their containers, as reported by the Watchly Docker agent.

A Docker project has hosts. Each host has an agent (a separate program,
github.com/TahjibNil75/watchly-docker-agent) that pushes the state of every
container to `POST /api/v1/docker/ingest` with the host's token: a heartbeat
every `interval_seconds`, and a smaller push soon after each Docker event.
Watchly never connects to the host; when the pushes stop, the host is offline
(see `monitor.py`).

A container is known by its name on its host, not Docker's id, which changes
every time the container is recreated, so a redeploy is the same container.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.monitoring.projects.models import Project


class HostStatus(str, enum.Enum):
    #: Created, but its agent has not pushed yet.
    PENDING = "pending"
    ONLINE = "online"
    #: Its agent stopped pushing.
    OFFLINE = "offline"
    #: Its agent pushes, but cannot reach Docker on the host.
    DOCKER_DOWN = "docker_down"


#: Docker states in which a container counts as up. A paused container was
#: paused on purpose; `restarting` is a crash under a restart policy.
UP_STATES = frozenset({"running", "paused"})
#: What Watchly sets for a container no longer on its host.
REMOVED = "removed"


class DockerHost(Base, TimestampMixin):
    """A machine running Docker, and the agent token it reports with."""

    __tablename__ = "docker_hosts"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_docker_hosts_project_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: SHA-256 of the agent token (`app.core.security.hash_token`). The token
    #: itself is shown once, when created or rotated.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    #: Its first and last characters, so the page can show which one is set.
    token_hint: Mapped[str] = mapped_column(String(32), nullable=False)
    #: A `HostStatus` value.
    status: Mapped[str] = mapped_column(
        String(16), default=HostStatus.PENDING.value, server_default=HostStatus.PENDING.value, nullable=False
    )
    #: When it went offline (or lost Docker); None while online or pending.
    down_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: An offline alert went out, so its recovery is owed one too.
    down_alerted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Sent back to the agent with every push, so it can be changed here.
    interval_seconds: Mapped[int] = mapped_column(
        Integer, default=30, server_default=text("30"), nullable=False
    )
    #: Container name globs the agent skips (`buildkit_*`), sent back likewise.
    ignore_patterns: Mapped[list[str]] = mapped_column(
        ARRAY(String(255)), default=list, server_default=text("'{}'"), nullable=False
    )

    # What the agent last said about the host.
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    os: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kernel: Mapped[str | None] = mapped_column(String(255), nullable=True)
    arch: Mapped[str | None] = mapped_column(String(32), nullable=True)
    docker_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cpus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mem_total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    containers_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    containers_running: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The agent's own memory use, so its footprint can be seen.
    agent_mem_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Why the agent cannot reach Docker, while it cannot.
    docker_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Docker events the agent had to drop (its buffer overflowed), in total.
    dropped_events: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    project: Mapped[Project] = relationship(lazy="selectin")

    def __repr__(self) -> str:
        return f"<DockerHost {self.name!r} {self.status}>"


class DockerContainer(Base):
    """One container on a host, by name, with its latest state."""

    __tablename__ = "docker_containers"
    __table_args__ = (UniqueConstraint("host_id", "name", name="uq_docker_containers_host_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        ForeignKey("docker_hosts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The host's, copied so lists can be scoped without a join.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Docker's id of the container now holding the name.
    docker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    image: Mapped[str] = mapped_column(String(1024), nullable=False)
    image_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Docker's state (`running`, `exited`, `restarting`...), or `removed`.
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Docker's own words, e.g. "Up 3 hours (healthy)".
    status_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: `healthy`, `unhealthy`, `starting`, or `none` without a healthcheck.
    health: Mapped[str] = mapped_column(String(16), default="none", server_default="none", nullable=False)
    restart_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oom_killed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    docker_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    compose_project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    compose_service: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When it disappeared from its host; it is forgotten
    #: DOCKER_REMOVED_CONTAINER_DAYS later.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    #: When it stopped running, having been seen running; None while up.
    down_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    down_alerted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    #: Recorded and shown, but never alerted on.
    muted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    #: Problems short of down (`unhealthy`, `oom`, `restart_loop`,
    #: `high_cpu`, `high_memory`): streaks, open since, last alerted at.
    problems: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False)
    #: The latest resource sample, as the agent sent it.
    metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metrics_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    host: Mapped[DockerHost] = relationship(lazy="selectin")

    @property
    def is_up(self) -> bool:
        return self.state in UP_STATES

    def __repr__(self) -> str:
        return f"<DockerContainer {self.name!r} {self.state}>"


class DockerSample(Base):
    """One container's resource use at one heartbeat. Kept
    DOCKER_SAMPLE_RETENTION_HOURS; `DockerSampleHourly` keeps the rest."""

    __tablename__ = "docker_samples"
    __table_args__ = (Index("ix_docker_samples_container_at", "container_id", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    container_id: Mapped[int] = mapped_column(
        ForeignKey("docker_containers.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    #: 100 = one full core.
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mem_limit_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mem_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_rx_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_tx_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    blk_read_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    blk_write_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    pids: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Lets a sample be added with its (possibly new) container before flush.
    container: Mapped[DockerContainer] = relationship()


class DockerSampleHourly(Base):
    """A container's samples summed up by the hour; see `history.py`."""

    __tablename__ = "docker_sample_hourly"

    container_id: Mapped[int] = mapped_column(
        ForeignKey("docker_containers.id", ondelete="CASCADE"), primary_key=True
    )
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    samples: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_max: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mem_pct_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_rx_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_tx_avg: Mapped[float | None] = mapped_column(Float, nullable=True)


class EventSource(str, enum.Enum):
    #: Reported by the agent: Docker's own event (`die`, `oom`, `start`...).
    DOCKER = "docker"
    #: Decided by Watchly: down, recovered, a problem opened, a host lost.
    WATCHLY = "watchly"


class DockerEvent(Base):
    """The Docker feed: Docker's events as the agent reported them, and what
    Watchly made of them."""

    __tablename__ = "docker_events"
    __table_args__ = (UniqueConstraint("host_id", "event_key", name="uq_docker_events_host_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    host_id: Mapped[int] = mapped_column(ForeignKey("docker_hosts.id", ondelete="CASCADE"), nullable=False)
    #: None for a host event.
    container_id: Mapped[int | None] = mapped_column(
        ForeignKey("docker_containers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    container_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: An `EventSource` value.
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    #: Docker's action, or what Watchly decided (`down`, `host_offline`...).
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    downtime_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The agent's id for a Docker event, so one delivered twice is stored once.
    event_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = [
    "REMOVED",
    "UP_STATES",
    "DockerContainer",
    "DockerEvent",
    "DockerHost",
    "DockerSample",
    "DockerSampleHourly",
    "EventSource",
    "HostStatus",
]
