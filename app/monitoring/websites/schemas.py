import enum
import ipaddress
import re
from datetime import datetime
from typing import Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.monitoring.projects.schemas import ProjectMemberRead, SlackSettings
from app.monitoring.websites.models import CheckType, WebsiteEnvironment, WebsiteStatus

HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "OPTIONS"})

#: A ping waits for echo replies, not pages, so its default timeout is short.
PING_TIMEOUT_SECONDS = 2

_HTTP_URL = TypeAdapter(AnyHttpUrl)
#: One DNS label. Underscores are not valid in host names, but turn up in
#: internal ones often enough to allow.
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")


def _blank_to_none(cls, value):
    """A content rule that is only whitespace is no rule at all."""
    return value if value and value.strip() else None


def http_url(value: str) -> str:
    """An http(s) URL, normalized as pydantic does, e.g. with a trailing slash
    after a bare host."""
    try:
        return str(_HTTP_URL.validate_python(value))
    except ValidationError as exc:
        raise PydanticCustomError("url_parsing", exc.errors()[0]["msg"]) from None


def ping_host(value: str) -> str:
    """A host name or IP address to ping: lower case, with no trailing dot, and
    an IPv6 address compressed and out of its brackets."""
    host = value.strip()
    if "/" in host:
        raise PydanticCustomError(
            "ping_host",
            "A ping needs a host name or IP address, not a URL: e.g. 203.0.113.10 "
            "or server.example.com.",
        )
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    host = host.rstrip(".").lower()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            host = ""
    labels = host.split(".")
    # A last label of only digits would make it an IP address, and this is not a valid one.
    if len(host) > 253 or not all(map(_HOST_LABEL.match, labels)) or labels[-1].isdigit():
        raise PydanticCustomError(
            "ping_host",
            "Enter a valid host name or IP address: e.g. 203.0.113.10 or server.example.com.",
        )
    return host


def monitor_target(check_type: CheckType, value: str) -> str:
    """What `url` holds for this kind of check, validated and normalized."""
    return ping_host(value) if check_type is CheckType.PING else http_url(value)


