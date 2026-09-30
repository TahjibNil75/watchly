import enum
import ipaddress
import re
from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import (
    AliasChoices,
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

from app.monitoring.projects.schemas import (
    MAX_WHATSAPP_RECIPIENTS,
    ProjectMemberRead,
    SlackSettings,
    TelegramSettings,
    WhatsAppSettings,
)
from app.monitoring.websites.models import (
    CheckType,
    DnsRecordType,
    WebsiteEnvironment,
    WebsiteStatus,
)

HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "OPTIONS"})

#: A ping waits for echo replies, not pages, so its default timeout is short.
PING_TIMEOUT_SECONDS = 2
#: A resolver may have to recurse through slow name servers on a cache miss,
#: so a DNS check waits longer than a ping, but not as long as for a page.
DNS_TIMEOUT_SECONDS = 5
#: Each check type's timeout when none is given; HTTP keeps the field default.
_DEFAULT_TIMEOUTS = {CheckType.PING: PING_TIMEOUT_SECONDS, CheckType.DNS: DNS_TIMEOUT_SECONDS}
#: Values a DNS check may pin; a record set rarely runs longer.
MAX_DNS_VALUES = 20
#: One pinned value; room for a DKIM key, which runs to several hundred.
_DnsValue = Annotated[str, Field(max_length=4096)]

_HTTP_URL = TypeAdapter(AnyHttpUrl)
#: One DNS label. Underscores are not valid in host names, but turn up in
#: internal ones often enough to allow.
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")
#: A TXT value pasted as zone-file strings: `"v=spf1 …" "…"`.
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _blank_to_none(cls, value):
    """A content rule that is only whitespace is no rule at all."""
    return value if value and value.strip() else None


#: Headers one site may add to its requests.
MAX_REQUEST_HEADERS = 10
#: A header name: an HTTP token (RFC 9110 §5.6.2).
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
#: Headers the HTTP client works out itself, or that belong to one connection.
_CLIENT_HEADERS = frozenset(
    {"connection", "content-length", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}
)


class RequestHeader(BaseModel):
    """One header an HTTP check sends with every request."""

    name: str = Field(min_length=1, max_length=100, examples=["Authorization"])
    value: str = Field(max_length=1024, description="Stored encrypted and never returned.")

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        name = value.strip()
        if not _HEADER_NAME.match(name):
            raise ValueError(f"'{name}' is not a valid header name")
        if name.lower() in _CLIENT_HEADERS:
            raise ValueError(f"{name} is set by the HTTP client and cannot be given")
        return name

    @field_validator("value")
    @classmethod
    def _valid_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        # A line break would start a header, or a body, of the caller's choosing.
        if any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in value):
            raise ValueError("a header value cannot contain line breaks or control characters")
        return value


class RequestHeaderUpdate(RequestHeader):
    value: str | None = Field(
        default=None,
        max_length=1024,
        description="Null keeps the value already stored under this name.",
    )


def unique_header_names(cls, headers: list[RequestHeader] | None):
    """Header names compare without case, so each may be given once."""
    seen: set[str] = set()
    for header in headers or []:
        if header.name.lower() in seen:
            raise ValueError(f"the {header.name} header is given twice")
        seen.add(header.name.lower())
    return headers


class RequestHeaderRead(BaseModel):
    name: str
    value_hint: str | None = Field(
        default=None,
        description="Masked tail of the stored value, e.g. `…9f2a`; null when it is empty or unreadable.",
    )


def http_url(value: str) -> str:
    """An http(s) URL, normalized as pydantic does, e.g. with a trailing slash
    after a bare host."""
    try:
        return str(_HTTP_URL.validate_python(value))
    except ValidationError as exc:
        raise PydanticCustomError("url_parsing", exc.errors()[0]["msg"]) from None


