"""add WhatsApp alerts, alongside Slack and Telegram

Projects get a WhatsApp Cloud API sender (access token and phone number id),
the numbers to alert, and a mute switch; a site may alert numbers of its own,
still from its project's business number. Notification settings get a per-kind
WhatsApp switch. All of it is optional: existing projects keep alerting exactly
as before.

Revision ID: 0027_whatsapp
Revises: 0026_telegram
Create Date: 2026-09-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_whatsapp"
down_revision: str | None = "0026_telegram"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Text, not String: the value is a Fernet ciphertext, not the raw token.
    op.add_column(
        "projects", sa.Column("whatsapp_access_token", sa.Text(), nullable=True)
    )
    op.add_column(
        "projects",
        sa.Column("whatsapp_phone_number_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "whatsapp_recipients",
            postgresql.ARRAY(sa.String(length=16)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "whatsapp_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "websites",
        sa.Column(
            "whatsapp_recipients",
            postgresql.ARRAY(sa.String(length=16)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "notification_settings",
        sa.Column("whatsapp_enabled", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_settings", "whatsapp_enabled")
    op.drop_column("websites", "whatsapp_recipients")
    op.drop_column("projects", "whatsapp_enabled")
    op.drop_column("projects", "whatsapp_recipients")
    op.drop_column("projects", "whatsapp_phone_number_id")
    op.drop_column("projects", "whatsapp_access_token")
