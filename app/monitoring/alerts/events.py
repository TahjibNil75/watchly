"""The things Watchly notifies about, each able to describe and compose itself.

Every event answers four questions the rest of the system asks generically:
what placeholders may a template use (`context`), how should it look
(`compose`), what goes to a webhook (`payload`), and what to call it in a log
line (`describe`).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from app.core.config import settings
from app.monitoring.alerts.base import (
    Message,
    Notification,
    NotificationKind,
    SiteRow,
    SlackTarget,
    Stat,
    Tone,
    WebsiteSnapshot,
    format_duration,
    format_uptime,
    plural,
    uptime_tone,
)
from app.monitoring.websites.checker import CheckResult

#: Sites listed in one report message; the rest are counted, not drawn.
REPORT_SITE_LIMIT = 100

#: A check's timed steps in the order they happen, as alerts name them.
_STEPS = (
    ("dns_ms", "DNS lookup"),
    ("connect_ms", "TCP connect"),
    ("tls_ms", "TLS handshake"),
    ("first_byte_ms", "Waiting for first byte"),
)
#: What the timed steps leave out of the total: sending the request, reading
#: the body, following redirects.
_REST = "Download & other"


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def dashboard_url(path: str = "") -> str:
    """A link into the web UI, or "" when ALERT_DASHBOARD_URL is not set."""
    base = settings.ALERT_DASHBOARD_URL.strip().rstrip("/")
    return f"{base}{path}" if base else ""


def _link(label: str, path: str) -> tuple[str, str] | None:
    url = dashboard_url(path)
    return (label, url) if url else None


def _time_split(result: CheckResult) -> list[tuple[str, int]]:
    """How a check's time divides up: each step that finished, in order, then
    whatever they leave out of the total. Empty when nothing was timed.

    The remainder is only counted when a response came back; for a failed
    check it would be the time spent failing, not downloading.
    """
    t = result.timings
    split = [
        (label, ms) for field, label in _STEPS if (ms := getattr(t, field)) is not None
    ]
    if split and result.status_code is not None and result.response_time_ms is not None:
        rest = result.response_time_ms - sum(ms for _, ms in split)
        if rest > 0:
            split.append((_REST, rest))
    return split


def _slowest_step(result: CheckResult) -> str | None:
    """The step that took longest, e.g. `Waiting for first byte (3920 ms, 93%)`."""
    split = _time_split(result)
    if not split:
        return None
    label, ms = max(split, key=lambda step: step[1])
    total = result.response_time_ms
    if total:
        return f"{label} ({ms} ms, {round(ms * 100 / total)}%)"
    return f"{label} ({ms} ms)"


# ---------------------------------------------------------------------------
# Site events: something about one website
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class SiteEvent(Notification):
    """Base for events about a single website's latest check."""

    website: WebsiteSnapshot
    result: CheckResult
    recipients: tuple[str, ...] = ()
    slack: SlackTarget | None = None

    @property
    def project_id(self) -> int:
        return self.website.project_id

    def describe(self) -> str:
        return f"{self.kind.value} alert for {self.website.url}"

    def _identity_facts(self) -> list[tuple[str, str]]:
        return [
            ("Project", self.website.project_name),
            ("Website", self.website.name),
            ("URL", self.website.url),
        ]

    def _response_facts(self) -> list[tuple[str, str]]:
        r = self.result
        rows: list[tuple[str, str]] = []
        if r.status_code is not None:
            code = f"{r.status_code}"
            if r.reason:
                code += f" {r.reason}"
            rows.append(("HTTP status", code))
        else:
            rows.append(("HTTP status", "no response"))
        if r.response_time_ms is not None:
            rows.append(("Response time", f"{r.response_time_ms} ms"))
        return rows

    def context(self) -> dict[str, str]:
        r = self.result
        return {
            "project": self.website.project_name,
            "website": self.website.name,
            "url": self.website.url,
            "status_code": str(r.status_code) if r.status_code is not None else "no response",
            "response_time": f"{r.response_time_ms} ms" if r.response_time_ms is not None else "—",
            "checked_at": _utc(r.checked_at),
            "summary": r.summary,
            "dashboard_url": dashboard_url(f"/websites/{self.website.id}"),
        }

    def _site_link(self) -> tuple[str, str] | None:
        return _link("Open in Watchly", f"/websites/{self.website.id}")

    def _website_payload(self) -> dict:
        return {
            "id": self.website.id,
            "name": self.website.name,
            "url": self.website.url,
            "expected_status": self.website.expected_status,
            "check_interval_seconds": self.website.check_interval_seconds,
        }

    def _check_payload(self) -> dict:
        r = self.result
        return {
            "checked_at": r.checked_at.isoformat(),
            "is_up": r.is_up,
            "status_code": r.status_code,
            "reason": r.reason,
            "response_time_ms": r.response_time_ms,
            "error": r.error,
            "error_type": r.error_type,
            "final_url": r.final_url,
            "redirected": r.redirected,
            "content_length": r.content_length,
            "headers": r.headers,
            "timings": {
                "dns_ms": r.timings.dns_ms,
                "connect_ms": r.timings.connect_ms,
                "tls_ms": r.timings.tls_ms,
                "first_byte_ms": r.timings.first_byte_ms,
            },
        }


