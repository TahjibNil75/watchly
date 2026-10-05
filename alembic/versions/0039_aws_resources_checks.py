"""aws_resources and their checks, results, rollups, feed and maintenance

A resource is an EC2 instance (public or private subnet), an Application or
Network Load Balancer (internal or internet-facing) or an EC2 Auto Scaling
group in a registered VPC, read from AWS; it has 1-10 checks (ping, tcp, http,
target_health, group_health), and the resource, not the check, is what goes
down and alerts. Results roll up by the
hour as website checks do. Starts empty.

Revision ID: 0039_aws_resources_checks
Revises: 0038_aws_vpcs
Create Date: 2026-10-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039_aws_resources_checks"
down_revision: str | None = "0038_aws_vpcs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "aws_resource_kind": ("server", "load_balancer", "auto_scaling_group"),
    "aws_check_type": ("ping", "tcp", "http", "target_health", "group_health"),
    "aws_resource_status": ("unknown", "up", "down"),
    "aws_check_health": ("unknown", "healthy", "degraded", "down"),
}
ENVIRONMENTS = ("development", "testing", "uat", "staging", "production")


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "aws_resources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("vpc_id", sa.Integer(), nullable=False),
        sa.Column("kind", _enum("aws_resource_kind"), nullable=False),
        sa.Column("aws_id", sa.String(length=512), nullable=False),
        sa.Column("aws_arn", sa.String(length=2048), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "environment",
            postgresql.ENUM(*ENVIRONMENTS, name="website_environment", create_type=False),
            nullable=True,
        ),
        sa.Column("address", sa.String(length=512), nullable=True),
        sa.Column("aws_state", sa.String(length=64), nullable=True),
        sa.Column(
            "aws_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("max_down_alerts", sa.Integer(), server_default=sa.text("4"), nullable=False),
        sa.Column(
            "status",
            _enum("aws_resource_status"),
            server_default=sa.text("'unknown'::aws_resource_status"),
            nullable=False,
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("down_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("degraded_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("down_alerts_sent", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("down_alert_held", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("slack_thread_ts", sa.String(length=32), nullable=True),
        sa.Column("slack_thread_channel", sa.String(length=32), nullable=True),
        sa.Column("telegram_thread_message_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_thread_chat", sa.String(length=64), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vpc_id"], ["aws_vpcs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vpc_id", "aws_id", name="uq_aws_resources_aws_id"),
    )
    op.create_index(op.f("ix_aws_resources_project_id"), "aws_resources", ["project_id"])
    op.create_index(op.f("ix_aws_resources_vpc_id"), "aws_resources", ["vpc_id"])
    op.create_index(op.f("ix_aws_resources_status"), "aws_resources", ["status"])

    op.create_table(
        "aws_resource_recipients",
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["resource_id"], ["aws_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("resource_id", "user_id"),
    )

    op.create_table(
        "aws_checks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("check_type", _enum("aws_check_type"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("check_interval_seconds", sa.Integer(), server_default=sa.text("300"), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("retries_on_failure", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "health",
            _enum("aws_check_health"),
            server_default=sa.text("'unknown'::aws_check_health"),
            nullable=False,
        ),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "problems",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(["resource_id"], ["aws_resources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_aws_checks_resource_id"), "aws_checks", ["resource_id"])
    op.create_index(op.f("ix_aws_checks_last_checked_at"), "aws_checks", ["last_checked_at"])

    op.create_table(
        "aws_check_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("response_time_ms", sa.Integer(), nullable=True),
        sa.Column("dns_ms", sa.Integer(), nullable=True),
        sa.Column("connect_ms", sa.Integer(), nullable=True),
        sa.Column("tls_ms", sa.Integer(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("address", sa.String(length=64), nullable=True),
        sa.Column("metric", sa.Float(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["check_id"], ["aws_checks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resource_id"], ["aws_resources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_aws_check_results_check_time",
        "aws_check_results",
        ["check_id", sa.text("checked_at DESC")],
    )
    op.create_index(op.f("ix_aws_check_results_resource_id"), "aws_check_results", ["resource_id"])
    op.create_index(op.f("ix_aws_check_results_checked_at"), "aws_check_results", ["checked_at"])

    op.create_table(
        "aws_check_hourly",
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checks", sa.Integer(), nullable=False),
        sa.Column("up_checks", sa.Integer(), nullable=False),
        sa.Column("timed_checks", sa.Integer(), nullable=False),
        sa.Column("sum_ms", sa.BigInteger(), nullable=False),
        sa.Column("max_ms", sa.Integer(), nullable=True),
        sa.Column("metric_min", sa.Float(), nullable=True),
        sa.Column("metric_max", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["check_id"], ["aws_checks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("check_id", "hour"),
    )

    op.create_table(
        "aws_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("vpc_id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("downtime_seconds", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vpc_id"], ["aws_vpcs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resource_id"], ["aws_resources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_aws_events_project_id"), "aws_events", ["project_id"])
    op.create_index(op.f("ix_aws_events_resource_id"), "aws_events", ["resource_id"])
    op.create_index(op.f("ix_aws_events_occurred_at"), "aws_events", ["occurred_at"])

    op.create_table(
        "aws_maintenance_windows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("ends_at > starts_at", name="ck_aws_maintenance_windows_order"),
        sa.ForeignKeyConstraint(["resource_id"], ["aws_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_aws_maintenance_windows_resource_end",
        "aws_maintenance_windows",
        ["resource_id", "ends_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_aws_maintenance_windows_resource_end", table_name="aws_maintenance_windows")
    op.drop_table("aws_maintenance_windows")
    for index in ("occurred_at", "resource_id", "project_id"):
        op.drop_index(op.f(f"ix_aws_events_{index}"), table_name="aws_events")
    op.drop_table("aws_events")
    op.drop_table("aws_check_hourly")
    for index in ("checked_at", "resource_id"):
        op.drop_index(op.f(f"ix_aws_check_results_{index}"), table_name="aws_check_results")
    op.drop_index("ix_aws_check_results_check_time", table_name="aws_check_results")
    op.drop_table("aws_check_results")
    for index in ("last_checked_at", "resource_id"):
        op.drop_index(op.f(f"ix_aws_checks_{index}"), table_name="aws_checks")
    op.drop_table("aws_checks")
    op.drop_table("aws_resource_recipients")
    for index in ("status", "vpc_id", "project_id"):
        op.drop_index(op.f(f"ix_aws_resources_{index}"), table_name="aws_resources")
    op.drop_table("aws_resources")
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
