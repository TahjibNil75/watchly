"""notification settings, monthly report log, SSL and slow-response state

Revision ID: 0007_notifications
Revises: 0006_website_recipients
Create Date: 2026-09-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_notifications"
down_revision: str | None = "0006_website_recipients"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # NULL = the global level.
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("email_enabled", sa.Boolean(), nullable=True),
        sa.Column("slack_enabled", sa.Boolean(), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_notification_settings_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_notification_settings_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_settings")),
    )
    op.create_index(
        "uq_notification_settings_project_kind",
        "notification_settings",
        ["project_id", "kind"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "uq_notification_settings_global_kind",
        "notification_settings",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
    )

    op.create_table(
        "report_deliveries",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "channels",
            postgresql.ARRAY(sa.String(length=16)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_report_deliveries_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "project_id", "period_start", name=op.f("pk_report_deliveries")
        ),
    )

    op.add_column("websites", sa.Column("slow_threshold_ms", sa.Integer(), nullable=True))
    op.add_column(
        "websites",
        sa.Column(
            "slow_streak", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "websites",
        sa.Column("last_slow_alert_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "websites",
        sa.Column("ssl_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "websites",
        sa.Column("ssl_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "websites", sa.Column("ssl_alert_bucket", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("websites", "ssl_alert_bucket")
    op.drop_column("websites", "ssl_checked_at")
    op.drop_column("websites", "ssl_expires_at")
    op.drop_column("websites", "last_slow_alert_at")
    op.drop_column("websites", "slow_streak")
    op.drop_column("websites", "slow_threshold_ms")
    op.drop_table("report_deliveries")
    op.drop_index(
        "uq_notification_settings_global_kind", table_name="notification_settings"
    )
    op.drop_index(
        "uq_notification_settings_project_kind", table_name="notification_settings"
    )
    op.drop_table("notification_settings")