def _host_name(value: str) -> str | None:
    """A host name in lower case, with no trailing dot and an IDN in its ASCII
    form; None when `value` is not a valid one."""
    host = value.strip().rstrip(".").lower()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    labels = host.split(".")
    # A last label of only digits would make it an IP address, and this is not a valid one.
    if len(host) > 253 or not all(map(_HOST_LABEL.match, labels)) or labels[-1].isdigit():
        return None
    return host


def _ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """`value` as an IP address, an IPv6 one possibly in brackets; else None."""
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


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
    if (address := _ip_address(host)) is not None:
        return address.compressed
    if (name := _host_name(host)) is None:
        raise PydanticCustomError(
            "ping_host",
            "Enter a valid host name or IP address: e.g. 203.0.113.10 or server.example.com.",
        )
    return name


def dns_name(value: str) -> str:
    """A domain name to look up, e.g. `example.com` or `_dmarc.example.com`:
    lower case, with no trailing dot."""
    name = value.strip()
    if "/" in name:
        raise PydanticCustomError(
            "dns_name", "A DNS check needs a domain name, not a URL: e.g. example.com."
        )
    if _ip_address(name) is not None:
        raise PydanticCustomError(
            "dns_name",
            "A DNS check needs a domain name, not an IP address: e.g. example.com.",
        )
    if (host := _host_name(name)) is None:
        raise PydanticCustomError(
            "dns_name",
            "Enter a valid domain name: e.g. example.com or _dmarc.example.com.",
        )
    return host


_TARGETS = {CheckType.HTTP: http_url, CheckType.PING: ping_host, CheckType.DNS: dns_name}


def monitor_target(check_type: CheckType, value: str) -> str:
    """What `url` holds for this kind of check, validated and normalized."""
    return _TARGETS[check_type](value)


def _not_a(record_type: DnsRecordType, value: str, what: str) -> PydanticCustomError:
    return PydanticCustomError(
        "dns_value",
        "{record_type} value '{value}' is not {what}.",
        {"record_type": record_type.value, "value": value, "what": what},
    )


def dns_value(record_type: DnsRecordType, value: str) -> str:
    """One expected value, written as `dns_probe.record_text` writes a record
    a resolver returned, so the two compare as strings."""
    text = value.strip()
    match record_type:
        case DnsRecordType.A | DnsRecordType.AAAA:
            version = 4 if record_type is DnsRecordType.A else 6
            address = _ip_address(text)
            if address is None or address.version != version:
                raise _not_a(record_type, text, f"an IPv{version} address")
            return address.compressed
        case DnsRecordType.CNAME:
            if (host := _host_name(text)) is None:
                raise _not_a(record_type, text, "a host name, e.g. example.net")
            return host
        case DnsRecordType.MX:
            parts = text.split()
            # "0 ." is a null MX (RFC 7505): the domain takes no mail.
            host = parts[1] if len(parts) == 2 and parts[1] == "." else None
            if len(parts) == 2 and host is None:
                host = _host_name(parts[1])
            if host is None or not parts[0].isdigit() or int(parts[0]) > 65_535:
                raise _not_a(record_type, text, "a preference and a host, e.g. 10 mx1.example.com")
            return f"{int(parts[0])} {host}"
        case DnsRecordType.TXT:
            # Pasted from a zone file or `dig`: one or more quoted strings,
            # which a resolver hands back joined into one value.
            strings = _QUOTED.findall(text)
            if strings and not _QUOTED.sub("", text).strip():
                text = "".join(re.sub(r"\\(.)", r"\1", part) for part in strings)
            if not text:
                raise PydanticCustomError("dns_value", "A TXT value cannot be empty.")
            return text
    raise PydanticCustomError("dns_value", "Unsupported record type.")


def dns_values(record_type: DnsRecordType, values: list[str]) -> list[str]:
    """Expected values, normalized, without blanks or repeats, and sorted as
    `dns_probe` sorts the records a resolver returns."""
    normalized = sorted({dns_value(record_type, value) for value in values if value.strip()})
    if record_type is DnsRecordType.CNAME and len(normalized) > 1:
        raise PydanticCustomError(
            "dns_value", "A name has at most one CNAME record: give a single value."
        )
    return normalized


