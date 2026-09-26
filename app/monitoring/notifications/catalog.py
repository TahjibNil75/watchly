"""What can be customized about each notification kind, and the defaults.

The single source of truth for the API and the UI: which kinds exist, what a
template may reference, and the wording used when nobody has changed it. The
default subjects deliberately match what Watchly sent before templates existed.
"""

from dataclasses import dataclass

from app.monitoring.alerts.base import NotificationKind

_SITE = {
    "project": "Project name",
    "website": "Website name",
    "url": "Website URL, the host of a ping check, or the domain of a DNS check",
    "status_code": "HTTP status of the latest check, “no response”, or “—” for a ping or "
    "DNS check",
    "response_time": "Response time of the latest check, e.g. “212 ms”; for a ping check "
    "the average round trip, for a DNS check the resolvers’ average answer time",
    "checked_at": "When the latest check ran (UTC)",
    "summary": "One-line result of the latest check, e.g. “HTTP 503 Service Unavailable”",
    "dashboard_url": "Link to this website in Watchly (empty if ALERT_DASHBOARD_URL is unset)",
}

_OUTAGE = {
    **_SITE,
    "status": "UP or DOWN",
    "error": "Error text from the latest check, or “—”",
    "downtime": "How long the site has been down, e.g. “1h 2m”",
    "down_since": "When the outage began (UTC)",
    "attempt": "Which alert this is within the outage",
    "max_attempts": "How many alerts an outage may send",
}

_SSL = {
    **_SITE,
    "expires_at": "When the certificate ends (UTC)",
    "expiry": "“expires in 5 days”, “expires in less than a day” or “expired 2 days ago”",
    "days_left": "Whole days remaining (0 when expired or under a day)",
    "issuer": "Who issued the certificate, or “—”",
}

_SLOW = {
    **_SITE,
    "threshold": "The slow-response threshold, e.g. “3000 ms”",
    "slow_checks": "Consecutive slow checks that triggered this alert",
    "slowest_step": "Which step of the latest check took longest, e.g. “Waiting for "
    "first byte (3920 ms, 93%)”, or “—” if it was not timed (always for a ping check)",
}

_LOSS = {
    **_SITE,
    "packet_loss": "Share of pings the latest check lost, e.g. “40%”",
    "threshold": "The packet-loss threshold, e.g. “20%”",
    "lossy_checks": "Consecutive lossy checks that triggered this alert",
}

_DNS_CHANGE = {
    **_SITE,
    "record_type": "The record watched: A, AAAA, CNAME, MX or TXT",
    "records": "The records every resolver returns now, e.g. “198.51.100.7”",
    "previous_records": "The records they returned before",
    "added": "Records that are new, or “—”",
    "removed": "Records that are gone, or “—”",
}

_REPORT = {
    "project": "Project name",
    "month": "The month covered, e.g. “August 2026”",
    "average_uptime": "Average uptime across the project’s websites, e.g. “99.95%”",
    "total_downtime": "Downtime summed across websites, or “none”",
    "incidents": "“no incidents”, “1 incident” or “3 incidents”",
    "sites": "“1 site” or “4 sites”",
    "dashboard_url": "Link to this project in Watchly (empty if ALERT_DASHBOARD_URL is unset)",
}


@dataclass(frozen=True, slots=True)
class KindInfo:
    kind: NotificationKind
    label: str
    description: str
    #: Who receives it, in words.
    audience: str
    default_subject: str
    default_body: str
    #: `{{name}}` -> what it becomes. Exactly the keys the event's `context()`
    #: returns; `samples.py` and a check in the API keep the two in step.
    placeholders: dict[str, str]


