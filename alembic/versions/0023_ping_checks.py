"""websites: ping checks, with packet loss and round trips

A site can now be a host to ping (`check_type` ping) instead of a URL to
request; it keeps the host name or IP address in `url`. Existing sites become
`http`, which is what they are. Each ping check records its packets and round
trips, and the hourly rollups sum the packets so packet loss can be charted.

Revision ID: 0023_ping_checks
Revises: 0022_slack_threads
Create Date: 2026-09-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_ping_checks"
down_revision: str | None = "0022_slack_threads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK_TYPES = ("http", "ping")


def upgrade() -> None:
    # add_column does not create a native enum's type the way create_table
    # does, so create it explicitly and tell the column not to.
    postgresql.ENUM(*CHECK_TYPES, name="check_type").create(op.get_bind(), checkfirst=True)
    op.add_column(
        "websites",
        sa.Column(
            "check_type",
            postgresql.ENUM(*CHECK_TYPES, name="check_type", create_type=False),
            server_default=sa.text("'http'::check_type"),
            nullable=False,
        ),
    )
    op.add_column(
        "websites",
        sa.Column("ping_count", sa.Integer(), server_default=sa.text("5"), nullable=False),
    )
    op.add_column(
        "websites", sa.Column("packet_loss_threshold_percent", sa.Integer(), nullable=True)
    )
    op.add_column(
        "websites",
        sa.Column("loss_streak", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "websites",
        sa.Column("last_loss_alert_at", sa.DateTime(timezone=True), nullable=True),
    )

    for name in ("packets_sent", "packets_received"):
        op.add_column("website_checks", sa.Column(name, sa.Integer(), nullable=True))
    for name in ("rtt_min_ms", "rtt_avg_ms", "rtt_max_ms", "jitter_ms"):
        op.add_column("website_checks", sa.Column(name, sa.Float(), nullable=True))
    op.add_column("website_checks", sa.Column("ip_address", sa.String(64), nullable=True))

    # 0 is right for every existing hour: nothing was pinged before this.
    for name in ("packets_sent", "packets_received"):
        op.add_column(
            "website_check_hourly",
            sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False),
        )


def downgrade() -> None:
    # A pinged host has a host name where an HTTP site needs a URL, so it cannot
    # carry on as one: it and its history go.
    op.execute("DELETE FROM websites WHERE check_type = 'ping'")

    for name in ("packets_received", "packets_sent"):
        op.drop_column("website_check_hourly", name)

    for name in (
        "ip_address",
        "jitter_ms",
        "rtt_max_ms",
        "rtt_avg_ms",
        "rtt_min_ms",
        "packets_received",
        "packets_sent",
    ):
        op.drop_column("website_checks", name)

    for name in (
        "last_loss_alert_at",
        "loss_streak",
        "packet_loss_threshold_percent",
        "ping_count",
        "check_type",
    ):
        op.drop_column("websites", name)
    postgresql.ENUM(name="check_type").drop(op.get_bind(), checkfirst=True)
