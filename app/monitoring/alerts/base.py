"""What every channel shares: kinds, the channel-neutral message, the interfaces.

An event (see `events.py`) says *what happened*. It is composed into a
`Message` — title, text, facts, tone — and each channel only decides how to
draw one. So a new notification kind touches the events and the catalog, and a
new channel touches only its own renderer.
"""

import abc
import enum
import math
from dataclasses import dataclass, field
from datetime import datetime

from app.monitoring.websites.models import CheckType, Website


class NotificationKind(str, enum.Enum):
    #: First failed check of an outage.
    DOWN = "down"
    #: A follow-up while the site is still down.
    STILL_DOWN = "still_down"
    #: The site answered again.
    RECOVERED = "recovered"
    #: The site's HTTPS certificate is close to (or past) its end date.
    SSL_EXPIRING = "ssl_expiring"
    #: The site answers, but slower than its threshold, repeatedly.
    SLOW_RESPONSE = "slow_response"
    #: A pinged host answers, but keeps losing packets over its threshold.
    PACKET_LOSS = "packet_loss"
    #: Once a month, per project: how every site did.
    MONTHLY_REPORT = "monthly_report"


class Tone(str, enum.Enum):
    """How a message should feel; each channel maps this to its own colours."""

    CRITICAL = "critical"
    WARNING = "warning"
    SUCCESS = "success"
    INFO = "info"


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


def format_uptime(percent: float | None) -> str:
    """`99.996` -> `"99.99%"`. Never rounds up to 100% while something failed:
    a report that says 100.00% next to "1 incident" is a report nobody trusts."""
    if percent is None:
        return "—"
    if percent >= 100:
        return "100%"
    return f"{math.floor(percent * 100) / 100:.2f}%"


def uptime_tone(percent: float | None) -> Tone:
    if percent is None or percent >= 99.9:
        return Tone.SUCCESS
    if percent >= 99.0:
        return Tone.WARNING
    return Tone.CRITICAL


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def escape_mrkdwn(text: str) -> str:
    """Slack's three reserved characters. Anything that came from outside a
    template — a remote server's error text, a header — must pass through this,
    or `<!channel>` in a response body would page the whole channel."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True, slots=True)
class SlackTarget:
    """Where one alert should be posted, resolved at raise time.

    The token is already decrypted here. It never reaches an API response and
    should not be logged.
    """

    bot_token: str
    channel_id: str
    #: Post as a reply in this thread (the `ts` of its first message) instead
    #: of as a new message in the channel.
    thread_ts: str | None = None
    #: A threaded reply that should also appear in the channel itself.
    broadcast: bool = False


@dataclass(frozen=True, slots=True)
class WebsiteSnapshot:
    """The website's state at the moment an alert fired.

    A frozen copy rather than the live ORM object: an event may be rendered,
    logged or retried after later checks have already mutated the row, and an
    alert must describe the moment it was raised.
    """

    id: int
    project_id: int
    name: str
    url: str
    project_name: str
    expected_status: int
    timeout_seconds: int
    check_interval_seconds: int
    down_since: datetime | None
    consecutive_failures: int
    check_type: CheckType = CheckType.HTTP

    @property
    def is_ping(self) -> bool:
        return self.check_type is CheckType.PING

    @property
    def noun(self) -> str:
        """What alerts call it: a site, or for a ping check a host."""
        return "host" if self.is_ping else "site"

    @classmethod
    def of(cls, website: Website) -> "WebsiteSnapshot":
        return cls(
            id=website.id,
            project_id=website.project_id,
            name=website.name,
            url=website.url,
            project_name=website.project.name if website.project else "—",
            expected_status=website.expected_status,
            timeout_seconds=website.timeout_seconds,
            check_interval_seconds=website.check_interval_seconds,
            down_since=website.down_since,
            consecutive_failures=website.consecutive_failures,
            check_type=website.check_type,
        )


@dataclass(frozen=True, slots=True)
class Stat:
    """One headline number, drawn as a tile."""

    label: str
    value: str
    tone: Tone | None = None


@dataclass(frozen=True, slots=True)
class SiteRow:
    """One website's line in a monthly report."""

    name: str
    url: str
    uptime: str
    #: For drawing a bar; None when there is nothing to draw.
    uptime_percent: float | None
    downtime: str
    incidents: int
    response: str
    tone: Tone


@dataclass(slots=True)
class Message:
    """A notification, composed and ready for any channel to draw.

    `subject` and `body` are the parts an admin can rewrite; everything else is
    the built-in design and facts. `body` is plain text for email; `slack_body`
    is the same template with Slack's reserved characters escaped in the
    substituted values only, so an author's own `<!channel>` still works.
    """

    kind: NotificationKind
    tone: Tone
    #: Small label above the title, e.g. "Outage".
    kicker: str
    title: str
    subject: str
    body: str
    slack_body: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    stats: list[Stat] = field(default_factory=list)
    sites: list[SiteRow] = field(default_factory=list)
    #: Sites left out of `sites` to keep the message a readable size.
    sites_omitted: int = 0
    #: Fixed explanatory text — what happens next, how a number was measured.
    note: str = ""
    #: (label, url) for the call-to-action button; None when no dashboard URL.
    link: tuple[str, str] | None = None


class Notification(abc.ABC):
    """One thing worth telling somebody about. Subclasses are dataclasses."""

    kind: NotificationKind
    project_id: int
    recipients: tuple[str, ...]
    #: None when the project has no Slack configured.
    slack: SlackTarget | None
    #: Set by the Slack alerter once its bot post lands: that message's `ts`,
    #: which later alerts pass as `thread_ts` to reply under it.
    slack_ts: str | None = None

    @abc.abstractmethod
    def describe(self) -> str:
        """Short label for log lines, e.g. `down alert for https://…`."""

    @abc.abstractmethod
    def context(self) -> dict[str, str]:
        """Placeholder values (`{{name}}` in a template), as plain strings."""

    @abc.abstractmethod
    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        """Build the message around the (already rendered) subject and body."""

    @abc.abstractmethod
    def payload(self, subject: str) -> dict:
        """Machine-readable form for the generic webhook."""


class Alerter(abc.ABC):
    """A delivery channel. Implementations must not raise on delivery failure."""

    #: Matches the per-kind toggle (`email`, `slack`). A channel with no toggle,
    #: like the webhook, is never switched off per project.
    name: str = "alerter"

    @abc.abstractmethod
    async def is_configured(self, event: Notification) -> bool:
        """Whether this channel can attempt a send for *this* event.

        Takes the event because configuration is not always global — Slack is
        resolved per project.
        """

    @abc.abstractmethod
    async def send(self, event: Notification, message: Message) -> bool:
        """Deliver `message`. Returns True on success; logs and returns False otherwise."""
