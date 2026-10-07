"""What Docker monitoring notifies about. Like the infrastructure events, each
composes itself into the channel-neutral `Message`; see `alerts/base.py`."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.monitoring.alerts.base import (
    Message,
    Notification,
    NotificationKind,
    SlackTarget,
    TelegramTarget,
    Tone,
    WhatsAppTarget,
    format_duration,
)
from app.monitoring.alerts.events import dashboard_url
from app.monitoring.docker.models import DockerContainer, DockerHost

CONTAINER_KINDS = frozenset(
    {
        NotificationKind.DOCKER_CONTAINER_DOWN,
        NotificationKind.DOCKER_CONTAINER_RECOVERED,
        NotificationKind.DOCKER_CONTAINER_UNHEALTHY,
        NotificationKind.DOCKER_CONTAINER_OOM,
        NotificationKind.DOCKER_RESTART_LOOP,
        NotificationKind.DOCKER_RESOURCE_HIGH,
    }
)
HOST_KINDS = frozenset({NotificationKind.DOCKER_HOST_OFFLINE, NotificationKind.DOCKER_HOST_RECOVERED})


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _link(label: str, path: str) -> tuple[str, str] | None:
    url = dashboard_url(path)
    return (label, url) if url else None


@dataclass(frozen=True, slots=True)
class ContainerSnapshot:
    """The container as it was when the alert fired."""

    id: int
    project_id: int
    project_name: str
    host_id: int
    host_name: str
    name: str
    image: str
    state: str
    health: str
    exit_code: int | None
    restart_count: int
    compose_project: str | None = None
    compose_service: str | None = None

    @classmethod
    def of(cls, container: DockerContainer, host: DockerHost) -> "ContainerSnapshot":
        return cls(
            id=container.id,
            project_id=host.project_id,
            project_name=host.project.name,
            host_id=host.id,
            host_name=host.name,
            name=container.name,
            image=container.image,
            state=container.state,
            health=container.health,
            exit_code=container.exit_code,
            restart_count=container.restart_count,
            compose_project=container.compose_project,
            compose_service=container.compose_service,
        )


# Per kind: tone, kicker, and the title after "<container> on <host>".
_CONTAINER_LOOK = {
    NotificationKind.DOCKER_CONTAINER_DOWN: (Tone.CRITICAL, "Container down", "is down"),
    NotificationKind.DOCKER_CONTAINER_RECOVERED: (Tone.SUCCESS, "Recovered", "is running again"),
    NotificationKind.DOCKER_CONTAINER_UNHEALTHY: (Tone.WARNING, "Unhealthy", "is unhealthy"),
    NotificationKind.DOCKER_CONTAINER_OOM: (Tone.CRITICAL, "Out of memory", "ran out of memory"),
    NotificationKind.DOCKER_RESTART_LOOP: (Tone.CRITICAL, "Restart loop", "keeps restarting"),
    NotificationKind.DOCKER_RESOURCE_HIGH: (Tone.WARNING, "Resources", "is running hot"),
}

_CONTAINER_NOTES = {
    NotificationKind.DOCKER_CONTAINER_DOWN: (
        "Watchly waits a short grace period before calling a container down, so a redeploy stays "
        "quiet. `docker logs {name}` on the host usually says why it stopped. You will hear again "
        "when it runs."
    ),
    NotificationKind.DOCKER_CONTAINER_RECOVERED: "",
    NotificationKind.DOCKER_CONTAINER_UNHEALTHY: (
        "The container runs, but its own healthcheck fails. `docker inspect --format "
        "'{{{{json .State.Health}}}}' {name}` shows the latest probe output."
    ),
    NotificationKind.DOCKER_CONTAINER_OOM: (
        "The kernel killed a process in the container because it reached its memory limit. "
        "Raise the limit (`mem_limit` / `--memory`) or find what grows."
    ),
    NotificationKind.DOCKER_RESTART_LOOP: (
        "Docker's restart policy keeps starting it and it keeps exiting. `docker logs {name}` "
        "shows the crash."
    ),
    NotificationKind.DOCKER_RESOURCE_HIGH: "",
}


@dataclass(kw_only=True)
class DockerContainerEvent(Notification):
    """Something happened to one container: down, back, unhealthy, killed for
    memory, restarting in a loop, or running hot."""

    kind: NotificationKind
    container: ContainerSnapshot
    #: What happened, in a sentence: "exited with code 137".
    detail: str
    occurred_at: datetime
    #: With DOCKER_CONTAINER_DOWN and _RECOVERED: how long it has been down.
    downtime_seconds: int | None = None
    #: DOCKER_PROBLEM_ALERT_COOLDOWN_SECONDS, for the problems' note.
    cooldown_seconds: int = 3600
    recipients: tuple[str, ...] = ()
    slack: SlackTarget | None = None
    telegram: TelegramTarget | None = None
    whatsapp: WhatsAppTarget | None = None

    def __post_init__(self) -> None:
        if self.kind not in CONTAINER_KINDS:
            raise ValueError(f"{self.kind} is not a container kind")

    @property
    def path(self) -> str:
        return f"/docker/containers/{self.container.id}"

    def describe(self) -> str:
        return f"{self.kind.value} alert for container {self.container.name} on {self.container.host_name}"

    def context(self) -> dict[str, str]:
        c = self.container
        return {
            "project": c.project_name,
            "host": c.host_name,
            "container": c.name,
            "image": c.image,
            "state": c.state,
            "health": c.health,
            "exit_code": "—" if c.exit_code is None else str(c.exit_code),
            "restart_count": str(c.restart_count),
            "detail": self.detail,
            "downtime": format_duration(self.downtime_seconds) if self.downtime_seconds is not None else "—",
            "occurred_at": _utc(self.occurred_at),
            "dashboard_url": dashboard_url(self.path),
        }

    def _note(self) -> str:
        note = _CONTAINER_NOTES[self.kind].format(name=self.container.name)
        if self.kind in (
            NotificationKind.DOCKER_CONTAINER_UNHEALTHY,
            NotificationKind.DOCKER_CONTAINER_OOM,
            NotificationKind.DOCKER_RESTART_LOOP,
            NotificationKind.DOCKER_RESOURCE_HIGH,
        ):
            note += (
                f" Watchly says so again about this container at most every "
                f"{format_duration(self.cooldown_seconds)}."
            )
        return note.strip()

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        c = self.container
        tone, kicker, verb = _CONTAINER_LOOK[self.kind]
        facts = [
            ("Project", c.project_name),
            ("Host", c.host_name),
            ("Container", c.name),
            ("Image", c.image),
        ]
        if c.compose_project:
            facts.append(("Compose", f"{c.compose_project} / {c.compose_service or '—'}"))
        facts.append(("What happened", self.detail))
        if self.downtime_seconds is not None:
            label = "Was down for" if self.kind is NotificationKind.DOCKER_CONTAINER_RECOVERED else "Down for"
            facts.append((label, format_duration(self.downtime_seconds)))
        facts.append(("At", _utc(self.occurred_at)))
        return Message(
            kind=self.kind,
            tone=tone,
            kicker=kicker,
            title=f"{c.name} on {c.host_name} {verb}",
            subject=subject,
            body=body,
            slack_body=slack_body,
            facts=facts,
            note=self._note(),
            link=_link("Open in Watchly", self.path),
        )

    def payload(self, subject: str) -> dict:
        c = self.container
        return {
            "event": self.kind.value,
            "subject": subject,
            "project": {"id": c.project_id, "name": c.project_name},
            "host": {"id": c.host_id, "name": c.host_name},
            "container": {
                "id": c.id,
                "name": c.name,
                "image": c.image,
                "state": c.state,
                "health": c.health,
                "exit_code": c.exit_code,
                "restart_count": c.restart_count,
                "compose_project": c.compose_project,
                "compose_service": c.compose_service,
            },
            "detail": self.detail,
            "downtime_seconds": self.downtime_seconds,
            "occurred_at": self.occurred_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class HostSnapshot:
    id: int
    project_id: int
    project_name: str
    name: str
    hostname: str | None
    last_seen_at: datetime | None
    containers_running: int | None

    @classmethod
    def of(cls, host: DockerHost) -> "HostSnapshot":
        return cls(
            id=host.id,
            project_id=host.project_id,
            project_name=host.project.name,
            name=host.name,
            hostname=host.hostname,
            last_seen_at=host.last_seen_at,
            containers_running=host.containers_running,
        )


@dataclass(kw_only=True)
class DockerHostEvent(Notification):
    """A host's agent stopped reporting or lost Docker, or is back: one alert
    for the host instead of one per container."""

    kind: NotificationKind
    host: HostSnapshot
    #: Why it is offline: the agent went quiet, or Docker does not answer it.
    reason: str
    occurred_at: datetime
    downtime_seconds: int | None = None
    recipients: tuple[str, ...] = ()
    slack: SlackTarget | None = None
    telegram: TelegramTarget | None = None
    whatsapp: WhatsAppTarget | None = None

    def __post_init__(self) -> None:
        if self.kind not in HOST_KINDS:
            raise ValueError(f"{self.kind} is not a host kind")

    @property
    def is_recovery(self) -> bool:
        return self.kind is NotificationKind.DOCKER_HOST_RECOVERED

    @property
    def path(self) -> str:
        return f"/docker/hosts/{self.host.id}"

    def describe(self) -> str:
        return f"{self.kind.value} alert for Docker host {self.host.name}"

    def context(self) -> dict[str, str]:
        h = self.host
        return {
            "project": h.project_name,
            "host": h.name,
            "hostname": h.hostname or "—",
            "reason": self.reason,
            "last_seen": _utc(h.last_seen_at) if h.last_seen_at else "never",
            "downtime": format_duration(self.downtime_seconds) if self.downtime_seconds is not None else "—",
            "occurred_at": _utc(self.occurred_at),
            "dashboard_url": dashboard_url(self.path),
        }

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        h = self.host
        facts = [("Project", h.project_name), ("Host", h.name)]
        if h.hostname and h.hostname != h.name:
            facts.append(("Hostname", h.hostname))
        facts.append(("Why" if not self.is_recovery else "Was", self.reason))
        if h.last_seen_at and not self.is_recovery:
            facts.append(("Last heard from", _utc(h.last_seen_at)))
        if self.downtime_seconds is not None:
            facts.append(("Offline for", format_duration(self.downtime_seconds)))
        facts.append(("At", _utc(self.occurred_at)))
        note = (
            ""
            if self.is_recovery
            else "Its containers' alerts are paused until it reports again: Watchly cannot tell "
            "what they are doing. Check that the host is up and the agent container runs "
            "(`docker ps --filter name=watchly-agent`)."
        )
        return Message(
            kind=self.kind,
            tone=Tone.SUCCESS if self.is_recovery else Tone.CRITICAL,
            kicker="Host back" if self.is_recovery else "Host offline",
            title=f"{h.name} {'reports again' if self.is_recovery else 'stopped reporting'}",
            subject=subject,
            body=body,
            slack_body=slack_body,
            facts=facts,
            note=note,
            link=_link("Open in Watchly", self.path),
        )

    def payload(self, subject: str) -> dict:
        h = self.host
        return {
            "event": self.kind.value,
            "subject": subject,
            "project": {"id": h.project_id, "name": h.project_name},
            "host": {"id": h.id, "name": h.name, "hostname": h.hostname},
            "reason": self.reason,
            "last_seen_at": h.last_seen_at.isoformat() if h.last_seen_at else None,
            "downtime_seconds": self.downtime_seconds,
            "occurred_at": self.occurred_at.isoformat(),
        }


def sample(kind: NotificationKind, now: datetime, recipients: tuple[str, ...]) -> Notification:
    """A made-up event of `kind`, for previewing templates (`samples.py`)."""
    if kind in HOST_KINDS:
        recovery = kind is NotificationKind.DOCKER_HOST_RECOVERED
        return DockerHostEvent(
            kind=kind,
            host=HostSnapshot(
                id=1,
                project_id=1,
                project_name="Acme Corp",
                name="prod-docker-1",
                hostname="ip-10-20-1-15",
                last_seen_at=now - timedelta(minutes=4),
                containers_running=12,
            ),
            reason="the agent stopped reporting",
            occurred_at=now,
            downtime_seconds=1260 if recovery else None,
            recipients=recipients,
        )
    container = ContainerSnapshot(
        id=7,
        project_id=1,
        project_name="Acme Corp",
        host_id=1,
        host_name="prod-docker-1",
        name="orders-api",
        image="ghcr.io/acme/orders-api:1.14.2",
        state="exited",
        health="none",
        exit_code=137,
        restart_count=0,
        compose_project="acme",
        compose_service="orders-api",
    )
    details = {
        NotificationKind.DOCKER_CONTAINER_DOWN: ("exited with code 137 (killed)", 75),
        NotificationKind.DOCKER_CONTAINER_RECOVERED: ("running again", 840),
        NotificationKind.DOCKER_CONTAINER_UNHEALTHY: ("its healthcheck reports unhealthy", None),
        NotificationKind.DOCKER_CONTAINER_OOM: ("killed for using all of its 512 MiB memory limit", None),
        NotificationKind.DOCKER_RESTART_LOOP: ("restarted 4 times in the last 10m", None),
        NotificationKind.DOCKER_RESOURCE_HIGH: ("memory at 94% of its 512 MiB limit for 3 heartbeats", None),
    }
    detail, downtime = details[kind]
    if kind is NotificationKind.DOCKER_CONTAINER_RECOVERED:
        container = ContainerSnapshot(**{**_fields(container), "state": "running", "exit_code": 0})
    return DockerContainerEvent(
        kind=kind,
        container=container,
        detail=detail,
        occurred_at=now,
        downtime_seconds=downtime,
        recipients=recipients,
    )


def _fields(snapshot: ContainerSnapshot) -> dict:
    return {name: getattr(snapshot, name) for name in ContainerSnapshot.__slots__}


__all__ = [
    "CONTAINER_KINDS",
    "HOST_KINDS",
    "ContainerSnapshot",
    "DockerContainerEvent",
    "DockerHostEvent",
    "HostSnapshot",
    "sample",
]
