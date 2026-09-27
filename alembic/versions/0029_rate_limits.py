"""rate_limit_buckets: per-client request counts for the public endpoints

The per-account lockout stops anyone guessing one account's password, but not
one client trying a few passwords on every account, mailing temporary
passwords to a list of addresses, or opening accounts in bulk. Each row counts
one client's requests to one group of public endpoints in its current window.
Starts empty.

Revision ID: 0029_rate_limits
Revises: 0028_maintenance_windows
Create Date: 2026-09-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_rate_limits"
down_revision: str | None = "0028_maintenance_windows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_buckets",
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("client", sa.String(length=64), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("resets_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope", "client", name=op.f("pk_rate_limit_buckets")),
    )
    op.create_index(
        op.f("ix_rate_limit_buckets_resets_at"), "rate_limit_buckets", ["resets_at"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rate_limit_buckets_resets_at"), table_name="rate_limit_buckets")
    op.drop_table("rate_limit_buckets")
