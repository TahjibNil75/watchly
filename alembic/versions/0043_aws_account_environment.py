"""aws_accounts.environment: the environment its resources get by default

Revision ID: 0043_aws_account_environment
Revises: 0042_project_monitors
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_aws_account_environment"
down_revision: str | None = "0042_project_monitors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENVIRONMENTS = ("development", "testing", "uat", "staging", "production")


def upgrade() -> None:
    op.add_column(
        "aws_accounts",
        sa.Column(
            "environment",
            postgresql.ENUM(*ENVIRONMENTS, name="website_environment", create_type=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("aws_accounts", "environment")
