"""What infrastructure notifies about. Each event composes itself into the
channel-neutral `Message`, so email, Slack, Telegram, WhatsApp and the webhook
draw it with no code of their own; see `alerts/base.py`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

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
from app.monitoring.infra.aws.models import AwsResource, ResourceKind

_KIND_NOUN = {
    ResourceKind.SERVER: "server",
    ResourceKind.LOAD_BALANCER: "load balancer",
    ResourceKind.AUTO_SCALING_GROUP: "Auto Scaling group",
    ResourceKind.DATABASE: "database",
}

#: How each problem kind reads in an alert.
PROBLEM_LABELS = {
    "slow_response": "Slow response",
    "packet_loss": "Packet loss",
    "targets_unhealthy": "Unhealthy targets",
    "instances_failing": "Instances failing their check",
    "instances_unhealthy": "Unhealthy instances",
    "capacity_short": "Short of desired capacity",
    "db_busy": "Database busy",
    "replication_broken": "Replication broken",
    "high_cpu": "High CPU",
    "low_storage": "Low free storage",
    "low_memory": "Low freeable memory",
    "many_connections": "Many connections",
    "replica_lag": "Replica lag",
}


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _link(label: str, path: str) -> tuple[str, str] | None:
    url = dashboard_url(path)
    return (label, url) if url else None


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """The resource at the moment an alert fired; see `WebsiteSnapshot`."""

    id: int
    project_id: int
    project_name: str
    name: str
    kind: ResourceKind
    aws_id: str
    address: str | None
    vpc_name: str
    aws_state: str | None
    down_since: datetime | None

    @property
    def noun(self) -> str:
        return _KIND_NOUN[self.kind]

    @property
    def label(self) -> str:
        """The noun to start a line with: "Server", "Auto Scaling group"."""
        return self.noun[:1].upper() + self.noun[1:]

    @classmethod
    def of(cls, resource: AwsResource) -> "ResourceSnapshot":
        return cls(
            id=resource.id,
            project_id=resource.project_id,
            project_name=resource.project.name if resource.project else "—",
            name=resource.name,
            kind=resource.kind,
            aws_id=resource.aws_id,
            address=resource.address,
            vpc_name=resource.vpc.name if resource.vpc else "—",
            aws_state=resource.aws_state,
            down_since=resource.down_since,
        )


@dataclass(frozen=True, slots=True)
class CheckLine:
    """One check, as an alert lists it."""

    name: str
    check_type: str
    summary: str
    error_type: str | None = None

    def text(self) -> str:
        return f"{self.name} ({self.check_type}): {self.summary}"


@dataclass(kw_only=True)
class InfraEvent(Notification):
    """Base for events about one resource."""

    resource: ResourceSnapshot
    checked_at: datetime
    recipients: tuple[str, ...] = ()
    slack: SlackTarget | None = None
    telegram: TelegramTarget | None = None
    whatsapp: WhatsAppTarget | None = None

    @property
    def project_id(self) -> int:
        return self.resource.project_id

    def describe(self) -> str:
        return f"{self.kind.value} alert for {self.resource.noun} {self.resource.name}"

    def _base_context(self) -> dict[str, str]:
        r = self.resource
        return {
            "project": r.project_name,
            "resource_name": r.name,
            "resource_kind": r.noun,
            "aws_id": r.aws_id,
            "vpc_name": r.vpc_name,
            "aws_state": r.aws_state or "—",
            "address": r.address or "—",
            "checked_at": _utc(self.checked_at),
            "dashboard_url": dashboard_url(f"/infra/resources/{r.id}"),
        }

    def _identity_facts(self) -> list[tuple[str, str]]:
        r = self.resource
        rows = [
            ("Project", r.project_name),
            (r.label, r.name),
            ("AWS id", r.aws_id),
            ("VPC", r.vpc_name),
        ]
        if r.address:
            rows.append(("Address", r.address))
        if r.aws_state:
            rows.append(("AWS state", r.aws_state))
        return rows

    def _resource_payload(self) -> dict:
        r = self.resource
        return {
            "id": r.id,
            "name": r.name,
            "kind": r.kind.value,
            "aws_id": r.aws_id,
            "address": r.address,
            "vpc": r.vpc_name,
            "aws_state": r.aws_state,
        }


@dataclass(kw_only=True)
class InfraOutageEvent(InfraEvent):
    """Down, still down, or recovered: one per resource, not per check."""

    kind: NotificationKind
    failing: tuple[CheckLine, ...] = ()
    downtime_seconds: float = 0.0
    attempt: int = 1
    max_attempts: int = 1

    @property
    def is_recovery(self) -> bool:
        return self.kind is NotificationKind.INFRA_RECOVERED

    @property
    def downtime(self) -> str:
        return format_duration(self.downtime_seconds)

    def _failing_text(self) -> str:
        if self.is_recovery:
            return "none"
        return "; ".join(line.text() for line in self.failing) or "—"

    def context(self) -> dict[str, str]:
        down_since = self.resource.down_since
        return {
            **self._base_context(),
            "failing_checks": self._failing_text(),
            "downtime": self.downtime,
            "down_since": _utc(down_since) if down_since else "—",
            "attempt": str(self.attempt),
            "max_attempts": str(self.max_attempts),
        }

    def _note(self) -> str:
        noun = self.resource.noun
        if self.kind is NotificationKind.INFRA_DOWN:
            return (
                f"Its checks keep running. You will get up to {self.max_attempts - 1} further "
                f"alerts while this {noun} stays down, then one when every check passes again. "
                "A timeout usually means a security group: “Why is this down?” on its page "
                "says which step failed."
            )
        if self.kind is NotificationKind.INFRA_STILL_DOWN:
            remaining = self.max_attempts - self.attempt
            if remaining > 0:
                return f"{remaining} further down-alert(s) will follow, then alerts pause until it recovers."
            return "This is the final down-alert; the next one will be the recovery."
        return f"No further alerts until this {noun} goes down again."

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        name = self.resource.name
        kicker, title, tone = {
            NotificationKind.INFRA_DOWN: ("Outage", f"{name} is down", Tone.CRITICAL),
            NotificationKind.INFRA_STILL_DOWN: ("Still down", f"{name} is still down", Tone.WARNING),
            NotificationKind.INFRA_RECOVERED: ("Recovered", f"{name} is back up", Tone.SUCCESS),
        }[self.kind]
        facts = self._identity_facts()
        facts.append(("Status", "UP" if self.is_recovery else "DOWN"))
        facts.append(("Checked at", _utc(self.checked_at)))
        for line in self.failing if not self.is_recovery else ():
            facts.append((f"Failing: {line.name}", line.summary))
        if self.downtime_seconds > 0:
            facts.append(("Total downtime" if self.is_recovery else "Down for", self.downtime))
        if self.resource.down_since and not self.is_recovery:
            facts.append(("Down since", _utc(self.resource.down_since)))
        if not self.is_recovery:
            facts.append(("Alert", f"{self.attempt} of {self.max_attempts}"))
        return Message(
            kind=self.kind,
            tone=tone,
            kicker=kicker,
            title=title,
            subject=subject,
            body=body,
            slack_body=slack_body,
            facts=facts,
            note=self._note(),
            link=_link("Open in Watchly", f"/infra/resources/{self.resource.id}"),
        )

    def payload(self, subject: str) -> dict:
        return {
            "event": self.kind.value,
            "subject": subject,
            "resource": self._resource_payload(),
            "checked_at": self.checked_at.isoformat(),
            "failing_checks": [
                {"name": line.name, "check_type": line.check_type, "summary": line.summary,
                 "error_type": line.error_type}
                for line in self.failing
            ],
            "outage": {
                "downtime_seconds": round(self.downtime_seconds),
                "downtime_human": self.downtime,
                "attempt": self.attempt,
                "max_attempts": self.max_attempts,
            },
        }


@dataclass(kw_only=True)
class InfraDegradedEvent(InfraEvent):
    """Up, but a problem has been open for INFRA_PROBLEM_CHECKS checks."""

    kind: NotificationKind = NotificationKind.INFRA_DEGRADED
    problem: str
    problem_detail: str
    check: CheckLine
    #: INFRA_PROBLEM_ALERT_COOLDOWN_SECONDS, for the note.
    cooldown_seconds: int = 21_600

    @property
    def problem_label(self) -> str:
        return PROBLEM_LABELS.get(self.problem, self.problem.replace("_", " ").capitalize())

    def context(self) -> dict[str, str]:
        return {
            **self._base_context(),
            "problem": self.problem_label,
            "problem_detail": self.problem_detail,
            "check": f"{self.check.name} ({self.check.check_type})",
        }

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        facts = self._identity_facts()
        facts.extend(
            [
                ("Problem", self.problem_label),
                ("Detail", self.problem_detail),
                ("Check", f"{self.check.name} ({self.check.check_type})"),
                ("Checked at", _utc(self.checked_at)),
            ]
        )
        return Message(
            kind=self.kind,
            tone=Tone.WARNING,
            kicker="Degraded",
            title=f"{self.resource.name}: {self.problem_label.lower()}",
            subject=subject,
            body=body,
            slack_body=slack_body,
            facts=facts,
            note=(
                f"It is still up. This alert repeats at most every "
                f"{format_duration(self.cooldown_seconds)} while the problem lasts."
            ),
            link=_link("Open in Watchly", f"/infra/resources/{self.resource.id}"),
        )

    def payload(self, subject: str) -> dict:
        return {
            "event": self.kind.value,
            "subject": subject,
            "resource": self._resource_payload(),
            "checked_at": self.checked_at.isoformat(),
            "problem": {"kind": self.problem, "detail": self.problem_detail},
            "check": {"name": self.check.name, "check_type": self.check.check_type},
        }


@dataclass(frozen=True, slots=True)
class ScaledInstance:
    """An instance that joined or left a group, and for one that joined, what
    its health endpoint said (none when the group has no endpoint set)."""

    id: str
    az: str | None = None
    address: str | None = None
    health_ok: bool | None = None
    health: str | None = None

    def text(self) -> str:
        parts = [self.id]
        if self.az:
            parts.append(self.az)
        if self.address:
            parts.append(self.address)
        line = ", ".join(parts)
        if self.health is not None:
            line += f": health endpoint {'OK' if self.health_ok else 'FAILED'} ({self.health})"
        return line


@dataclass(kw_only=True)
class ScalingEvent(InfraEvent):
    """Instances joined (`asg_scaled_out`) or left (`asg_scaled_in`) an Auto
    Scaling group. Only sent to a group whose user asked for it."""

    kind: NotificationKind
    instances: tuple[ScaledInstance, ...]
    in_service: int = 0
    desired: int = 0
    health_endpoint: str | None = None

    @property
    def is_out(self) -> bool:
        return self.kind is NotificationKind.ASG_SCALED_OUT

    @property
    def count(self) -> str:
        n = len(self.instances)
        return f"{n} instance{'s' if n != 1 else ''}"

    @property
    def health_failed(self) -> bool:
        return any(i.health_ok is False for i in self.instances)

    def context(self) -> dict[str, str]:
        return {
            **self._base_context(),
            "change": "added" if self.is_out else "removed",
            "instance_count": str(len(self.instances)),
            "instances": "; ".join(i.text() for i in self.instances),
            "in_service": str(self.in_service),
            "desired": str(self.desired),
            "health_endpoint": self.health_endpoint or "not set",
        }

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        name = self.resource.name
        if self.is_out:
            kicker, title = "Scaled out", f"{name} added {self.count}"
            tone = Tone.WARNING if self.health_failed else Tone.INFO
        else:
            kicker, title, tone = "Scaled in", f"{name} removed {self.count}", Tone.INFO
        facts = self._identity_facts()
        facts.append(("In service now", f"{self.in_service} (desired {self.desired})"))
        facts.append(("Checked at", _utc(self.checked_at)))
        if self.health_endpoint:
            facts.append(("Health endpoint", self.health_endpoint))
        for instance in self.instances[:10]:
            facts.append(("Added" if self.is_out else "Removed", instance.text()))
        if len(self.instances) > 10:
            facts.append(("And", f"{len(self.instances) - 10} more"))
        note = (
            "You are told about this because scaling notifications are on for this group; "
            "turn them off on its settings."
        )
        return Message(
            kind=self.kind,
            tone=tone,
            kicker=kicker,
            title=title,
            subject=subject,
            body=body,
            slack_body=slack_body,
            facts=facts,
            note=note,
            link=_link("Open in Watchly", f"/infra/resources/{self.resource.id}"),
        )

    def payload(self, subject: str) -> dict:
        return {
            "event": self.kind.value,
            "subject": subject,
            "resource": self._resource_payload(),
            "checked_at": self.checked_at.isoformat(),
            "in_service": self.in_service,
            "desired": self.desired,
            "health_endpoint": self.health_endpoint,
            "instances": [
                {"id": i.id, "az": i.az, "address": i.address, "health_ok": i.health_ok, "health": i.health}
                for i in self.instances
            ],
        }


@dataclass(kw_only=True)
class VpcEvent(Notification):
    """A whole VPC lost, or back: one alert per project instead of one per
    resource."""

    kind: NotificationKind
    vpc_id: int
    vpc_name: str
    cidrs: tuple[str, ...]
    project_id: int
    project_name: str
    occurred_at: datetime
    failed_checks: int = 0
    total_checks: int = 0
    downtime_seconds: float = 0.0
    failing: tuple[CheckLine, ...] = ()
    recipients: tuple[str, ...] = ()
    slack: SlackTarget | None = None
    telegram: TelegramTarget | None = None
    whatsapp: WhatsAppTarget | None = None

    @property
    def is_recovery(self) -> bool:
        return self.kind is NotificationKind.VPC_RECOVERED

    def describe(self) -> str:
        return f"{self.kind.value} alert for VPC {self.vpc_name} (project {self.project_name})"

    def context(self) -> dict[str, str]:
        base = {
            "project": self.project_name,
            "vpc_name": self.vpc_name,
            "cidrs": ", ".join(self.cidrs),
            "dashboard_url": dashboard_url(f"/infra/vpcs/{self.vpc_id}"),
        }
        if self.is_recovery:
            return {**base, "downtime": format_duration(self.downtime_seconds)}
        return {**base, "failed_checks": str(self.failed_checks), "total_checks": str(self.total_checks)}

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        facts = [
            ("Project", self.project_name),
            ("VPC", self.vpc_name),
            ("Ranges", ", ".join(self.cidrs)),
            ("At", _utc(self.occurred_at)),
        ]
        if self.is_recovery:
            facts.append(("Unreachable for", format_duration(self.downtime_seconds)))
            note = (
                "Resources still down now send their own alerts; the ones that came back "
                "with the VPC do not."
            )
            kicker, title, tone = "Reachable", f"{self.vpc_name} answers again", Tone.SUCCESS
        else:
            facts.append(("Checks failing to connect", f"{self.failed_checks} of {self.total_checks}"))
            for line in self.failing[:5]:
                facts.append((line.name, line.summary))
            note = (
                "Watchly cannot reach into this VPC: a security group, NACL, route or peering "
                "change is the usual cause. Each resource's own down alert is held until the "
                "VPC answers again."
            )
            kicker, title, tone = "VPC unreachable", f"{self.vpc_name} is unreachable", Tone.CRITICAL
        return Message(
            kind=self.kind,
            tone=tone,
            kicker=kicker,
            title=title,
            subject=subject,
            body=body,
            slack_body=slack_body,
            facts=facts,
            note=note,
            link=_link("Open in Watchly", f"/infra/vpcs/{self.vpc_id}"),
        )

    def payload(self, subject: str) -> dict:
        return {
            "event": self.kind.value,
            "subject": subject,
            "vpc": {"id": self.vpc_id, "name": self.vpc_name, "cidrs": list(self.cidrs)},
            "project": {"id": self.project_id, "name": self.project_name},
            "occurred_at": self.occurred_at.isoformat(),
            "failed_checks": self.failed_checks,
            "total_checks": self.total_checks,
            "downtime_seconds": round(self.downtime_seconds),
        }


#: How each environment reads in a deployment's title.
ENVIRONMENT_LABELS = {
    "development": "development",
    "testing": "testing",
    "uat": "UAT",
    "staging": "staging",
    "production": "production",
}

#: How CodeDeploy's end states read, and their tone.
DEPLOY_OUTCOMES = {
    "Succeeded": ("succeeded", Tone.SUCCESS),
    "Failed": ("failed", Tone.CRITICAL),
    "Stopped": ("was stopped", Tone.WARNING),
}


@dataclass(frozen=True, slots=True)
class DeployedResource:
    """A monitored resource a deployment silences."""

    id: int
    name: str
    kind: ResourceKind

    def text(self) -> str:
        return f"{self.name} ({_KIND_NOUN[self.kind]})"


@dataclass(kw_only=True)
class DeployEvent(Notification):
    """A CodeDeploy deployment started (`deploy_started`), or ended
    (`deploy_finished`): one message to the account's project, the end one
    replying to the start's."""

    kind: NotificationKind
    project_id: int
    project_name: str
    account_name: str
    aws_account_id: str | None
    region: str
    deployment_id: str
    application: str
    group: str
    environment: str | None
    started_at: datetime
    creator: str | None = None
    description: str | None = None
    revision: str | None = None
    resources: tuple[DeployedResource, ...] = ()
    #: Why every resource of the region was silenced instead of its own.
    fallback: str | None = None
    #: CodeDeploy's end state and when; with `deploy_finished` only.
    status: str | None = None
    finished_at: datetime | None = None
    error: str | None = None
    #: DEPLOY_SILENCE_MAX_MINUTES and DEPLOY_SETTLE_SECONDS, for the note.
    max_minutes: int = 180
    settle_seconds: int = 60
    recipients: tuple[str, ...] = ()
    slack: SlackTarget | None = None
    telegram: TelegramTarget | None = None
    whatsapp: WhatsAppTarget | None = None

    @property
    def is_finished(self) -> bool:
        return self.kind is NotificationKind.DEPLOY_FINISHED

    @property
    def environment_label(self) -> str:
        if not self.environment:
            return "—"
        return ENVIRONMENT_LABELS.get(self.environment, self.environment)

    @property
    def target(self) -> str:
        """"orders-api to production", or just "orders-api"."""
        if self.environment:
            return f"{self.application} to {self.environment_label}"
        return self.application

    @property
    def outcome(self) -> str:
        return DEPLOY_OUTCOMES.get(self.status or "", (f"ended ({self.status})", Tone.WARNING))[0]

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or datetime.now(UTC)
        return max((end - self.started_at).total_seconds(), 0.0)

    def _resources_text(self) -> str:
        return "; ".join(r.text() for r in self.resources) or "none"

    def describe(self) -> str:
        return f"{self.kind.value} for {self.deployment_id} ({self.application}/{self.group})"

    def context(self) -> dict[str, str]:
        base = {
            "project": self.project_name,
            "environment": self.environment_label,
            "application": self.application,
            "deployment_group": self.group,
            "deployment_id": self.deployment_id,
            "account": self.account_name,
            "region": self.region,
            "revision": self.revision or "—",
            "initiated_by": self.creator or "—",
            "resources": self._resources_text(),
            "resource_count": str(len(self.resources)),
            "dashboard_url": dashboard_url(f"/projects/{self.project_id}"),
        }
        if not self.is_finished:
            return base
        return {
            **base,
            "status": self.outcome,
            "duration": format_duration(self.duration_seconds),
            "error": self.error or "—",
        }

    def _note(self) -> str:
        if self.is_finished:
            if not self.resources:
                return "No monitored resource was paused for it."
            settle = (
                f"in {format_duration(self.settle_seconds)}" if self.settle_seconds else "now"
            )
            return (
                f"Checks of its resources resume {settle}: any still down then alerts as usual."
            )
        if self.fallback:
            return (
                f"{self.fallback} So down alerts for every server, load balancer and Auto Scaling "
                f"group of this account in {self.region} are paused until it ends."
            )
        if not self.resources:
            return (
                "None of this project's monitored resources is in this deployment, so no alerts "
                "are paused."
            )
        return (
            "Down alerts for the resources below are paused until it ends, for at most "
            f"{format_duration(self.max_minutes * 60)}. Watchly says when it does."
        )

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        facts = [
            ("Project", self.project_name),
            ("Environment", self.environment_label),
            ("Application", self.application),
            ("Deployment group", self.group),
            ("Deployment", self.deployment_id),
            ("AWS account", f"{self.account_name} ({self.aws_account_id})" if self.aws_account_id else self.account_name),
            ("Region", self.region),
        ]
        if self.revision:
            facts.append(("Revision", self.revision))
        if self.description:
            facts.append(("Description", self.description))
        if self.creator:
            facts.append(("Initiated by", self.creator))
        facts.append(("Started", _utc(self.started_at)))
        if self.is_finished:
            if self.finished_at:
                facts.append(("Ended", _utc(self.finished_at)))
            facts.append(("Took", format_duration(self.duration_seconds)))
            if self.error:
                facts.append(("Error", self.error))
            kicker = "Deployed" if self.status == "Succeeded" else "Deployment ended"
            title = f"{self.target} {self.outcome}"
            tone = DEPLOY_OUTCOMES.get(self.status or "", ("", Tone.WARNING))[1]
        else:
            # CodeDeploy's automatic rollback is a deployment of its own.
            kicker = "Rolling back" if self.creator == "codeDeployRollback" else "Deploying"
            title, tone = f"Deploying {self.target}", Tone.INFO
        for resource in self.resources[:10]:
            facts.append(("Alerts paused" if not self.is_finished else "Resumes", resource.text()))
        if len(self.resources) > 10:
            facts.append(("And", f"{len(self.resources) - 10} more"))
        return Message(
            kind=self.kind,
            tone=tone,
            kicker=kicker,
            title=title,
            subject=subject,
            body=body,
            slack_body=slack_body,
            facts=facts,
            note=self._note(),
            link=_link("Open in Watchly", f"/projects/{self.project_id}"),
        )

    def payload(self, subject: str) -> dict:
        return {
            "event": self.kind.value,
            "subject": subject,
            "project": {"id": self.project_id, "name": self.project_name},
            "deployment": {
                "id": self.deployment_id,
                "application": self.application,
                "group": self.group,
                "environment": self.environment,
                "account": {"name": self.account_name, "aws_account_id": self.aws_account_id},
                "region": self.region,
                "revision": self.revision,
                "description": self.description,
                "creator": self.creator,
                "started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "status": self.status,
                "error": self.error,
            },
            "resources": [{"id": r.id, "name": r.name, "kind": r.kind.value} for r in self.resources],
            "fallback": self.fallback,
        }


__all__ = [
    "CheckLine",
    "DeployEvent",
    "DeployedResource",
    "InfraDegradedEvent",
    "InfraEvent",
    "InfraOutageEvent",
    "PROBLEM_LABELS",
    "ResourceSnapshot",
    "ScaledInstance",
    "ScalingEvent",
    "VpcEvent",
]
