from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    func,
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


def member_project_ids(user_id: int) -> Select:
    """Subquery of the project ids `user_id` belongs to.

    Used to scope what viewers and developers are allowed to see, in both the
    project and the website queries.
    """
    return select(project_members.c.project_id).where(
        project_members.c.user_id == user_id
    )


class Project(Base, TimestampMixin):
    """A client or product whose sites are monitored together."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
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
    def alert_channels(self) -> list[str]:
        """Which channels this project is set up for. Never empty — every
        project must have at least one."""
        channels = []
        if self.has_email_alerting:
            channels.append("email")
        if self.has_slack_alerting:
            channels.append("slack")
        return channels

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_enabled and self.slack_bot_token and self.slack_channel_id)

    @property
    def slack_token_hint(self) -> str | None:
        """Masked tail of the stored token, so the UI can show what is set."""
        return mask_secret(decrypt_secret(self.slack_bot_token))

    @property
    def recipient_emails(self) -> list[str]:
        """Everyone this project alerts: its members plus its extra addresses."""
        return [*self.member_emails, *self.extra_emails]

    def __repr__(self) -> str:
        return f"<Project {self.name!r} members={len(self.members)}>"
