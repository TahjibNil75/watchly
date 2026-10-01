"""websites: the CDN in front of the site, if one is found

Each HTTP site is read for CDN traces (response headers and the host's DNS
aliases) the first time it answers and every CDN_CHECK_INTERVAL_SECONDS after.

The columns start empty and fill from the next successful check. Nothing alerts
on them.

Revision ID: 0035_cdn_detection
Revises: 0034_check_network_details
Create Date: 2026-10-02

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035_cdn_detection"
down_revision: str | None = "0034_check_network_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    sa.Column("cdn", postgresql.JSONB(), nullable=True),
    sa.Column("cdn_checked_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("websites", column)


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("websites", column.name)