class WebsiteBase(BaseModel):
    # Before `url`, whose validation depends on it.
    check_type: CheckType = Field(
        default=CheckType.HTTP,
        description=(
            "`http` requests `url`; `ping` sends ICMP echo requests to the host "
            "name or IP address in `url`. Fixed once the site is created."
        ),
    )
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(
        min_length=1,
        max_length=2048,
        description=(
            "The http(s) URL to request, or for a ping check the host name or "
            "IP address, e.g. `203.0.113.10` or `server.example.com`."
        ),
    )
    method: str = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=120,
        description=(
            "Per request; for a ping check, how long to wait for each reply "
            f"(default {PING_TIMEOUT_SECONDS})."
        ),
    )
    check_interval_seconds: int = Field(default=300, ge=30, le=86_400)
    max_down_alerts: int = Field(
        default=4,
        ge=1,
        le=50,
        description="Alerts per outage: the immediate one plus follow-ups.",
    )
    is_enabled: bool = True
    retries_on_failure: int = Field(
        default=1,
        ge=0,
        le=3,
        description=(
            "Times a failed check is repeated (CHECK_RETRY_DELAY_SECONDS apart) "
            "before it counts as failed. 0 alerts on the first failure."
        ),
    )
    must_contain: str | None = Field(
        default=None,
        max_length=255,
        description="The response body must contain this text, or the check fails.",
    )
    must_not_contain: str | None = Field(
        default=None,
        max_length=255,
        description="The response body must not contain this text, or the check fails.",
    )
    environment: WebsiteEnvironment | None = Field(
        default=None,
        description="Which deployment this is: development, testing, uat, staging or production.",
    )
    slow_threshold_ms: int | None = Field(
        default=None,
        ge=1,
        le=120_000,
        description=(
            "Alert when successful responses stay slower than this; for a ping "
            "check, the average round trip. Leave null to use the server-wide "
            "SLOW_RESPONSE_THRESHOLD_MS."
        ),
    )
    ping_count: int = Field(
        default=5, ge=1, le=20, description="Ping checks: echo requests sent per check."
    )
    packet_loss_threshold_percent: int | None = Field(
        default=None,
        ge=1,
        le=99,
        description=(
            "Ping checks: alert when the host answers but keeps losing at least "
            "this share of pings. Leave null to use the server-wide "
            "PACKET_LOSS_THRESHOLD_PERCENT."
        ),
    )
    alert_emails: list[EmailStr] = Field(
        default_factory=list,
        description=(
            "Addresses alerted for this site only that are not user accounts, "
            "such as a client contact or a shared inbox."
        ),
    )
    inherit_project_recipients: bool = Field(
        default=True,
        description=(
            "Also email the project's members and extra_emails. Set false so "
            "only this site's own recipients and alert_emails are emailed."
        ),
    )
    slack_channel_id: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "Post this site's alerts to its own channel instead of the "
            "project's. Uses the project's bot token unless slack_bot_token "
            "is also given."
        ),
    )
    slack_bot_token: str | None = Field(
        default=None,
        min_length=8,
        max_length=255,
        description=(
            "The site's own Slack bot token (`xoxb-…`), for a site whose "
            "project has no Slack or that posts to another workspace. Needs "
            "slack_channel_id. Stored encrypted and never returned."
        ),
    )

    @field_validator("url")
    @classmethod
    def _valid_target(cls, value: str, info: ValidationInfo) -> str:
        # Missing when check_type itself failed validation; that error is reported.
        return monitor_target(info.data.get("check_type", CheckType.HTTP), value)

    @field_validator("method")
    @classmethod
    def known_method(cls, value: str) -> str:
        upper = value.upper()
        if upper not in HTTP_METHODS:
            raise ValueError(f"method must be one of {sorted(HTTP_METHODS)}")
        return upper

    @model_validator(mode="after")
    def _ping_timeout(self) -> Self:
        if self.check_type is CheckType.PING and "timeout_seconds" not in self.model_fields_set:
            self.timeout_seconds = PING_TIMEOUT_SECONDS
        return self

    _bot_token = field_validator("slack_bot_token")(
        SlackSettings.looks_like_a_bot_token.__func__
    )
    _channel_id = field_validator("slack_channel_id")(
        SlackSettings.looks_like_a_channel_id.__func__
    )
    _content_rules = field_validator("must_contain", "must_not_contain")(_blank_to_none)


class WebsiteCreate(WebsiteBase):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "project_id": 1,
                "name": "Marketing site",
                "url": "https://www.monstarpeople.com/",
                "check_interval_seconds": 300,
                "recipient_ids": [3, 7],
                "alert_emails": ["client@theirdomain.com"],
            }
        }
    )

    project_id: int = Field(ge=1, description="The project this site belongs to.")
    recipient_ids: list[int] = Field(
        default_factory=list,
        description="Users alerted about this site only, in addition to alert_emails.",
    )


