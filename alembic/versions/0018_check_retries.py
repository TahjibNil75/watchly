"""websites: retry a failed check before it counts

Existing sites start at one retry, so a one-off blip no longer raises an alert.
Set retries_on_failure to 0 on a site that must alert on the first failure.

Revision ID: 0018_check_retries
Revises: 0017_check_hourly_rollup
Create Date: 2026-09-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_check_retries"
down_revision: str | None = "0017_check_hourly_rollup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "websites",
        sa.Column(
            "retries_on_failure",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("websites", "retries_on_failure")
