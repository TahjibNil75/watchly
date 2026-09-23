"""The alert payload every channel renders, and the channel interface."""

import abc
import enum
from dataclasses import dataclass
from datetime import UTC, datetime

from app.monitoring.websites.checker import CheckResult
from app.monitoring.websites.models import Website


class AlertKind(str, enum.Enum):
    #: First failed check of an outage.
    DOWN = "down"
    #: A follow-up while the site is still down.
    STILL_DOWN = "still_down"
    #: The site answered again.
    RECOVERED = "recovered"


def format_duration(seconds: float) -> str:
    """`3725` -> `"1h 2m"`. Alert bodies read better than raw seconds."""
    total = int(max(seconds, 0))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class SlackTarget:
    """Where one alert should be posted, resolved at raise time.

    The token is already decrypted here. It never reaches an API response and
    should not be logged.
    """

    bot_token: str
    channel_id: str


@dataclass(frozen=True, slots=True)
class WebsiteSnapshot:
    """The website's state at the moment an alert fired.

    A frozen copy rather than the live ORM object: an event may be rendered,
    logged or retried after later checks have already mutated the row, and an
    alert must describe the moment it was raised.
    """

    id: int
    name: str
    url: str
    project_name: str
    expected_status: int
    timeout_seconds: int
    check_interval_seconds: int
    down_since: datetime | None
    consecutive_failures: int

    @classmethod
    def of(cls, website: Website) -> "WebsiteSnapshot":
        return cls(
            id=website.id,
            name=website.name,
            url=website.url,
            project_name=website.project.name if website.project else "—",
            expected_status=website.expected_status,
            timeout_seconds=website.timeout_seconds,
            check_interval_seconds=website.check_interval_seconds,
            down_since=website.down_since,
            consecutive_failures=website.consecutive_failures,
        )


@dataclass(slots=True)
class AlertEvent:
    """One thing worth telling a human about."""

    kind: AlertKind
    website: WebsiteSnapshot
    result: CheckResult
    #: Seconds the site has been down (or was down, for a recovery).
    downtime_seconds: float = 0.0
    #: Which alert this is within the current outage, 1-based.
    attempt: int = 1
    max_attempts: int = 1
    recipients: tuple[str, ...] = ()
    #: None when the site's project has no Slack configured.
    slack: SlackTarget | None = None

    @property
    def downtime(self) -> str:
        return format_duration(self.downtime_seconds)

    @property
    def is_recovery(self) -> bool:
        return self.kind is AlertKind.RECOVERED

    @property
    def subject(self) -> str:
        # Project-qualified so a recipient on several projects can tell at a
        # glance which client is affected.
        site = f"{self.website.project_name} / {self.website.name}"
        match self.kind:
            case AlertKind.DOWN:
                return f"[DOWN] {site} is not responding"
            case AlertKind.STILL_DOWN:
                # Kept ASCII: a non-ASCII subject gets RFC2047-encoded, which
                # reads badly in raw logs and older mail clients.
                return f"[STILL DOWN] {site} has been down for {self.downtime}"
            case AlertKind.RECOVERED:
                return f"[RECOVERED] {site} is back up after {self.downtime}"

    def facts(self) -> list[tuple[str, str]]:
        """Ordered diagnostic rows shared by every channel's rendering."""
        r = self.result
        rows: list[tuple[str, str]] = [
            ("Project", self.website.project_name),
            ("Website", self.website.name),
            ("URL", self.website.url),
            ("Status", "UP" if r.is_up else "DOWN"),
            ("Checked at", r.checked_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")),
        ]
        if r.status_code is not None:
            code = f"{r.status_code}"
            if r.reason:
                code += f" {r.reason}"
            rows.append(("HTTP status", code))
        else:
            rows.append(("HTTP status", "no response"))
        if not r.is_up:
            rows.append(("Expected status", str(self.website.expected_status)))
        if r.error:
            rows.append(("Error", r.error))
        if r.error_type:
            rows.append(("Error type", r.error_type))
        if r.response_time_ms is not None:
            rows.append(("Response time", f"{r.response_time_ms} ms"))
        rows.append(("Timeout", f"{self.website.timeout_seconds}s"))
        if r.final_url and r.redirected:
            rows.append(("Redirected to", r.final_url))
        if r.content_length is not None:
            rows.append(("Response size", f"{r.content_length} bytes"))
        if self.downtime_seconds > 0:
            label = "Total downtime" if self.is_recovery else "Down for"
            rows.append((label, self.downtime))
        if self.website.down_since and not self.is_recovery:
            rows.append(
                (
                    "Down since",
                    self.website.down_since.astimezone(UTC).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    ),
                )
            )
        rows.append(("Consecutive failures", str(self.website.consecutive_failures)))
        rows.append(("Check interval", format_duration(self.website.check_interval_seconds)))
        if not self.is_recovery:
            rows.append(("Alert", f"{self.attempt} of {self.max_attempts}"))
        for name, value in self.result.headers.items():
            rows.append((f"Header: {name}", value))
        return rows

    def as_text(self) -> str:
        width = max(len(label) for label, _ in self.facts())
        lines = [f"{label.ljust(width)}  {value}" for label, value in self.facts()]
        headline = self.subject
        if self.kind is AlertKind.DOWN:
            tail = (
                f"\nThis site will be re-checked every "
                f"{format_duration(self.website.check_interval_seconds)}. "
                f"You will get up to {self.max_attempts - 1} further alerts while it "
                "stays down, then one when it recovers."
            )
        elif self.kind is AlertKind.STILL_DOWN:
            remaining = self.max_attempts - self.attempt
            tail = (
                f"\n{remaining} further down-alert(s) will follow, then alerts pause "
                "until the site recovers."
                if remaining > 0
                else "\nThis is the final down-alert; the next one will be the recovery."
            )
        else:
            tail = "\nNo further alerts until the site goes down again."
        return f"{headline}\n\n" + "\n".join(lines) + "\n" + tail


class Alerter(abc.ABC):
    """A delivery channel. Implementations must not raise on delivery failure."""

    name: str = "alerter"

    @abc.abstractmethod
    async def is_configured(self, event: "AlertEvent") -> bool:
        """Whether this channel can attempt a send for *this* event.

        Takes the event because configuration is not always global — Slack is
        resolved per project.
        """

    @abc.abstractmethod
    async def send(self, event: AlertEvent) -> bool:
        """Deliver `event`. Returns True on success; logs and returns False otherwise."""