class WebsiteUpdate(BaseModel):
    """Partial update; omitted fields are left alone."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    url: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        description="A URL, or for a ping check a host name or IP address.",
    )
    method: str | None = None
    expected_status: int | None = Field(default=None, ge=100, le=599)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    check_interval_seconds: int | None = Field(default=None, ge=30, le=86_400)
    max_down_alerts: int | None = Field(default=None, ge=1, le=50)
    is_enabled: bool | None = None
    retries_on_failure: int | None = Field(default=None, ge=0, le=3)
    must_contain: str | None = Field(
        default=None, max_length=255, description="Send null to remove the rule."
    )
    must_not_contain: str | None = Field(
        default=None, max_length=255, description="Send null to remove the rule."
    )
    environment: WebsiteEnvironment | None = Field(
        default=None, description="Send null to clear it."
    )
    slow_threshold_ms: int | None = Field(
        default=None,
        ge=1,
        le=120_000,
        description="Send null to go back to the server-wide default.",
    )
    ping_count: int | None = Field(default=None, ge=1, le=20)
    packet_loss_threshold_percent: int | None = Field(
        default=None,
        ge=1,
        le=99,
        description="Send null to go back to the server-wide default.",
    )
    alert_emails: list[EmailStr] | None = Field(
        default=None, description="Replaces the whole list when supplied."
    )
    inherit_project_recipients: bool | None = None
    slack_channel_id: str | None = Field(
        default=None,
        max_length=32,
        description="Send null to remove the site's own Slack, token included.",
    )
    slack_bot_token: str | None = Field(
        default=None,
        min_length=8,
        max_length=255,
        description="Send null to go back to the project's token.",
    )

    _known_method = field_validator("method")(WebsiteBase.known_method.__func__)
    _content_rules = field_validator("must_contain", "must_not_contain")(_blank_to_none)
    _bot_token = field_validator("slack_bot_token")(
        SlackSettings.looks_like_a_bot_token.__func__
    )
    _channel_id = field_validator("slack_channel_id")(
        SlackSettings.looks_like_a_channel_id.__func__
    )


class WebsiteRecipientsUpdate(BaseModel):
    """Add several site recipients in one call."""

    model_config = ConfigDict(json_schema_extra={"example": {"recipient_ids": [3, 7]}})

    recipient_ids: list[int] = Field(min_length=1)


class WebsiteRecipientRead(ProjectMemberRead):
    """A user alerted about this one site."""


class PingCheckRead(BaseModel):
    """What one ping check's echo requests found."""

    address: str | None = Field(
        description="The IP address pinged: the host itself, or what its name resolved to."
    )
    sent: int = Field(description="Echo requests attempted.")
    received: int = Field(description="Requests answered within the timeout.")
    loss_percent: float
    min_ms: float | None = Field(description="Round trips; null when nothing came back.")
    avg_ms: float | None
    max_ms: float | None
    jitter_ms: float | None = Field(
        description="Mean difference between consecutive round trips; null under two replies."
    )


class WebsiteCheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    checked_at: datetime
    is_up: bool
    status_code: int | None
    reason: str | None = None
    response_time_ms: int | None
    error: str | None
    error_type: str | None = Field(
        default=None,
        description=(
            "Why the check failed, e.g. `dns_error`, `connect_timeout`, `tls_error`, "
            "`unexpected_status`, or for a ping `no_reply`; null when it succeeded."
        ),
    )
    final_url: str | None
    headers: dict[str, str] | None = Field(
        default=None, description="Diagnostic response headers, e.g. `server`, `cf-ray`."
    )
    dns_ms: int | None = None
    connect_ms: int | None = None
    tls_ms: int | None = None
    first_byte_ms: int | None = Field(
        default=None,
        description=(
            "From the request being sent to the response headers arriving. This and "
            "the three step times before it are summed over redirects, and null when "
            "the step did not finish (or a reused connection skipped it)."
        ),
    )
    ping: PingCheckRead | None = Field(
        default=None,
        description=(
            "Ping checks: packets and round trips. Null for HTTP checks, and for a "
            "ping that sent nothing (e.g. the name did not resolve). `response_time_ms` "
            "is then the average round trip, rounded."
        ),
    )


class WebsiteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    check_type: CheckType
    name: str
    url: str
    ping_count: int
    packet_loss_threshold_percent: int | None = None
    method: str
    expected_status: int
    timeout_seconds: int
    check_interval_seconds: int
    max_down_alerts: int
    is_enabled: bool
    retries_on_failure: int
    must_contain: str | None = None
    must_not_contain: str | None = None
    environment: WebsiteEnvironment | None = None
    slow_threshold_ms: int | None = None
    recipients: list[WebsiteRecipientRead]
    alert_emails: list[str]
    inherit_project_recipients: bool
    alert_channels: list[str] = Field(
        default_factory=list,
        description="Channels this site's alerts reach, e.g. `[\"email\"]`.",
    )
    slack_channel_id: str | None = None
    slack_token_hint: str | None = Field(
        default=None,
        description=(
            "Masked tail of the site's own bot token, e.g. `xoxb-…9f2a`; null "
            "when the site uses its project's."
        ),
    )
    status: WebsiteStatus
    last_checked_at: datetime | None
    down_since: datetime | None
    consecutive_failures: int
    down_alerts_sent: int
    ssl_expires_at: datetime | None = Field(
        default=None,
        description="When the HTTPS certificate ends; null for plain HTTP or before the first read.",
    )
    created_at: datetime
    updated_at: datetime