NAME_MAX_WORDS = 25


def check_name_length(value: str | None) -> str | None:
    """A site name runs to at most 25 words."""
    if value is not None and len(value.split()) > NAME_MAX_WORDS:
        raise ValueError(f"name must be at most {NAME_MAX_WORDS} words")
    return value


class WebsiteBase(BaseModel):
    # Before `url`, whose validation depends on it.
    check_type: CheckType = Field(
        default=CheckType.HTTP,
        description=(
            "`http` requests `url`; `ping` sends ICMP echo requests to the host "
            "name or IP address in `url`; `dns` looks up one record of the domain "
            "in `url` at several resolvers. Fixed once the site is created."
        ),
    )
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(
        min_length=1,
        max_length=2048,
        description=(
            "The http(s) URL to request; for a ping check the host name or IP "
            "address, e.g. `203.0.113.10` or `server.example.com`; for a DNS "
            "check the domain name, e.g. `example.com` or `_dmarc.example.com`."
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
            f"(default {PING_TIMEOUT_SECONDS}); for a DNS check, for each "
            f"resolver's answer (default {DNS_TIMEOUT_SECONDS})."
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
    request_headers: list[RequestHeader] = Field(
        default_factory=list,
        max_length=MAX_REQUEST_HEADERS,
        description=(
            "HTTP checks: headers sent with every request, e.g. `Authorization` "
            "for a page behind a login, or `User-Agent` in place of Watchly's. "
            "Values are stored encrypted and never returned. Ignored for other "
            "check types."
        ),
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
            "check, the average round trip; for a DNS check, the resolvers' "
            "average answer time. Leave null to use the server-wide "
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
    dns_record_type: DnsRecordType | None = Field(
        default=None,
        description=(
            "DNS checks: the record to look up, `A` when omitted. Ignored for other "
            "check types."
        ),
    )
    dns_expected_values: list[_DnsValue] = Field(
        default_factory=list,
        max_length=MAX_DNS_VALUES,
        description=(
            "DNS checks: the records every resolver must return, e.g. `203.0.113.10` "
            "for A, `10 mx1.example.com` for MX, or a TXT value; any other answer "
            "is an outage. Leave empty to have the check learn the records and "
            "alert when they change."
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
    telegram_chat_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Send this site's alerts to its own Telegram chat instead of the "
            "project's. Uses the project's bot unless telegram_bot_token is "
            "also given."
        ),
    )
    telegram_bot_token: str | None = Field(
        default=None,
        min_length=20,
        max_length=255,
        description=(
            "The site's own Telegram bot token (`123456789:AA…`), for a site "
            "whose project has no Telegram or that uses another bot. Needs "
            "telegram_chat_id. Stored encrypted and never returned."
        ),
    )
    whatsapp_recipients: list[str] = Field(
        default_factory=list,
        max_length=MAX_WHATSAPP_RECIPIENTS,
        description=(
            "WhatsApp this site's alerts to these numbers (with country code, "
            "such as `+8801712345678`) instead of the project's. They are sent "
            "from the project's business number, so the project needs WhatsApp."
        ),
    )

    _name_length = field_validator("name")(check_name_length)

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
    def _per_check_type(self) -> Self:
        default = _DEFAULT_TIMEOUTS.get(self.check_type)
        if default and "timeout_seconds" not in self.model_fields_set:
            self.timeout_seconds = default
        if self.check_type is CheckType.DNS:
            self.dns_record_type = self.dns_record_type or DnsRecordType.A
            self.dns_expected_values = dns_values(self.dns_record_type, self.dns_expected_values)
        else:
            # Only a DNS check has a record type; one on another type would
            # also slip it past the one-check-per-target rule.
            self.dns_record_type = None
            self.dns_expected_values = []
        if self.check_type is not CheckType.HTTP:
            self.request_headers = []
        return self

    _bot_token = field_validator("slack_bot_token")(
        SlackSettings.looks_like_a_bot_token.__func__
    )
    _channel_id = field_validator("slack_channel_id")(
        SlackSettings.looks_like_a_channel_id.__func__
    )
    _telegram_token = field_validator("telegram_bot_token")(
        TelegramSettings.looks_like_a_telegram_token.__func__
    )
    _chat_id = field_validator("telegram_chat_id")(
        TelegramSettings.looks_like_a_chat_id.__func__
    )
    _whatsapp_recipients = field_validator("whatsapp_recipients")(
        WhatsAppSettings.looks_like_phone_numbers.__func__
    )
    _content_rules = field_validator("must_contain", "must_not_contain")(_blank_to_none)
    _header_names = field_validator("request_headers")(unique_header_names)


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
        description=(
            "A URL; for a ping check a host name or IP address; for a DNS check a "
            "domain name."
        ),
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
    request_headers: list[RequestHeaderUpdate] | None = Field(
        default=None,
        max_length=MAX_REQUEST_HEADERS,
        description=(
            "HTTP checks only. Replaces the whole list; send [] to remove them "
            "all. A header sent with `value: null` keeps its stored value."
        ),
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
    dns_record_type: DnsRecordType | None = Field(
        default=None,
        description=(
            "DNS checks only. Changing it starts learning the records afresh, and "
            "clears dns_expected_values unless new ones are sent."
        ),
    )
    dns_expected_values: list[_DnsValue] | None = Field(
        default=None,
        max_length=MAX_DNS_VALUES,
        description="DNS checks only. Replaces the whole list; send [] to unpin.",
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
    telegram_chat_id: str | None = Field(
        default=None,
        max_length=64,
        description="Send null to remove the site's own Telegram, token included.",
    )
    telegram_bot_token: str | None = Field(
        default=None,
        min_length=20,
        max_length=255,
        description="Send null to go back to the project's bot.",
    )
    whatsapp_recipients: list[str] | None = Field(
        default=None,
        max_length=MAX_WHATSAPP_RECIPIENTS,
        description=(
            "Replaces the whole list; send [] (or null) to go back to the "
            "project's numbers."
        ),
    )

    _name_length = field_validator("name")(check_name_length)
    _known_method = field_validator("method")(WebsiteBase.known_method.__func__)
    _content_rules = field_validator("must_contain", "must_not_contain")(_blank_to_none)
    _header_names = field_validator("request_headers")(unique_header_names)
    _bot_token = field_validator("slack_bot_token")(
        SlackSettings.looks_like_a_bot_token.__func__
    )
    _channel_id = field_validator("slack_channel_id")(
        SlackSettings.looks_like_a_channel_id.__func__
    )
    _telegram_token = field_validator("telegram_bot_token")(
        TelegramSettings.looks_like_a_telegram_token.__func__
    )
    _chat_id = field_validator("telegram_chat_id")(
        TelegramSettings.looks_like_a_chat_id.__func__
    )
    _whatsapp_recipients = field_validator("whatsapp_recipients")(
        WhatsAppSettings.looks_like_phone_numbers.__func__
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


class DnsAnswerRead(BaseModel):
    """What one resolver answered."""

    resolver: str = Field(description="The resolver's name, e.g. `Cloudflare`.")
    address: str = Field(description="The resolver's IP address.")
    records: list[str] | None = Field(
        description="The records it returned, sorted; null when it returned none."
    )
    ttl: int | None = Field(description="Seconds it may serve these from its cache.")
    time_ms: int | None = Field(description="How long it took to answer; null when it did not.")
    error: str | None = None
    error_type: str | None = Field(
        default=None,
        description=(
            "`nxdomain`, `no_records`, `servfail`, `refused`, `timeout`, "
            "`network_error` or `dns_error`; null when it returned the record."
        ),
    )


class DnsCheckRead(BaseModel):
    """What one DNS check's resolvers answered."""

    record_type: DnsRecordType
    expected: list[str] = Field(description="The values pinned at the time; empty when none were.")
    records: list[str] | None = Field(
        description=(
            "The records the resolvers agreed on; null when they disagreed or "
            "fewer than half returned the record."
        )
    )
    consistent: bool = Field(
        description=(
            "Every resolver that returned the record returned the same set, and "
            "none said there is none. Timeouts and failures do not count against it."
        )
    )
    answers: list[DnsAnswerRead]


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
            "`unexpected_status`, for a ping `no_reply`, or for a DNS check "
            "`nxdomain` or `dns_mismatch`; null when it succeeded."
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
    dns: DnsCheckRead | None = Field(
        default=None,
        description=(
            "DNS checks: each resolver's answer. `response_time_ms` is then the "
            "resolvers' average answer time."
        ),
    )


#: The longest a maintenance window may last; pause a site for longer.
MAX_MAINTENANCE_MINUTES = 7 * 24 * 60


class MaintenanceCreate(BaseModel):
    """Start maintenance now, or schedule it. Give `ends_at` or
    `duration_minutes`, not both."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"duration_minutes": 30, "reason": "Deploying 2.4"},
                {
                    "starts_at": "2026-10-01T22:00:00Z",
                    "ends_at": "2026-10-01T23:30:00Z",
                    "reason": "Database upgrade",
                },
            ]
        }
    )

    starts_at: datetime | None = Field(
        default=None,
        description=(
            "When it begins. Omit it to begin now; a time already past also "
            "begins now. Without a timezone it is read as UTC."
        ),
    )
    ends_at: datetime | None = Field(
        default=None, description="When it ends. Without a timezone it is read as UTC."
    )
    duration_minutes: int | None = Field(
        default=None,
        ge=1,
        le=MAX_MAINTENANCE_MINUTES,
        description="How long it lasts from `starts_at`, instead of `ends_at`.",
    )
    reason: str | None = Field(
        default=None, max_length=255, description="What is going on, e.g. `Deploying 2.4`."
    )

    @field_validator("reason")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @model_validator(mode="after")
    def _one_end(self) -> Self:
        if (self.ends_at is None) == (self.duration_minutes is None):
            raise ValueError("give either ends_at or duration_minutes")
        return self


class MaintenanceWindowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    starts_at: datetime
    ends_at: datetime
    reason: str | None = None
    created_by_id: int | None = None


class SecurityHeaderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str = Field(description="`hsts`, `csp`, `frame`, `nosniff` or `referrer`.")
    header: str = Field(description="The header's name, e.g. `Strict-Transport-Security`.")
    status: str = Field(description="`ok`, `weak` (sent, but protects little) or `missing`.")
    value: str | None = Field(default=None, description="What the site sent, if anything.")
    note: str | None = Field(default=None, description="Why it is weak or missing.")


class SecurityHeadersRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    url: str = Field(description="The response graded: the site's URL after redirects.")
    score: int = Field(description="How many of the headers are ok.")
    total: int
    grade: str = Field(description="`A` for all five ok, then `B`, `C`, `D`, and `F`.")
    items: list[SecurityHeaderRead]


class WebsiteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    check_type: CheckType
    name: str
    url: str
    ping_count: int
    packet_loss_threshold_percent: int | None = None
    dns_record_type: DnsRecordType | None = None
    dns_expected_values: list[str] = Field(default_factory=list)
    dns_records: list[str] | None = Field(
        default=None,
        description=(
            "DNS checks: the records the resolvers last agreed on, what a change "
            "is measured against; null until they first agree."
        ),
    )
    method: str
    expected_status: int
    timeout_seconds: int
    check_interval_seconds: int
    max_down_alerts: int
    is_enabled: bool
    retries_on_failure: int
    must_contain: str | None = None
    must_not_contain: str | None = None
    request_headers: list[RequestHeaderRead] = Field(
        default_factory=list,
        validation_alias="request_header_hints",
        description="The headers sent with every request; values masked.",
    )
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
    telegram_chat_id: str | None = None
    telegram_token_hint: str | None = Field(
        default=None,
        description=(
            "Masked site's own Telegram bot token, e.g. `123456789:…wxyz`; null "
            "when the site uses its project's."
        ),
    )
    whatsapp_recipients: list[str] = Field(
        default_factory=list,
        description="The site's own WhatsApp numbers; empty when it uses its project's.",
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
    ssl_valid_from: datetime | None = Field(
        default=None, description="When the certificate began."
    )
    ssl_subject: str | None = Field(
        default=None,
        description="Who the certificate was issued to: its common name, else its first SAN.",
    )
    ssl_issuer: str | None = Field(
        default=None, description="Who issued it, e.g. `Let's Encrypt`."
    )
    ssl_sans: list[str] | None = Field(
        default=None,
        description="The names it covers (subject alternative names), up to 100.",
    )
    ssl_tls_version: str | None = Field(
        default=None, description="The TLS version of the last read, e.g. `TLSv1.3`."
    )
    ssl_checked_at: datetime | None = Field(
        default=None, description="When the certificate was last read, or tried to be."
    )
    domain_name: str | None = Field(
        default=None,
        description=(
            "The registered domain the host belongs to, e.g. `example.co.uk`; null "
            "for an IP address or before the first lookup."
        ),
    )
    domain_expires_at: datetime | None = Field(
        default=None, description="When the domain registration ends, as its registry says."
    )
    domain_registrar: str | None = Field(
        default=None, description="Who the domain is registered with."
    )
    domain_checked_at: datetime | None = Field(
        default=None, description="When the domain was last looked up, or tried to be."
    )
    domain_error: str | None = Field(
        default=None,
        description=(
            "Why the last lookup told nothing new, e.g. a registry without RDAP; "
            "what earlier ones learned is kept."
        ),
    )
    domain_nameservers: list[str] | None = Field(
        default=None,
        description=(
            "The nameservers the registry delegates the domain to, sorted; null "
            "until it first names any."
        ),
    )
    domain_nameservers_changed_at: datetime | None = Field(
        default=None, description="When this site last saw the nameservers change."
    )
    security: SecurityHeadersRead | None = Field(
        default=None,
        # The model property, or the field itself when FastAPI re-validates a dump.
        validation_alias=AliasChoices("security_report", "security"),
        description=(
            "The security headers of the last HTTP check that came up, graded; null "
            "before one has, and for ping and DNS checks."
        ),
    )
    security_checked_at: datetime | None = Field(
        default=None, description="When the security headers were last read."
    )
    maintenance: MaintenanceWindowRead | None = Field(
        default=None,
        description=(
            "The maintenance window in effect now, if any: the site is not "
            "checked and raises no alerts until it ends."
        ),
    )
    upcoming_maintenance: list[MaintenanceWindowRead] = Field(
        default_factory=list, description="Maintenance scheduled ahead, soonest first."
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
        description=(
            "`down`, `recovered`, `slow_response`, `packet_loss`, `dns_changed`, "
            "`ssl_expiring`, `domain_expiring` or `nameservers_changed`."
        )
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
    domain_expires_at: datetime | None = Field(
        default=None,
        description="`domain_expiring`: when the domain registration ends, or ended.",
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
    """How many of the sites the caller can see are in each state. Up, down,
    unknown, maintenance and paused add up to `total`."""

    total: int
    up: int
    down: int
    unknown: int = Field(description="Enabled, but not checked yet.")
    maintenance: int = Field(
        default=0,
        description="Enabled, and in a maintenance window: whatever its status, not checked now.",
    )
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
            "recovered, ssl_expiring, domain_expiring, nameservers_changed, "
            "slow_response, packet_loss or dns_changed."
        )
    )
