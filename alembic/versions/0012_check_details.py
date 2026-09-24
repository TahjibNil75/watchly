"""keep more of each check: why it failed, and where its time went

Until now a check kept only its status, time, error and final URL; the error
class, reason phrase and diagnostic headers reached the alert and were then
lost. Storing error_type lets incidents be grouped by cause, and the per-step
times let a slow check say which step was slow.

Revision ID: 0012_check_details
Revises: 0011_website_slack_token
Create Date: 2026-09-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_check_details"
down_revision: str | None = "0011_website_slack_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMINGS = ("dns_ms", "connect_ms", "tls_ms", "first_byte_ms")


def upgrade() -> None:
    # All nullable with no default, so existing rows need no rewrite: they
    # simply have none of this, which is the truth.
    op.add_column("website_checks", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column(
        "website_checks", sa.Column("error_type", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "website_checks",
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    for name in _TIMINGS:
        op.add_column("website_checks", sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    for name in reversed(_TIMINGS):
        op.drop_column("website_checks", name)
    op.drop_column("website_checks", "headers")
    op.drop_column("website_checks", "error_type")
    op.drop_column("website_checks", "reason")
