"""websites: domain nameservers, and the security headers of the last good check

The domain lookup now also keeps the nameservers the registry delegates the
domain to, and alerts when they change. Each successful HTTP check keeps the
raw values of five security headers, graded when read.

The columns start empty. Forgetting when each domain was looked up makes the
next check look it up again and learn its nameservers now rather than within
a day; its expiry warnings stay where they were, so nobody is warned twice.

Revision ID: 0031_nameservers_sec_headers
Revises: 0030_ssl_details_domain_expiry
Create Date: 2026-09-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031_nameservers_sec_headers"
down_revision: str | None = "0030_ssl_details_domain_expiry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    sa.Column("domain_nameservers", postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column("domain_nameservers_changed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("security_headers", postgresql.JSONB(), nullable=True),
    sa.Column("security_checked_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("websites", column)
    op.execute("UPDATE websites SET domain_checked_at = NULL")


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("websites", column.name)
