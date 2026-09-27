"""add Telegram alerts, alongside Slack

Projects get a bot token, a default chat and a mute switch; a site may override
the chat or bring its own bot, and remembers the outage's first message so the
follow-ups reply to it. Notification settings get a per-kind Telegram switch.
All of it is optional: existing projects keep alerting exactly as before.

Revision ID: 0026_telegram
Revises: 0025_login_lockout
Create Date: 2026-09-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_telegram"
down_revision: str | None = "0025_login_lockout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Text, not String: the value is a Fernet ciphertext, not the raw token.
    op.add_column("projects", sa.Column("telegram_bot_token", sa.Text(), nullable=True))
    op.add_column(
        "projects", sa.Column("telegram_chat_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "projects",
        sa.Column(
            "telegram_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "websites", sa.Column("telegram_chat_id", sa.String(length=64), nullable=True)
    )
    op.add_column("websites", sa.Column("telegram_bot_token", sa.Text(), nullable=True))
    op.add_column(
        "websites",
        sa.Column("telegram_thread_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "websites",
        sa.Column("telegram_thread_chat", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "notification_settings",
        sa.Column("telegram_enabled", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_settings", "telegram_enabled")
    op.drop_column("websites", "telegram_thread_chat")
    op.drop_column("websites", "telegram_thread_message_id")
    op.drop_column("websites", "telegram_bot_token")
    op.drop_column("websites", "telegram_chat_id")
    op.drop_column("projects", "telegram_enabled")
    op.drop_column("projects", "telegram_chat_id")
    op.drop_column("projects", "telegram_bot_token")
