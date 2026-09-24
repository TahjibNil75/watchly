"""users: an email change waiting for the new address to be confirmed

Revision ID: 0013_pending_email_change
Revises: 0012_check_details
Create Date: 2026-09-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_pending_email_change"
down_revision: str | None = "0012_check_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("pending_email", sa.String(length=255), nullable=True))
    op.add_column(
        "users", sa.Column("email_token_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column("email_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        op.f("uq_users_email_token_hash"), "users", ["email_token_hash"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_users_email_token_hash"), "users", type_="unique")
    op.drop_column("users", "email_token_expires_at")
    op.drop_column("users", "email_token_hash")
    op.drop_column("users", "pending_email")
