"""users: count failed sign-ins, to suspend after too many in a row

Revision ID: 0016_failed_login_attempts
Revises: 0015_refresh_tokens
Create Date: 2026-09-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_failed_login_attempts"
down_revision: str | None = "0015_refresh_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "failed_login_attempts")