@dataclass(kw_only=True)
class OutageEvent(SiteEvent):
    """Down, still down, or recovered."""

    kind: NotificationKind
    #: Seconds the site has been down (or was down, for a recovery).
    downtime_seconds: float = 0.0
    #: Which alert this is within the current outage, 1-based.
    attempt: int = 1
    max_attempts: int = 1

    @property
    def downtime(self) -> str:
        return format_duration(self.downtime_seconds)

    @property
    def is_recovery(self) -> bool:
        return self.kind is NotificationKind.RECOVERED

    def context(self) -> dict[str, str]:
        r = self.result
        down_since = self.website.down_since
        return {
            **super().context(),
            "status": "UP" if r.is_up else "DOWN",
            "error": r.error or "—",
            "downtime": self.downtime,
            "down_since": _utc(down_since) if down_since else "—",
            "attempt": str(self.attempt),
            "max_attempts": str(self.max_attempts),
        }

    def facts(self) -> list[tuple[str, str]]:
        """Ordered diagnostic rows."""
        r = self.result
        rows = self._identity_facts()
        rows.append(("Status", "UP" if r.is_up else "DOWN"))
        rows.append(("Checked at", _utc(r.checked_at)))
        rows.extend(self._response_facts()[:1])  # the HTTP status line
        if not r.is_up:
            rows.append(("Expected status", str(self.website.expected_status)))
        if r.error:
            rows.append(("Error", r.error))
        if r.error_type:
            rows.append(("Error type", r.error_type))
        if r.response_time_ms is not None:
            rows.append(("Response time", f"{r.response_time_ms} ms"))
        if split := _time_split(r):
            rows.append(("Timing", " · ".join(f"{label} {ms} ms" for label, ms in split)))
        rows.append(("Timeout", f"{self.website.timeout_seconds}s"))
        if r.final_url and r.redirected:
            rows.append(("Redirected to", r.final_url))
        if r.content_length is not None:
            rows.append(("Response size", f"{r.content_length} bytes"))
        if self.downtime_seconds > 0:
            label = "Total downtime" if self.is_recovery else "Down for"
            rows.append((label, self.downtime))
        if self.website.down_since and not self.is_recovery:
            rows.append(("Down since", _utc(self.website.down_since)))
        rows.append(("Consecutive failures", str(self.website.consecutive_failures)))
        rows.append(("Check interval", format_duration(self.website.check_interval_seconds)))
        if not self.is_recovery:
            rows.append(("Alert", f"{self.attempt} of {self.max_attempts}"))
        for name, value in r.headers.items():
            rows.append((f"Header: {name}", value))
        return rows

    def _note(self) -> str:
        if self.kind is NotificationKind.DOWN:
            return (
                f"This site will be re-checked every "
                f"{format_duration(self.website.check_interval_seconds)}. "
                f"You will get up to {self.max_attempts - 1} further alerts while it "
                "stays down, then one when it recovers."
            )
        if self.kind is NotificationKind.STILL_DOWN:
            remaining = self.max_attempts - self.attempt
            if remaining > 0:
                return (
                    f"{remaining} further down-alert(s) will follow, then alerts pause "
                    "until the site recovers."
                )
            return "This is the final down-alert; the next one will be the recovery."
        return "No further alerts until the site goes down again."

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        name = self.website.name
        kicker, title, tone = {
            NotificationKind.DOWN: ("Outage", f"{name} is down", Tone.CRITICAL),
            NotificationKind.STILL_DOWN: (
                "Still down",
                f"{name} is still down",
                Tone.WARNING,
            ),
            NotificationKind.RECOVERED: (
                "Recovered",
                f"{name} is back up",
                Tone.SUCCESS,
            ),
        }[self.kind]
        return Message(
            kind=self.kind,
            tone=tone,
            kicker=kicker,
            title=title,
            subject=subject,
            body=body,
            slack_body=slack_body,
            facts=self.facts(),
            note=self._note(),
            link=self._site_link(),
        )

    def payload(self, subject: str) -> dict:
        return {
            "event": self.kind.value,
            "subject": subject,
            "summary": self.result.summary,
            "website": self._website_payload(),
            "check": self._check_payload(),
            "outage": {
                "downtime_seconds": round(self.downtime_seconds),
                "downtime_human": self.downtime,
                "consecutive_failures": self.website.consecutive_failures,
                "attempt": self.attempt,
                "max_attempts": self.max_attempts,
            },
        }


