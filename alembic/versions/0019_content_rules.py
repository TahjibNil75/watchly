"""websites: fail a check when the body lacks, or has, given text

Revision ID: 0019_content_rules
Revises: 0018_check_retries
Create Date: 2026-09-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_content_rules"
down_revision: str | None = "0018_check_retries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("websites", sa.Column("must_contain", sa.String(255), nullable=True))
    op.add_column("websites", sa.Column("must_not_contain", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("websites", "must_not_contain")
    op.drop_column("websites", "must_contain")
