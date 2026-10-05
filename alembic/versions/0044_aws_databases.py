"""RDS databases: the `database` resource kind, and its `db_status` and
`db_metrics` checks, which read RDS and CloudWatch without logging in

Revision ID: 0044_aws_databases
Revises: 0043_aws_account_environment
Create Date: 2026-10-05

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0044_aws_databases"
down_revision: str | None = "0043_aws_account_environment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE aws_resource_kind ADD VALUE IF NOT EXISTS 'database'")
    op.execute("ALTER TYPE aws_check_type ADD VALUE IF NOT EXISTS 'db_status'")
    op.execute("ALTER TYPE aws_check_type ADD VALUE IF NOT EXISTS 'db_metrics'")


def downgrade() -> None:
    # Postgres cannot drop an enum value. The rows go; the values stay unused.
    op.execute("DELETE FROM aws_resources WHERE kind = 'database'")
    op.execute("DELETE FROM aws_checks WHERE check_type IN ('db_status', 'db_metrics')")
