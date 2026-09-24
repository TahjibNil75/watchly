import enum
from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import ARRAY
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
    #: Populated when the request never produced a response (DNS, TLS, timeout).
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: URL actually reached, after redirects.
    final_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    website: Mapped[Website] = relationship(back_populates="checks")

    def __repr__(self) -> str:
        state = "up" if self.is_up else "down"
        return f"<WebsiteCheck site={self.website_id} {state} {self.status_code}>"
