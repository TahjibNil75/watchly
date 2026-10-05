"""aws_resources: opt-in scaling notifications for Auto Scaling groups

An Auto Scaling group can notify when instances are added (scale-out) or
removed (scale-in); both are off by default. `scale_health_check` is an
optional endpoint probed on each newly launched instance, its result carried
in the scale-out notification. `scaling_members` is the instance ids last seen
in service, which the next sync is compared with; null until the first sync
after the group's notifications are looked at.

Revision ID: 0040_aws_scaling_notifications
Revises: 0039_aws_resources_checks
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_aws_scaling_notifications"
down_revision: str | None = "0039_aws_resources_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aws_resources",
        sa.Column("notify_scale_out", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "aws_resources",
        sa.Column("notify_scale_in", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "aws_resources", sa.Column("scale_health_check", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        "aws_resources", sa.Column("scaling_members", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("aws_resources", "scaling_members")
    op.drop_column("aws_resources", "scale_health_check")
    op.drop_column("aws_resources", "notify_scale_in")
    op.drop_column("aws_resources", "notify_scale_out")
