import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    func,
    or_,
    select,
    text,
)
from sqlalchemy import Select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.crypto import decrypt_secret, mask_secret
from app.db.base import Base, TimestampMixin
from app.db.models.user import User

#: Who is responsible for a project. Membership is what decides who gets
#: alert emails when one of the project's sites goes down.
project_members = Table(
    "project_members",
    Base.metadata,
    Column(
        "project_id",
        ForeignKey("projects.id", ondelete="CASCADE"),
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


class ProjectMonitors(str, enum.Enum):
    """What a project watches. Chosen when it is created, and fixed."""

    #: Websites: URLs, hosts and DNS records (`app/monitoring/websites`).
    WEBSITES = "websites"
    #: AWS infrastructure, through the project's own AWS accounts
    #: (`app/monitoring/infra/aws`).
    INFRASTRUCTURE = "infrastructure"


class Project(Base, TimestampMixin):
    """A client or product whose sites are monitored together."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    monitors: Mapped[ProjectMonitors] = mapped_column(
        SAEnum(
            ProjectMonitors,
            name="project_monitors",
            native_enum=True,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda cls: [member.value for member in cls],
        ),
        default=ProjectMonitors.WEBSITES,
        server_default=ProjectMonitors.WEBSITES.value,
        nullable=False,
    )
    #: Addresses that are not user accounts — a client contact, a shared
    #: on-call inbox. Alerted for every site in the project, alongside members.
    extra_emails: Mapped[list[str]] = mapped_column(
        ARRAY(String(255)), default=list, server_default=text("'{}'"), nullable=False
    )
    # --- Slack ------------------------------------------------------------
    #: Bot token (`xoxb-…`), encrypted at rest. Never returned by the API.
    slack_bot_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Default channel for this project's alerts, e.g. `C0123456789`. A site
    #: may override it with a channel of its own.
    slack_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Lets you mute Slack without discarding the token and channel.
    slack_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    # --- Telegram ---------------------------------------------------------
    #: Bot token from @BotFather (`123456789:AA…`), encrypted at rest. Never
    #: returned by the API.
    telegram_bot_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Default chat for this project's alerts: a numeric id such as
    #: `-1001234567890`, or a public `@channelname`. A site may override it.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Lets you mute Telegram without discarding the token and chat.
    telegram_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    # --- WhatsApp ---------------------------------------------------------
    #: Meta access token of a system user that may send for the business
    #: number, encrypted at rest. Never returned by the API.
    whatsapp_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Meta's id for the business number that sends (WhatsApp Manager → API
    #: setup) — not the phone number itself.
    whatsapp_phone_number_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    #: Numbers alerted for every site in this project, with country code, e.g.
    #: `+8801712345678`. A site may alert numbers of its own instead.
    whatsapp_recipients: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), default=list, server_default=text("'{}'"), nullable=False
    )
    #: Lets you mute WhatsApp without discarding the settings.
    whatsapp_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    #: The project manager (or admin/DevOps) who created it. Decides who may
    #: edit the project — see app/core/permissions.can_manage_project.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    owner: Mapped[User | None] = relationship(
        "User", foreign_keys=[owner_id], lazy="selectin"
    )
    # selectin, not lazy: these are read from the async monitoring loop, where
    # a lazy load would raise MissingGreenlet.
    members: Mapped[list[User]] = relationship(
        "User", secondary=project_members, lazy="selectin", order_by=User.id
    )
    websites: Mapped[list["Website"]] = relationship(  # noqa: F821
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def includes_user(self, user_id: int) -> bool:
        """Whether `user_id` owns this project or is one of its members — what
        lets someone without a global role see it. The in-memory twin of
        `visible_project_ids`."""
        return self.owner_id == user_id or any(m.id == user_id for m in self.members)

    @property
    def member_emails(self) -> list[str]:
        """Active members' addresses. A suspended user is not alerted."""
        return [member.email for member in self.members if member.is_active]

    @property
    def has_email_alerting(self) -> bool:
        """Somebody would be emailed. Counts members regardless of suspension —
        this is about how the project is *configured*, not who is reachable
        right now."""
        return bool(self.members or self.extra_emails)

    @property
    def has_slack_alerting(self) -> bool:
        """Both halves of the Slack config are present. `slack_enabled` is a
        mute switch and deliberately does not count here."""
        return bool(self.slack_bot_token and self.slack_channel_id)

    @property
    def has_telegram_alerting(self) -> bool:
        """Both halves of the Telegram config are present. Like Slack's,
        `telegram_enabled` is a mute switch and does not count here."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def has_whatsapp_alerting(self) -> bool:
        """A sender and somebody to send to are all present. Like the others,
        `whatsapp_enabled` is a mute switch and does not count here."""
        return bool(
            self.whatsapp_access_token
            and self.whatsapp_phone_number_id
            and self.whatsapp_recipients
        )

    @property
    def alert_channels(self) -> list[str]:
        """Which channels this project is set up for. Never empty — every
        project must have at least one."""
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

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_enabled and self.slack_bot_token and self.slack_channel_id)

    @property
    def slack_token_hint(self) -> str | None:
        """Masked tail of the stored token, so the UI can show what is set."""
        return mask_secret(decrypt_secret(self.slack_bot_token))

    @property
    def telegram_configured(self) -> bool:
        return bool(
            self.telegram_enabled and self.telegram_bot_token and self.telegram_chat_id
        )

    @property
    def telegram_token_hint(self) -> str | None:
        """Masked stored token, e.g. `123456789:…wxyz`."""
        return mask_secret(decrypt_secret(self.telegram_bot_token))

    @property
    def whatsapp_configured(self) -> bool:
        return self.whatsapp_enabled and self.has_whatsapp_alerting

    @property
    def whatsapp_token_hint(self) -> str | None:
        """Masked stored token, e.g. `EAA…wxyz`."""
        return mask_secret(decrypt_secret(self.whatsapp_access_token))

    @property
    def recipient_emails(self) -> list[str]:
        """Everyone this project alerts: its members plus its extra addresses."""
        return [*self.member_emails, *self.extra_emails]

    def __repr__(self) -> str:
        return f"<Project {self.name!r} members={len(self.members)}>"


def visible_project_ids(user_id: int) -> Select:
    """Subquery of the project ids `user_id` may see without a global role:
    the ones they are a member of, and the ones they own.

    Used to scope what everyone but admin and DevOps is allowed to see, in both
    the project and the website queries. Owners are included so a project
    manager can see what they create without having to add themselves to it —
    membership also means receiving its alerts.
    """
    member_of = select(project_members.c.project_id).where(
        project_members.c.user_id == user_id
    )
    return select(Project.id).where(
        or_(Project.owner_id == user_id, Project.id.in_(member_of))
    )
