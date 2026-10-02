"""websites: the server behind a site; hourly rollups: time spent per step

A site now keeps where it is served from (the address, its network and reverse
name) in `server`, read from the next successful HTTP check.

The hourly rollup also sums how long the DNS lookup, TCP connect, TLS
handshake and wait for the first byte took, with how many checks performed
each step (a reused connection skips the first three), so History can chart
where the time goes after the raw checks are purged. Existing hours are
filled in from the raw checks still held; hours already purged keep zero
counts and show no breakdown.

Revision ID: 0037_server_info_step_timings
Revises: 0036_login_countries
Create Date: 2026-10-03

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037_server_info_step_timings"
down_revision: str | None = "0036_login_countries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STEPS = ("dns", "connect", "tls", "first_byte")
_HOURLY_COLUMNS = [
    sa.Column(
        f"{kind}_{step}{'_ms' if kind == 'sum' else ''}",
        sa.BigInteger() if kind == "sum" else sa.Integer(),
        nullable=False,
        server_default=sa.text("0"),
    )
    for step in _STEPS
    for kind in ("sum", "n")
]

# Successful checks only, as the response-time figures are.
_BACKFILL = f"""
UPDATE website_check_hourly AS h
SET {", ".join(f"sum_{s}_ms = c.sum_{s}_ms, n_{s} = c.n_{s}" for s in _STEPS)}
FROM (
    SELECT website_id,
           date_trunc('hour', checked_at, 'UTC') AS hour,
           {", ".join(
               f"coalesce(sum({s}_ms) FILTER (WHERE is_up), 0) AS sum_{s}_ms, "
               f"count({s}_ms) FILTER (WHERE is_up) AS n_{s}"
               for s in _STEPS
           )}
    FROM website_checks
    GROUP BY website_id, hour
) AS c
WHERE h.website_id = c.website_id AND h.hour = c.hour
"""


def upgrade() -> None:
    op.add_column("websites", sa.Column("server", postgresql.JSONB(), nullable=True))
    op.add_column(
        "websites", sa.Column("server_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    for column in _HOURLY_COLUMNS:
        op.add_column("website_check_hourly", column)
    op.execute(_BACKFILL)


def downgrade() -> None:
    for column in reversed(_HOURLY_COLUMNS):
        op.drop_column("website_check_hourly", column.name)
    op.drop_column("websites", "server_checked_at")
    op.drop_column("websites", "server")