CATALOG: dict[NotificationKind, KindInfo] = {
    info.kind: info
    for info in (
        KindInfo(
            kind=NotificationKind.DOWN,
            label="Site down",
            description="The first failed check of an outage.",
            audience="The website’s recipients (project members and extra emails, site "
            "recipients) and the project’s Slack channel.",
            default_subject="[DOWN] {{project}} / {{website}} is not responding",
            default_body="{{website}} did not respond as expected: {{summary}}",
            placeholders=_OUTAGE,
        ),
        KindInfo(
            kind=NotificationKind.STILL_DOWN,
            label="Still down",
            description="Follow-up reminders while a site stays down, up to the "
            "website’s alert limit.",
            audience="Same as “Site down”.",
            default_subject="[STILL DOWN] {{project}} / {{website}} has been down for {{downtime}}",
            default_body="{{website}} is still not responding after {{downtime}}: {{summary}}",
            placeholders=_OUTAGE,
        ),
        KindInfo(
            kind=NotificationKind.RECOVERED,
            label="Back up",
            description="The site answered again after an outage.",
            audience="Same as “Site down”.",
            default_subject="[RECOVERED] {{project}} / {{website}} is back up after {{downtime}}",
            default_body="{{website}} is responding again after {{downtime}} of downtime.",
            placeholders=_OUTAGE,
        ),
        KindInfo(
            kind=NotificationKind.SSL_EXPIRING,
            label="SSL certificate expiring",
            description="An HTTPS site’s certificate is close to its end date, at 14, 7, "
            "3 and 1 days (configurable) and once more if it expires.",
            audience="Same as “Site down”.",
            default_subject="[SSL] {{project}} / {{website}}: certificate {{expiry}}",
            default_body=(
                "The SSL certificate for {{website}} {{expiry}} ({{expires_at}}). "
                "Renew it promptly to avoid browser warnings and failed connections."
            ),
            placeholders=_SSL,
        ),
        KindInfo(
            kind=NotificationKind.SLOW_RESPONSE,
            label="Slow response",
            description="The site is up but has answered slower than its threshold for "
            "several checks in a row.",
            audience="Same as “Site down”.",
            default_subject="[SLOW] {{project}} / {{website}} is responding slowly ({{response_time}})",
            default_body=(
                "{{website}} has taken longer than {{threshold}} to respond for "
                "{{slow_checks}} checks in a row; the latest took {{response_time}}. "
                "Slowest step: {{slowest_step}}. It is still up."
            ),
            placeholders=_SLOW,
        ),
        KindInfo(
            kind=NotificationKind.PACKET_LOSS,
            label="Packet loss",
            description="A pinged host still answers, but has lost at least its threshold "
            "of pings for several checks in a row.",
            audience="Same as “Site down”.",
            default_subject="[PACKET LOSS] {{project}} / {{website}} is losing {{packet_loss}} of pings",
            default_body=(
                "{{website}} has lost at least {{threshold}} of its pings for "
                "{{lossy_checks}} checks in a row; the latest lost {{packet_loss}}, and "
                "the replies that came back took {{response_time}} on average. It is "
                "still up."
            ),
            placeholders=_LOSS,
        ),
        KindInfo(
            kind=NotificationKind.DNS_CHANGED,
            label="DNS records changed",
            description="A DNS check with no expected values: every resolver now returns "
            "different records than before. (With expected values, a different answer "
            "is an outage instead.)",
            audience="Same as “Site down”.",
            default_subject="[DNS CHANGED] {{project}} / {{website}}: {{record_type}} records "
            "for {{url}} changed",
            default_body=(
                "The {{record_type}} records for {{url}} are now {{records}} (were "
                "{{previous_records}}). If you did not make this change, check your DNS "
                "provider and registrar accounts."
            ),
            placeholders=_DNS_CHANGE,
        ),
        KindInfo(
            kind=NotificationKind.MONTHLY_REPORT,
            label="Monthly uptime report",
            description="How every site in the project did last month: uptime, downtime, "
            "incidents and response time.",
            audience="The project’s members and extra emails, ALERT_DEFAULT_EMAILS, and "
            "the project’s Slack channel.",
            default_subject="{{project}}: {{month}} uptime report ({{average_uptime}})",
            default_body=(
                "Here is how {{sites}} in {{project}} performed in {{month}}: "
                "{{average_uptime}} average uptime with {{incidents}}."
            ),
            placeholders=_REPORT,
        ),
    )
}

#: Kinds in the order the UI lists them.
KINDS: tuple[NotificationKind, ...] = tuple(CATALOG)

#: The one kind that may not be silenced on every channel: without it, a
#: project would go down without anyone being told.
PROTECTED_KIND = NotificationKind.DOWN
