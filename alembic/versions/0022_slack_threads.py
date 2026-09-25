"""websites: remember the Slack thread of the current outage

Still-down alerts now reply under the outage's first Slack message instead of
posting to the channel again, and the recovery replies there too (also shown in
the channel). Sites that are down while this runs have no thread yet; their
next alert starts one.

Revision ID: 0022_slack_threads
Revises: 0021_website_events
Create Date: 2026-09-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_slack_threads"
down_revision: str | None = "0021_website_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "websites", sa.Column("slack_thread_ts", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "websites", sa.Column("slack_thread_channel", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("websites", "slack_thread_channel")
    op.drop_column("websites", "slack_thread_ts")
