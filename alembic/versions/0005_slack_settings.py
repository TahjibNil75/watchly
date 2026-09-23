"""add per-project Slack settings and a per-site channel override

Revision ID: 0005_slack_settings
Revises: 0004_project_extra_emails
Create Date: 2026-08-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_slack_settings"
down_revision: str | None = "0004_project_extra_emails"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Text, not String: the value is a Fernet ciphertext, not the raw token.
    op.add_column("projects", sa.Column("slack_bot_token", sa.Text(), nullable=True))
    op.add_column(
        "projects", sa.Column("slack_channel_id", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "projects",
        sa.Column(
            "slack_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "websites", sa.Column("slack_channel_id", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("websites", "slack_channel_id")
    op.drop_column("projects", "slack_enabled")
    op.drop_column("projects", "slack_channel_id")
    op.drop_column("projects", "slack_bot_token")