def _expiry_phrase(remaining: timedelta) -> str:
    """`expires in 5 days` / `expires in less than a day` / `expired 3 days ago`."""
    seconds = remaining.total_seconds()
    if seconds <= 0:
        days = int(-seconds // 86_400)
        return "has expired" if days == 0 else f"expired {plural(days, 'day')} ago"
    days = int(seconds // 86_400)
    if days == 0:
        return "expires in less than a day"
    return f"expires in {plural(days, 'day')}"


@dataclass(kw_only=True)
class SslExpiryEvent(SiteEvent):
    """The site's certificate has crossed a warning threshold."""

    kind: ClassVar[NotificationKind] = NotificationKind.SSL_EXPIRING

    expires_at: datetime
    issuer: str | None = None
    #: The threshold in days this alert is for; 0 means already expired.
    bucket: int = 0

    @property
    def remaining(self) -> timedelta:
        return self.expires_at - self.result.checked_at

    @property
    def expired(self) -> bool:
        return self.remaining.total_seconds() <= 0

    @property
    def days_left(self) -> int:
        return max(int(self.remaining.total_seconds() // 86_400), 0)

    def context(self) -> dict[str, str]:
        return {
            **super().context(),
            "expires_at": _utc(self.expires_at),
            "expiry": _expiry_phrase(self.remaining),
            "days_left": str(self.days_left),
            "issuer": self.issuer or "—",
        }

    def _note(self) -> str:
        if self.expired:
            return (
                "Browsers will warn visitors, and API clients will refuse to connect, "
                "until the certificate is renewed or replaced."
            )
        later = sorted(
            (d for d in settings.SSL_EXPIRY_ALERT_DAYS if d < self.bucket), reverse=True
        )
        if later:
            listed = ", ".join(str(d) for d in later[:-1])
            listed = f"{listed} and {later[-1]}" if listed else str(later[-1])
            follow_up = (
                f"You will be reminded again with {listed} "
                f"{'day' if later == [1] else 'days'} left, and once more if it expires."
            )
        else:
            follow_up = "You will be told once more if it expires."
        return f"Renew the certificate before it expires. {follow_up}"

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        critical = self.expired or self.remaining < timedelta(days=1)
        rows = self._identity_facts()
        rows.append(("Certificate expires", _utc(self.expires_at)))
        time_left = (
            "expired" if self.expired else format_duration(self.remaining.total_seconds())
        )
        rows.append(("Time left", time_left))
        if self.issuer:
            rows.append(("Issuer", self.issuer))
        rows.append(("Checked at", _utc(self.result.checked_at)))
        name = self.website.name
        return Message(
            kind=self.kind,
            tone=Tone.CRITICAL if critical else Tone.WARNING,
            kicker="SSL certificate",
            title=(
                f"SSL certificate for {name} has expired"
                if self.expired
                else f"SSL certificate for {name} is expiring"
            ),
            subject=subject,
            body=body,
            slack_body=slack_body,
            facts=rows,
            note=self._note(),
            link=self._site_link(),
        )

    def payload(self, subject: str) -> dict:
        return {
            "event": self.kind.value,
            "subject": subject,
            "website": self._website_payload(),
            "certificate": {
                "expires_at": self.expires_at.isoformat(),
                "days_left": self.days_left,
                "expired": self.expired,
                "issuer": self.issuer,
            },
        }


@dataclass(kw_only=True)
class SlowResponseEvent(SiteEvent):
    """The site is up but has been slower than its threshold, repeatedly."""

    kind: ClassVar[NotificationKind] = NotificationKind.SLOW_RESPONSE

    threshold_ms: int
    #: Consecutive slow checks that led to this alert.
    slow_checks: int

    def context(self) -> dict[str, str]:
        return {
            **super().context(),
            "threshold": f"{self.threshold_ms} ms",
            "slow_checks": str(self.slow_checks),
            "slowest_step": _slowest_step(self.result) or "—",
        }

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        rows = self._identity_facts()
        rows.extend(self._response_facts()[::-1])  # response time first
        if slowest := _slowest_step(self.result):
            rows.append(("Slowest step", slowest))
            rows.extend((label, f"{ms} ms") for label, ms in _time_split(self.result))
        rows.append(("Threshold", f"{self.threshold_ms} ms"))
        rows.append(("Slow checks in a row", str(self.slow_checks)))
        rows.append(("Checked at", _utc(self.result.checked_at)))
        rows.append(
            ("Check interval", format_duration(self.website.check_interval_seconds))
        )
        cooldown = format_duration(settings.SLOW_ALERT_COOLDOWN_SECONDS)
        return Message(
            kind=self.kind,
            tone=Tone.WARNING,
            kicker="Slow response",
            title=f"{self.website.name} is responding slowly",
            subject=subject,
            body=body,
            slack_body=slack_body,
            facts=rows,
            note=(
                f"The site is still up. Watchly will not repeat this alert for "
                f"{cooldown}, even if it stays slow."
            ),
            link=self._site_link(),
        )

    def payload(self, subject: str) -> dict:
        return {
            "event": self.kind.value,
            "subject": subject,
            "summary": self.result.summary,
            "website": self._website_payload(),
            "check": self._check_payload(),
            "slow": {
                "threshold_ms": self.threshold_ms,
                "consecutive_slow_checks": self.slow_checks,
            },
        }


# ---------------------------------------------------------------------------
# Monthly report: how every site in a project did
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SiteStats:
    """One site's month, computed from its stored checks."""

    website_id: int
    name: str
    url: str
    checks: int
    up_checks: int
    downtime_seconds: float
    #: Outages that overlapped the period, including one carried in from before.
    incidents: int
    longest_outage_seconds: float
    avg_response_ms: int | None
    p95_response_ms: int | None

    @property
    def uptime_percent(self) -> float:
        return self.up_checks / self.checks * 100 if self.checks else 100.0


@dataclass(kw_only=True)
class ReportEvent(Notification):
    kind: ClassVar[NotificationKind] = NotificationKind.MONTHLY_REPORT

    project_id: int
    project_name: str
    #: The month covered: `period_start` inclusive, `period_end` exclusive, UTC.
    period_start: datetime
    period_end: datetime
    sites: list[SiteStats]
    #: Sites in the project with no checks in the period (paused, or new).
    sites_without_data: int = 0
    recipients: tuple[str, ...] = ()
    slack: SlackTarget | None = None

    @property
    def month_label(self) -> str:
        return self.period_start.strftime("%B %Y")

    @property
    def average_uptime(self) -> float | None:
        if not self.sites:
            return None
        return sum(site.uptime_percent for site in self.sites) / len(self.sites)

    @property
    def total_downtime_seconds(self) -> float:
        return sum(site.downtime_seconds for site in self.sites)

    @property
    def incidents(self) -> int:
        return sum(site.incidents for site in self.sites)

    @property
    def _incidents_phrase(self) -> str:
        return "no incidents" if not self.incidents else plural(self.incidents, "incident")

    def describe(self) -> str:
        return f"{self.month_label} report for project {self.project_name!r}"

    def context(self) -> dict[str, str]:
        return {
            "project": self.project_name,
            "month": self.month_label,
            "average_uptime": format_uptime(self.average_uptime),
            "total_downtime": format_duration(self.total_downtime_seconds)
            if self.total_downtime_seconds
            else "none",
            "incidents": self._incidents_phrase,
            "sites": plural(len(self.sites), "site"),
            "dashboard_url": dashboard_url(f"/projects/{self.project_id}"),
        }

    def _note(self) -> str:
        last_day = self.period_end - timedelta(days=1)
        first = self.period_start
        text = (
            "Uptime is the share of checks that succeeded. Downtime runs from a "
            "site's first failed check to its next successful one. Covers "
            f"{first.day} {first:%b %Y} – {last_day.day} {last_day:%b %Y} (UTC)."
        )
        if self.sites_without_data:
            n = self.sites_without_data
            text += (
                f" {plural(n, 'site')} had no checks in this period "
                f"(paused or newly added) and {'is' if n == 1 else 'are'} left out."
            )
        return text

    def compose(self, *, subject: str, body: str, slack_body: str) -> Message:
        ranked = sorted(self.sites, key=lambda s: (s.uptime_percent, s.name.lower()))
        shown = ranked[:REPORT_SITE_LIMIT]
        average = self.average_uptime
        downtime = self.total_downtime_seconds
        return Message(
            kind=self.kind,
            tone=Tone.INFO,
            kicker=f"Monthly report · {self.month_label}",
            title=f"{self.project_name} uptime",
            subject=subject,
            body=body,
            slack_body=slack_body,
            stats=[
                Stat("Average uptime", format_uptime(average), uptime_tone(average)),
                Stat(
                    "Incidents",
                    str(self.incidents),
                    Tone.SUCCESS if not self.incidents else Tone.WARNING,
                ),
                Stat(
                    "Downtime",
                    format_duration(downtime) if downtime else "none",
                    Tone.SUCCESS if not downtime else Tone.WARNING,
                ),
                Stat("Websites", str(len(self.sites))),
            ],
            sites=[
                SiteRow(
                    name=site.name,
                    url=site.url,
                    uptime=format_uptime(site.uptime_percent),
                    uptime_percent=site.uptime_percent,
                    downtime=format_duration(site.downtime_seconds)
                    if site.downtime_seconds
                    else "none",
                    incidents=site.incidents,
                    response=f"{site.avg_response_ms} ms"
                    if site.avg_response_ms is not None
                    else "—",
                    tone=uptime_tone(site.uptime_percent),
                )
                for site in shown
            ],
            sites_omitted=len(ranked) - len(shown),
            note=self._note(),
            link=_link("Open project in Watchly", f"/projects/{self.project_id}"),
        )

    def payload(self, subject: str) -> dict:
        return {
            "event": self.kind.value,
            "subject": subject,
            "project": {"id": self.project_id, "name": self.project_name},
            "period": {
                "start": self.period_start.isoformat(),
                "end": self.period_end.isoformat(),
            },
            "summary": {
                "average_uptime_percent": self.average_uptime,
                "total_downtime_seconds": round(self.total_downtime_seconds),
                "incidents": self.incidents,
                "sites": len(self.sites),
            },
            "sites": [
                {
                    "id": site.website_id,
                    "name": site.name,
                    "url": site.url,
                    "uptime_percent": site.uptime_percent,
                    "checks": site.checks,
                    "downtime_seconds": round(site.downtime_seconds),
                    "incidents": site.incidents,
                    "longest_outage_seconds": round(site.longest_outage_seconds),
                    "avg_response_ms": site.avg_response_ms,
                    "p95_response_ms": site.p95_response_ms,
                }
                for site in self.sites
            ],
        }
