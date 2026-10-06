"""Capacity alerts: a server's `ec2_metrics` check, more `db_metrics`
thresholds, and an AWS account's Elastic IPs and quota

- `ec2_metrics` reads a server's CloudWatch metrics: CPU, a burstable
  instance's CPU credits, its volumes' burst balance and IOPS, and its disks
  when the CloudWatch agent reports them.
- `db_metrics` gains a storage forecast, connections against
  max_connections, provisioned IOPS, gp2 burst balance and CPU credits.
  Existing checks get them at their defaults; a key a check already has is
  left as it is.
- An AWS account may now watch its capacity (`watch_capacity`, off for every
  existing account): Elastic IPs attached to nothing, and its Elastic IP
  quota. What the last look found is kept in `capacity`.

Revision ID: 0047_capacity_alerts
Revises: 0046_aws_deployments
Create Date: 2026-10-07

"""
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_capacity_alerts"
down_revision: str | None = "0046_aws_deployments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The new db_metrics settings, at the defaults `DbMetricsSettings` gives.
DB_METRICS_DEFAULTS = {
    "storage_full_days_min": 14,
    "connections_percent_max": 80,
    "max_connections": None,
    "iops_percent_max": 90,
    "burst_balance_percent_min": 20,
    "cpu_credits_percent_min": 10,
}


def upgrade() -> None:
    # A value added to an enum cannot be used in the transaction that adds it;
    # nothing below does.
    op.execute("ALTER TYPE aws_check_type ADD VALUE IF NOT EXISTS 'ec2_metrics'")

    # The defaults on the left, so a key the check already has wins. Compared
    # as text: on a fresh database `db_metrics` was added to the enum in this
    # same transaction, and Postgres refuses to use it as one yet.
    op.execute(
        sa.text(
            "UPDATE aws_checks SET settings = CAST(:defaults AS jsonb) || settings "
            "WHERE check_type::text = 'db_metrics'"
        ).bindparams(defaults=json.dumps(DB_METRICS_DEFAULTS))
    )

    op.add_column(
        "aws_accounts",
        sa.Column("watch_capacity", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("aws_accounts", sa.Column("capacity_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("aws_accounts", sa.Column("capacity_error", sa.Text(), nullable=True))
    op.add_column(
        "aws_accounts",
        sa.Column(
            "capacity",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.execute("DELETE FROM aws_events WHERE kind = 'account_capacity'")
    op.drop_column("aws_accounts", "capacity")
    op.drop_column("aws_accounts", "capacity_error")
    op.drop_column("aws_accounts", "capacity_checked_at")
    op.drop_column("aws_accounts", "watch_capacity")

    # The older settings model refuses keys it does not know.
    removed = " - ".join(f"'{key}'" for key in DB_METRICS_DEFAULTS)
    op.execute(f"UPDATE aws_checks SET settings = settings - {removed} WHERE check_type = 'db_metrics'")

    # Postgres cannot drop an enum value. The rows go; the value stays unused.
    op.execute("DELETE FROM aws_checks WHERE check_type = 'ec2_metrics'")
