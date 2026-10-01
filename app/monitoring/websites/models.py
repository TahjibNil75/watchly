import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    Exists,
    Float,
    ForeignKey,
    Index,
    Integer,
    Select,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    desc,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.crypto import decrypt_secret, mask_secret
from app.db.base import Base, TimestampMixin

# `websites.created_by_id` targets `users.id`, so that table must be registered
# on Base.metadata whenever this module is imported — otherwise resolving the
# foreign key raises NoReferencedTableError.
from app.db.models.user import User

# `websites.project_id` targets `projects.id`; importing the module registers
# that table and resolves the Project relationship by name.
from app.monitoring.projects.models import Project  # noqa: F401
from app.monitoring.websites.security_headers import SecurityReport
from app.monitoring.websites.security_headers import grade as grade_security_headers


class WebsiteStatus(str, enum.Enum):
    #: Never checked yet.
    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"


website_status_enum = SAEnum(
    WebsiteStatus,
    name="website_status",
    native_enum=True,
    create_constraint=False,
    validate_strings=True,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class CheckType(str, enum.Enum):
    """How a site is checked. Fixed when it is created."""

    #: Request `url` and judge the response.
    HTTP = "http"
    #: Send ICMP echo requests to the host in `url` and count the replies.
    PING = "ping"
    #: Ask several DNS resolvers for one record of the domain in `url`.
    DNS = "dns"


check_type_enum = SAEnum(
    CheckType,
    name="check_type",
    native_enum=True,
    create_constraint=False,
    validate_strings=True,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class DnsRecordType(str, enum.Enum):
    """The records a DNS check can watch."""

    A = "A"
    AAAA = "AAAA"
    CNAME = "CNAME"
    MX = "MX"
    TXT = "TXT"


dns_record_type_enum = SAEnum(
    DnsRecordType,
    name="dns_record_type",
    native_enum=True,
    create_constraint=False,
    validate_strings=True,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class WebsiteEnvironment(str, enum.Enum):
    """Which deployment of a project a site is. Declared in the order the UI's
    dropdown lists them, from least to most critical."""

    DEVELOPMENT = "development"
    TESTING = "testing"
    UAT = "uat"
    STAGING = "staging"
    PRODUCTION = "production"


website_environment_enum = SAEnum(
    WebsiteEnvironment,
    name="website_environment",
    native_enum=True,
    create_constraint=False,
    validate_strings=True,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


#: Users alerted about one site only — on top of the project's recipients, or
#: instead of them when the site's `inherit_project_recipients` is off.
website_recipients = Table(
    "website_recipients",
    Base.metadata,
    Column(
        "website_id",
        ForeignKey("websites.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "user_id",
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "added_at",
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    ),
)


def recipient_website_ids(user_id: int) -> Select:
    """Subquery of the website ids `user_id` is a site recipient of.

    A recipient can see the site they are alerted about even when they are not
    a member of its project.
    """
    return select(website_recipients.c.website_id).where(
        website_recipients.c.user_id == user_id
    )


class Website(Base, TimestampMixin):
    """A site to poll, together with the live state of its current outage.

    Also a host to ping (`check_type` ping), which keeps its host name or IP
    address in `url` and ignores the HTTP-only settings; or one DNS record of a
    domain (`check_type` dns), which keeps the domain in `url`.
    """

    __tablename__ = "websites"
    __table_args__ = (
        # One check per target: a domain can still be pinged, and have each of
        # its record types looked up, side by side. dns_record_type is null
        # for the other types, which must count as equal here.
        UniqueConstraint(
            "url",
            "check_type",
            "dns_record_type",
            name="uq_websites_target",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The URL to request, for a ping check the host name or IP address, or
    #: for a DNS check the domain name.
    url: Mapped[str] = mapped_column(String(2048), nullable=False)

    # --- how to check ---------------------------------------------------
    check_type: Mapped[CheckType] = mapped_column(
        check_type_enum,
        default=CheckType.HTTP,
        server_default=text("'http'::check_type"),
        nullable=False,
    )
    #: Ping checks: echo requests sent per check.
    ping_count: Mapped[int] = mapped_column(
        Integer, default=5, server_default=text("5"), nullable=False
    )
    #: DNS checks: the record to look up; None for every other check type.
    dns_record_type: Mapped[DnsRecordType | None] = mapped_column(
        dns_record_type_enum, nullable=True
    )
    #: DNS checks: the records every resolver must return, normalized as
    #: `dns_probe` writes them. Empty means none are pinned: the check then
    #: learns the records and alerts when they change.
    dns_expected_values: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'"), nullable=False
    )
    method: Mapped[str] = mapped_column(
        String(10), default="GET", server_default=text("'GET'"), nullable=False
    )
    expected_status: Mapped[int] = mapped_column(
        Integer, default=200, server_default=text("200"), nullable=False
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, default=10, server_default=text("10"), nullable=False
    )
    check_interval_seconds: Mapped[int] = mapped_column(
        Integer, default=300, server_default=text("300"), nullable=False
    )
    #: Alerts per outage: the immediate one plus follow-ups. 4 => 1 + 3 retries.
    max_down_alerts: Mapped[int] = mapped_column(
        Integer, default=4, server_default=text("4"), nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: Times a failed check is repeated, CHECK_RETRY_DELAY_SECONDS apart, before
    #: it counts as failed. 0 makes every failure count at once.
    retries_on_failure: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    #: Text the response body must contain, and text it must not — else the
    #: check fails even on the expected status. Case-sensitive; None is no rule.
    must_contain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    must_not_contain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Headers sent with every request, e.g. an Authorization or an API key,
    #: as `[{"name": ..., "value": ...}]` in the order given. A value may be a
    #: secret, so each is encrypted at rest like a bot token.
    request_headers: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    #: Development, staging, production... None for sites added before the
    #: field existed; the UI asks for it on every new one.
    environment: Mapped[WebsiteEnvironment | None] = mapped_column(
        website_environment_enum, nullable=True
    )

    #: Addresses that are not user accounts, alerted for this site only.
    alert_emails: Mapped[list[str]] = mapped_column(
        ARRAY(String(255)), default=list, server_default=text("'{}'"), nullable=False
    )
    #: Also email the project's members and extra_emails. Off means only this
    #: site's own recipients hear about it. Slack, Telegram and WhatsApp are
    #: unaffected either way.
    inherit_project_recipients: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: Send this site's alerts to its own channel instead of the project's.
    #: Uses the project's bot token unless the site has one of its own.
    slack_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The site's own bot token (`xoxb-…`), encrypted at rest. Always paired
    #: with the site's own channel; lets a site post to Slack when its project
    #: has none, or to another workspace.
    slack_bot_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Send this site's Telegram alerts to its own chat instead of the
    #: project's. Uses the project's bot unless the site has one of its own.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The site's own Telegram bot token, encrypted at rest. Always paired with
    #: the site's own chat, like its Slack token.
    telegram_bot_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: WhatsApp this site's alerts to these numbers instead of the project's,
    #: still from the project's business number. Empty uses the project's.
    whatsapp_recipients: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), default=list, server_default=text("'{}'"), nullable=False
    )

    # --- live state -------------------------------------------------------
    status: Mapped[WebsiteStatus] = mapped_column(
        website_status_enum,
        default=WebsiteStatus.UNKNOWN,
        server_default=text("'unknown'::website_status"),
        nullable=False,
        index=True,
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    #: Start of the current outage; None while the site is up.
    down_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: Alerts already sent for the current outage; reset on recovery.
    down_alerts_sent: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: The current outage's first Slack message, and the channel it is in:
    #: follow-ups and the recovery reply in its thread. Cleared on recovery.
    slack_thread_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    slack_thread_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The current outage's first Telegram message, and the chat it is in:
    #: follow-ups and the recovery reply to it. Cleared on recovery.
    telegram_thread_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    telegram_thread_chat: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- site problems short of down --------------------------------------
    #: A response slower than this counts as slow. None uses the global
    #: SLOW_RESPONSE_THRESHOLD_MS.
    slow_threshold_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Consecutive slow (but successful) checks; reset by any fast or failed one.
    slow_streak: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    last_slow_alert_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Ping checks: a check that loses at least this share of its pings while
    #: the host still answers counts as lossy. None uses the global
    #: PACKET_LOSS_THRESHOLD_PERCENT.
    packet_loss_threshold_percent: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    #: Consecutive lossy (but answered) checks; reset by any clean or failed one.
    loss_streak: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    last_loss_alert_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The certificate's notAfter, as last read. None for plain-HTTP sites or
    #: before the first successful read.
    ssl_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the certificate was last *attempted*, so a failing handshake is
    #: retried on the interval rather than on every tick.
    ssl_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The smallest days-left threshold already warned about for the current
    #: certificate (0 = expired). None once the certificate is healthy again,
    #: which is what re-arms the warnings after a renewal.
    ssl_alert_bucket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The rest of the certificate as last read, for the site's page: who it
    #: was issued to (common name, else the first SAN) and by, the names it
    #: covers, when it began, and the TLS version the handshake settled on.
    ssl_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssl_issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssl_sans: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    ssl_valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ssl_tls_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: And how the handshake went: the cipher suite, the protocol agreed
    #: through ALPN (`h2` or `http/1.1`), and the certificates the server sent,
    #: leaf first, as `checker.chain_entry` writes them.
    ssl_cipher: Mapped[str | None] = mapped_column(String(96), nullable=True)
    ssl_alpn: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ssl_chain: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)

    # --- domain registration, looked up over RDAP -------------------------
    #: The registered domain the host belongs to, e.g. `example.co.uk` for
    #: `www.shop.example.co.uk`. None for an IP address, or before the first
    #: successful lookup.
    domain_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: When the registration ends, as the registry last said.
    domain_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    domain_registrar: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: When the domain was last *attempted*, like `ssl_checked_at`.
    domain_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Why the last lookup told us nothing new, e.g. a registry without RDAP;
    #: None after one that worked. What was learned before is kept meanwhile.
    domain_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: As `ssl_alert_bucket`, for DOMAIN_EXPIRY_ALERT_DAYS.
    domain_alert_bucket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The nameservers the registry last delegated the domain to, sorted;
    #: what a change is measured against. None before the registry first names
    #: any (some, like .de's, never do).
    domain_nameservers: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    #: When this site last saw them change, so the project's other sites on
    #: the domain can tell the change was already announced.
    domain_nameservers_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- security headers, from the last HTTP check that came up ----------
    #: `security_headers.capture()` of that response: its URL and the raw
    #: values; graded when read, see `security_report`.
    security_headers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    security_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- CDN, read from a successful HTTP check every CDN_CHECK_INTERVAL ---
    #: `cdn.CdnInfo.as_dict()`: the providers recognised with their evidence,
    #: the host's CNAME chain and the cache status. A dict with no providers
    #: means "looked, none found"; None means not looked at yet.
    cdn: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cdn_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: DNS checks: the records the resolvers last agreed on, what a change is
    #: measured against. None before they first agree, and after the record
    #: type or domain changes.
    dns_records: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # selectin so the monitoring loop can read project members without a lazy
    # load, which would raise MissingGreenlet under async SQLAlchemy.
    project: Mapped[Project] = relationship(
        back_populates="websites", lazy="selectin"
    )
    # selectin for the same reason as `project`.
    recipients: Mapped[list[User]] = relationship(
        "User", secondary=website_recipients, lazy="selectin", order_by=User.id
    )
    checks: Mapped[list["WebsiteCheck"]] = relationship(
        back_populates="website",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    #: The windows in effect or still to come, soonest first. Ended ones stay
    #: in the table but are not loaded. selectin for the same reason as
    #: `project`; read-only, so windows are added and ended through their own
    #: rows.
    maintenance_windows: Mapped[list["MaintenanceWindow"]] = relationship(
        "MaintenanceWindow",
        primaryjoin=lambda: and_(
            Website.id == MaintenanceWindow.website_id,
            MaintenanceWindow.ends_at > func.now(),
        ),
        lazy="selectin",
        viewonly=True,
        order_by=lambda: MaintenanceWindow.starts_at,
    )

    def maintenance_at(self, moment: datetime) -> "MaintenanceWindow | None":
        """The window `moment` falls in, if any."""
        return next(
            (w for w in self.maintenance_windows if w.starts_at <= moment < w.ends_at),
            None,
        )

    @property
    def maintenance(self) -> "MaintenanceWindow | None":
        """The window in effect now: no scheduled checks and no alerts until it ends."""
        return self.maintenance_at(datetime.now(UTC))

    @property
    def upcoming_maintenance(self) -> list["MaintenanceWindow"]:
        """Windows that have not started yet, soonest first."""
        now = datetime.now(UTC)
        return [w for w in self.maintenance_windows if w.starts_at > now]

    @property
    def security_report(self) -> "SecurityReport | None":
        """The stored security headers, graded; None before the first
        successful HTTP check, and for ping and DNS checks."""
        return grade_security_headers(self.security_headers)

    def outgoing_headers(self) -> dict[str, str]:
        """The request headers to send, decrypted. One that can no longer be
        read (the key changed; see `decrypt_secret`) is left out."""
        headers = {}
        for header in self.request_headers:
            value = decrypt_secret(header["value"])
            if value is not None:
                headers[header["name"]] = value
        return headers

    @property
    def request_header_hints(self) -> list[dict[str, str | None]]:
        """The request headers for the API: names, with values masked."""
        return [
            {"name": header["name"], "value_hint": mask_secret(decrypt_secret(header["value"]))}
            for header in self.request_headers
        ]

    @property
    def recipient_emails(self) -> list[str]:
        """This site's own addresses: active recipients, then alert_emails."""
        users = [user.email for user in self.recipients if user.is_active]
        return [*users, *self.alert_emails]

    @property
    def alert_recipients(self) -> list[str]:
        """Who is emailed about this site: the project's recipients (unless the
        site opted out of them), then the site's own."""
        inherited = (
            self.project.recipient_emails if self.inherit_project_recipients else []
        )
        return [*inherited, *self.recipient_emails]

    @property
    def has_email_alerting(self) -> bool:
        """Somebody would be emailed. Like the project's check, this counts
        suspended users — it is about configuration, not reachability."""
        if self.recipients or self.alert_emails:
            return True
        return self.inherit_project_recipients and self.project.has_email_alerting

    @property
    def has_slack_alerting(self) -> bool:
        """A token and a channel resolve, from the site or else its project.
        Like the project's check, the mute switch does not count here."""
        if self.slack_bot_token:
            return bool(self.slack_channel_id)
        channel = self.slack_channel_id or self.project.slack_channel_id
        return bool(self.project.slack_bot_token and channel)

    @property
    def slack_token_hint(self) -> str | None:
        """Masked tail of the site's own token; None when it uses the project's."""
        return mask_secret(decrypt_secret(self.slack_bot_token))

    @property
    def has_telegram_alerting(self) -> bool:
        """A token and a chat resolve, from the site or else its project.
        Like Slack's, the mute switch does not count here."""
        if self.telegram_bot_token:
            return bool(self.telegram_chat_id)
        chat = self.telegram_chat_id or self.project.telegram_chat_id
        return bool(self.project.telegram_bot_token and chat)

    @property
    def telegram_token_hint(self) -> str | None:
        """Masked site's own token; None when it uses the project's."""
        return mask_secret(decrypt_secret(self.telegram_bot_token))

    @property
    def has_whatsapp_alerting(self) -> bool:
        """The project has a sender, and there are numbers to send to, the
        site's or else the project's. The mute switch does not count here."""
        project = self.project
        if not (project.whatsapp_access_token and project.whatsapp_phone_number_id):
            return False
        return bool(self.whatsapp_recipients or project.whatsapp_recipients)

    @property
    def alert_channels(self) -> list[str]:
        """Which channels reach this site's alerts."""
        channels = []
        if self.has_email_alerting:
            channels.append("email")
        if self.has_slack_alerting:
            channels.append("slack")
        if self.has_telegram_alerting:
            channels.append("telegram")
        if self.has_whatsapp_alerting:
            channels.append("whatsapp")
        return channels

    def __repr__(self) -> str:
        return f"<Website {self.name!r} {self.url!r} status={self.status.value!r}>"


class WebsiteCheck(Base):
    """One poll of a website. The audit trail behind every alert."""

    __tablename__ = "website_checks"
    __table_args__ = (
        # Serves "latest checks for this site", the only way history is read.
        Index("ix_website_checks_site_time", "website_id", desc("checked_at")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), nullable=False
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    is_up: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The HTTP reason phrase that came with status_code, e.g. "Service Unavailable".
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Populated when the request never produced a response (DNS, TLS, timeout).
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Why the check failed, as a stable key to group incidents by:
    #: `dns_error`, `connect_timeout`, `tls_error`, `unexpected_status`, …
    #: None when the check succeeded.
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: URL actually reached, after redirects.
    final_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    #: The response headers kept for diagnosis (checker.DIAGNOSTIC_HEADERS).
    #: None when there was no response, or it sent none of them.
    headers: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    #: The hops that led to `final_url`, each `{url, status, location}`, as
    #: `checker.redirect_chain` writes them. None when there were none.
    redirects: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    #: Bytes of the response body once decoded. None when there was no response.
    content_length: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- where the time went, in ms, summed over any redirects -------------
    # None when the step did not finish on this check: it failed first, or a
    # reused connection left nothing to look up, connect or negotiate. Rows
    # from before these were recorded have None throughout.
    dns_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connect_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tls_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: From the request being sent to the response headers arriving.
    first_byte_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- ping checks only; None on HTTP checks ------------------------------
    # For a ping, `response_time_ms` is the average round trip, rounded, and
    # `dns_ms` the time to resolve the host name.
    #: Echo requests attempted, and those answered within the timeout. None
    #: when nothing could be sent, e.g. the name did not resolve.
    packets_sent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    packets_received: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Round trips of the answered requests; None when none were.
    rtt_min_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_avg_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_max_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Mean difference between consecutive round trips; None under two replies.
    jitter_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: The address pinged: the host itself, or what its name resolved to.
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: DNS checks only: the record type, the expected values, and each
    #: resolver's answer, as `dns_probe.DnsResult.as_dict()` writes them. For a
    #: DNS check `response_time_ms` is the resolvers' average answer time.
    dns: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    website: Mapped[Website] = relationship(back_populates="checks")

    @property
    def ping(self) -> dict | None:
        """The ping figures as one group, for the API; None for an HTTP check
        and for a ping that sent nothing."""
        if self.packets_sent is None:
            return None
        sent, received = self.packets_sent, self.packets_received or 0
        return {
            "address": self.ip_address,
            "sent": sent,
            "received": received,
            "loss_percent": round(100 * (sent - received) / sent, 1) if sent else 100.0,
            "min_ms": self.rtt_min_ms,
            "avg_ms": self.rtt_avg_ms,
            "max_ms": self.rtt_max_ms,
            "jitter_ms": self.jitter_ms,
        }

    def __repr__(self) -> str:
        state = "up" if self.is_up else "down"
        return f"<WebsiteCheck site={self.website_id} {state} {self.status_code}>"


class WebsiteEvent(Base):
    """Something a check found that people should hear about: an outage, a
    recovery, a slow spell, an expiring certificate or domain, changed DNS
    records. The app's own feed.

    Written with the check that raised it, whether or not email or Slack is
    switched on for that kind, so the feed never depends on those settings.
    `kind` is a `NotificationKind` value, a plain string as in
    `notification_settings`. Each kind sets only the columns it uses.
    Purged after CHECK_RETENTION_DAYS, like the raw checks.
    """

    __tablename__ = "website_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: The check's one-line description, e.g. "HTTP 503 Service Unavailable".
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: recovered: how long the outage lasted.
    downtime_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: slow_response: the threshold the site was slower than.
    threshold_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: ssl_expiring: when the certificate ends (or ended).
    ssl_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: domain_expiring: when the domain registration ends (or ended).
    domain_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    website: Mapped[Website] = relationship()

    def __repr__(self) -> str:
        return f"<WebsiteEvent site={self.website_id} {self.kind!r}>"


class MaintenanceWindow(Base):
    """A stretch of time when a site is expected to fail, e.g. a deployment.

    While one is in effect the scheduler does not check the site, so nothing
    alerts and its uptime is not dinged; the first check after it picks up
    from the state the site was in before. Started by hand ("30 minutes from
    now") or scheduled ahead; ending one early sets `ends_at` to the moment it
    ended. A site's windows never overlap. Purged after CHECK_RETENTION_DAYS,
    like the feed.
    """

    __tablename__ = "maintenance_windows"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_maintenance_windows_order"),
        # Serves "this site's windows that have not ended", the only way
        # they are read.
        Index("ix_maintenance_windows_site_end", "website_id", "ends_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: What is going on, e.g. "Deploying 2.4"; shown on the site's page.
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<MaintenanceWindow site={self.website_id} "
            f"{self.starts_at.isoformat()} -> {self.ends_at.isoformat()}>"
        )


def maintenance_in_effect(at) -> Exists:
    """SQL: the site has a window covering `at`, a datetime or SQL expression."""
    return (
        select(MaintenanceWindow.id)
        .where(
            MaintenanceWindow.website_id == Website.id,
            MaintenanceWindow.starts_at <= at,
            MaintenanceWindow.ends_at > at,
        )
        .exists()
    )


#: Lower edge, in ms, of each response-time bucket in
#: `WebsiteCheckHourly.histogram`; the last bucket is open-ended. Each is about
#: 25% wider than the one before, so a percentile read back from the buckets is
#: off by at most one bucket's width. Migration 0017 holds a copy for its
#: backfill: change the two together, and re-roll history when you do.
RESPONSE_BUCKETS_MS: tuple[int, ...] = (
    0, 10, 12, 16, 20, 24, 31, 38, 48, 60, 75, 93, 116, 146, 182, 227, 284,
    355, 444, 555, 694, 867, 1084, 1355, 1694, 2118, 2647, 3309, 4136, 5170,
    6462, 8078, 10097, 12622, 15777, 19722, 24652, 30815, 38519, 48148, 60185,
)


class WebsiteCheckHourly(Base):
    """One site's checks in one UTC hour, summed up.

    Charts and long-range uptime read these instead of `website_checks`, which
    is purged after CHECK_RETENTION_DAYS; these rows are kept. Response-time
    figures cover successful checks only, as in the monthly report.
    """

    __tablename__ = "website_check_hourly"

    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), primary_key=True
    )
    #: Start of the hour, UTC.
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    checks: Mapped[int] = mapped_column(Integer, nullable=False)
    up_checks: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Successful checks that recorded a response time; what sum_ms divides by.
    timed_checks: Mapped[int] = mapped_column(Integer, nullable=False)
    sum_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Count of timed checks per RESPONSE_BUCKETS_MS bucket. Sums across hours,
    #: which a stored p95 would not.
    histogram: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)
    #: Ping checks' echo requests, summed; 0 for HTTP checks. Packet loss is
    #: read from these, so a lossy hour weighs by its packets.
    packets_sent: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    packets_received: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