class WebsiteListResponse(BaseModel):
    items: list[WebsiteRead]
    total: int
    limit: int
    offset: int


class WebsiteEventSite(BaseModel):
    """Which site an event is about."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    check_type: CheckType
    environment: WebsiteEnvironment | None = None


class WebsiteEventRead(BaseModel):
    """One entry in the alert feed."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    website: WebsiteEventSite
    kind: str = Field(
        description="`down`, `recovered`, `slow_response`, `packet_loss` or `ssl_expiring`."
    )
    occurred_at: datetime
    summary: str = Field(
        description="The check's one-line description, e.g. `HTTP 503 Service Unavailable`."
    )
    response_time_ms: int | None = None
    downtime_seconds: int | None = Field(
        default=None, description="`recovered`: how long the outage lasted."
    )
    threshold_ms: int | None = Field(
        default=None, description="`slow_response`: the threshold the site was slower than."
    )
    ssl_expires_at: datetime | None = Field(
        default=None, description="`ssl_expiring`: when the certificate ends, or ended."
    )


class WebsiteEventList(BaseModel):
    items: list[WebsiteEventRead]
    total: int = Field(description="How many events match; may be more than `items` holds.")
    limit: int


class WebsiteSort(str, enum.Enum):
    ID = "id"
    NAME = "name"
    #: Down sites (that are not paused) first, then by name — the dashboard's order.
    STATUS = "status"


class WebsiteSummary(BaseModel):
    """How many of the sites the caller can see are in each state. The first
    four add up to `total`."""

    total: int
    up: int
    down: int
    unknown: int = Field(description="Enabled, but not checked yet.")
    paused: int


class StatsRange(str, enum.Enum):
    """How far back, and so how the series is bucketed."""

    DAY = "24h"
    WEEK = "7d"
    MONTH = "30d"
    QUARTER = "90d"


class StatsFormat(str, enum.Enum):
    JSON = "json"
    CSV = "csv"


class StatsFigures(BaseModel):
    checks: int
    up_checks: int
    uptime_percent: float | None = Field(
        description="Share of checks that succeeded; null with no checks."
    )
    avg_response_ms: int | None = Field(
        description="Mean response time of successful checks; null with none."
    )
    p95_response_ms: int | None = Field(
        description=(
            "95th percentile of successful checks, read from response-time "
            "buckets: accurate to within about 25%."
        )
    )
    packet_loss_percent: float | None = Field(
        default=None,
        description="Share of pings lost; null when none were sent, as for an HTTP check.",
    )


class StatsBucket(StatsFigures):
    start: datetime


class WebsiteStats(StatsFigures):
    """Uptime and response time over a range, with the series behind a chart."""

    range: StatsRange
    start: datetime = Field(description="Start of the first bucket, UTC.")
    end: datetime
    bucket_seconds: int = Field(description="3600 for 24h and 7d, 86400 for longer.")
    series: list[StatsBucket] = Field(
        description="Oldest first, every bucket present; an empty one has 0 checks."
    )


class CheckNowResponse(BaseModel):
    """Result of an on-demand probe, including any alert it triggered."""

    website: WebsiteRead
    check: WebsiteCheckRead
    alert_sent: str | None = Field(
        default=None, description=(
            "Which notification the check raised, if any: down, still_down, "
            "recovered, ssl_expiring, slow_response or packet_loss."
        )
    )
