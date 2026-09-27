"""Storage for how each notification behaves, and which reports have gone out."""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# `notification_settings.project_id` targets `projects.id` and `updated_by_id`
# targets `users.id`; both tables must be registered on Base.metadata.
from app.db.models.user import User  # noqa: F401
from app.monitoring.projects.models import Project  # noqa: F401


class NotificationSetting(Base, TimestampMixin):
    """Overrides for one notification kind, at one level.

    `project_id` NULL is the global level, set by admins; a project row
    overrides it for that project. Every override column is nullable and NULL
    means "inherit from the next level up", ending at the built-in defaults in
    `catalog.py`. So a row only ever records what someone changed, and a
    project that changes nothing has no row at all.

    `kind` is a plain string rather than a Postgres enum so adding a
    notification kind never needs an `ALTER TYPE` migration.
    """

    __tablename__ = "notification_settings"
    __table_args__ = (
        # NULLs are distinct in a plain unique constraint, so the global level
        # (project_id NULL) needs a partial index of its own.
        Index(
            "uq_notification_settings_project_kind",
            "project_id",
            "kind",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
        ),
        Index(
            "uq_notification_settings_global_kind",
            "kind",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    email_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    slack_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    telegram_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Template text with `{{placeholders}}`; see `catalog.py` for the names.
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        scope = f"project={self.project_id}" if self.project_id else "global"
        return f"<NotificationSetting {self.kind!r} {scope}>"


class ReportDelivery(Base):
    """One monthly report the scheduler has handled for a project.

    The row is written *before* sending, so a slow or failing mail server can
    never make the same report go out twice. The trade-off is at-most-once: a
    report nobody received is not retried, and `channels` being empty is how
    to spot one. `POST /projects/{id}/report` sends it again by hand.
    """

    __tablename__ = "report_deliveries"

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    #: First day of the month the report covers.
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Channels that delivered it. Empty while sending, and if none did.
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), default=list, server_default=text("'{}'"), nullable=False
    )
