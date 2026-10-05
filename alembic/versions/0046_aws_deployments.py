"""aws_deployments: CodeDeploy deployments, announced, with their resources silenced

An AWS account may now watch its CodeDeploy deployments (`watch_deployments`,
off for every existing account). Each one Watchly sees start is announced and
kept in `aws_deployments`; the resources it deploys to get a maintenance
window tied to it (`aws_maintenance_windows.deployment_id`), renewed while it
runs and ended with it. Its feed events are about the account, not a VPC, so
`aws_events.vpc_id` may now be empty.

Revision ID: 0046_aws_deployments
Revises: 0045_database_checks
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046_aws_deployments"
down_revision: str | None = "0045_database_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENVIRONMENTS = ("development", "testing", "uat", "staging", "production")


def upgrade() -> None:
    op.add_column(
        "aws_accounts",
        sa.Column("watch_deployments", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "aws_accounts", sa.Column("deployments_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("aws_accounts", sa.Column("deployments_error", sa.Text(), nullable=True))

    op.create_table(
        "aws_deployments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("deployment_id", sa.String(length=32), nullable=False),
        sa.Column("application_name", sa.String(length=255), nullable=False),
        sa.Column("group_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "environment",
            postgresql.ENUM(*ENVIRONMENTS, name="website_environment", create_type=False),
            nullable=True,
        ),
        sa.Column("creator", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("revision", sa.String(length=1024), nullable=True),
        sa.Column(
            "resources",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("fallback", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("slack_thread_ts", sa.String(length=32), nullable=True),
        sa.Column("slack_thread_channel", sa.String(length=32), nullable=True),
        sa.Column("telegram_thread_message_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_thread_chat", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["aws_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "region", "deployment_id", name="uq_aws_deployments_deployment"),
    )
    op.create_index("ix_aws_deployments_account_id", "aws_deployments", ["account_id"])
    op.create_index("ix_aws_deployments_finished_at", "aws_deployments", ["finished_at"])

    op.add_column("aws_maintenance_windows", sa.Column("deployment_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "aws_maintenance_windows_deployment_id_fkey",
        "aws_maintenance_windows",
        "aws_deployments",
        ["deployment_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_aws_maintenance_windows_deployment_id", "aws_maintenance_windows", ["deployment_id"]
    )

    op.alter_column("aws_events", "vpc_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # Deployment events have no VPC to keep them by.
    op.execute("DELETE FROM aws_events WHERE vpc_id IS NULL")
    op.alter_column("aws_events", "vpc_id", existing_type=sa.Integer(), nullable=False)

    # A deployment's windows go with it, as its cascade would take them.
    op.execute("DELETE FROM aws_maintenance_windows WHERE deployment_id IS NOT NULL")
    op.drop_index("ix_aws_maintenance_windows_deployment_id", table_name="aws_maintenance_windows")
    op.drop_constraint(
        "aws_maintenance_windows_deployment_id_fkey", "aws_maintenance_windows", type_="foreignkey"
    )
    op.drop_column("aws_maintenance_windows", "deployment_id")

    op.drop_index("ix_aws_deployments_finished_at", table_name="aws_deployments")
    op.drop_index("ix_aws_deployments_account_id", table_name="aws_deployments")
    op.drop_table("aws_deployments")

    op.drop_column("aws_accounts", "deployments_error")
    op.drop_column("aws_accounts", "deployments_checked_at")
    op.drop_column("aws_accounts", "watch_deployments")
