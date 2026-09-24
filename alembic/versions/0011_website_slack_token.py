"""add websites.slack_bot_token, so a site can set up Slack of its own

Until now a site could only redirect its project's Slack to another channel.
With its own token it can post to Slack even when the project has none.

Revision ID: 0011_website_slack_token
Revises: 0010_user_invitations
Create Date: 2026-09-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_website_slack_token"
down_revision: str | None = "0010_user_invitations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Text, not String: the value is a Fernet ciphertext, not the raw token.
    op.add_column("websites", sa.Column("slack_bot_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("websites", "slack_bot_token")
