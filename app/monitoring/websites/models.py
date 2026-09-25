import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Select,
    String,
    Table,
    Text,
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
    """A site to poll, together with the live state of its current outage."""

    __tablename__ = "websites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)

    # --- how to check ---------------------------------------------------
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
    #: site's own recipients hear about it. Slack is unaffected either way.
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
    def alert_channels(self) -> list[str]:
        """Which channels reach this site's alerts."""
        channels = []
        if self.has_email_alerting:
            channels.append("email")
        if self.has_slack_alerting:
            channels.append("slack")
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

    # --- where the time went, in ms, summed over any redirects -------------
    # None when the step did not finish on this check: it failed first, or a
    # reused connection left nothing to look up, connect or negotiate. Rows
    # from before these were recorded have None throughout.
    dns_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connect_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tls_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: From the request being sent to the response headers arriving.
    first_byte_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    website: Mapped[Website] = relationship(back_populates="checks")

    def __repr__(self) -> str:
        state = "up" if self.is_up else "down"
        return f"<WebsiteCheck site={self.website_id} {state} {self.status_code}>"


class WebsiteEvent(Base):
    """Something a check found that people should hear about: an outage, a
    recovery, a slow spell, an expiring certificate. The app's own feed.

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

    website: Mapped[Website] = relationship()

    def __repr__(self) -> str:
        return f"<WebsiteEvent site={self.website_id} {self.kind!r}>"


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
