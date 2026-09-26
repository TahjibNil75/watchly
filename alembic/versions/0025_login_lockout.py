"""users: lock sign-in for a while after too many wrong passwords

Too many wrong passwords used to suspend the account, which only someone
allowed to reinstate it could undo, so anyone who knew the admin's username
could lock them out. Now they lock sign-in until `locked_until`, which runs
out by itself. Accounts already suspended that way stay suspended.

Revision ID: 0025_login_lockout
Revises: 0024_dns_checks
Create Date: 2026-09-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_login_lockout"
down_revision: str | None = "0024_dns_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "locked_until")
