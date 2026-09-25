"""website_check_hourly: checks summed per site per UTC hour, backfilled

Charts and long-range uptime read these rows, which are kept, while raw checks
are now purged after CHECK_RETENTION_DAYS. The backfill sums all existing
history, so it takes a while on a large `website_checks`.

Revision ID: 0017_check_hourly_rollup
Revises: 0016_failed_login_attempts
Create Date: 2026-09-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_check_hourly_rollup"
down_revision: str | None = "0016_failed_login_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of RESPONSE_BUCKETS_MS and HistoryService's rollup as they were
# when this ran, so later changes to either cannot change what it did.
_BUCKETS_MS = (
    0, 10, 12, 16, 20, 24, 31, 38, 48, 60, 75, 93, 116, 146, 182, 227, 284,
    355, 444, 555, 694, 867, 1084, 1355, 1694, 2118, 2647, 3309, 4136, 5170,
    6462, 8078, 10097, 12622, 15777, 19722, 24652, 30815, 38519, 48148, 60185,
)
_HISTOGRAM = ", ".join(
    f"count(*) FILTER (WHERE bucket = {n})" for n in range(1, len(_BUCKETS_MS) + 1)
)
_BACKFILL = f"""
INSERT INTO website_check_hourly
    (website_id, hour, checks, up_checks, timed_checks, sum_ms, max_ms, histogram)
SELECT website_id,
       hour,
       count(*),
       count(*) FILTER (WHERE is_up),
       count(bucket),
       coalesce(sum(response_time_ms) FILTER (WHERE bucket IS NOT NULL), 0),
       max(response_time_ms) FILTER (WHERE bucket IS NOT NULL),
       ARRAY[{_HISTOGRAM}]
FROM (
    SELECT website_id,
           date_trunc('hour', checked_at, 'UTC') AS hour,
           is_up,
           response_time_ms,
           CASE WHEN is_up AND response_time_ms IS NOT NULL
                THEN width_bucket(greatest(response_time_ms, 0),
                                  ARRAY[{", ".join(map(str, _BUCKETS_MS))}])
           END AS bucket
    FROM website_checks
) AS c
GROUP BY website_id, hour
"""


def upgrade() -> None:
    op.create_table(
        "website_check_hourly",
        sa.Column(
            "website_id",
            sa.Integer(),
            sa.ForeignKey("websites.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("hour", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("checks", sa.Integer(), nullable=False),
        sa.Column("up_checks", sa.Integer(), nullable=False),
        sa.Column("timed_checks", sa.Integer(), nullable=False),
        sa.Column("sum_ms", sa.BigInteger(), nullable=False),
        sa.Column("max_ms", sa.Integer(), nullable=True),
        sa.Column("histogram", sa.ARRAY(sa.Integer()), nullable=False),
    )
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.drop_table("website_check_hourly")
