"""users: temporary passwords for "forgot password"

Revision ID: 0014_forgot_password
Revises: 0013_pending_email_change
Create Date: 2026-09-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_forgot_password"
down_revision: str | None = "0013_pending_email_change"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("temp_password_hash", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column("temp_password_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "temp_password_expires_at")
    op.drop_column("users", "temp_password_hash")
