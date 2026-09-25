"""website_events: outages, recoveries, slow spells and expiring certificates,
kept for the app's own alert feed

Until now an event was sent by email, Slack or webhook and then forgotten, so
someone signing in had no way to learn what happened while they were away.
Starts empty: nothing before this migration was recorded.

Revision ID: 0021_website_events
Revises: 0020_session_version
Create Date: 2026-09-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_website_events"
down_revision: str | None = "0020_session_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "website_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("response_time_ms", sa.Integer(), nullable=True),
        sa.Column("downtime_seconds", sa.Integer(), nullable=True),
        sa.Column("threshold_ms", sa.Integer(), nullable=True),
        sa.Column("ssl_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_website_events_website_id", "website_events", ["website_id"])
    op.create_index("ix_website_events_occurred_at", "website_events", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_website_events_occurred_at", table_name="website_events")
    op.drop_index("ix_website_events_website_id", table_name="website_events")
    op.drop_table("website_events")
