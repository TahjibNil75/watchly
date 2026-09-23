"""add per-site recipients and websites.inherit_project_recipients

Users alerted about one website only, and a switch that stops a site emailing
its project's members and extra_emails.

Revision ID: 0006_website_recipients
Revises: 0005_slack_settings
Create Date: 2026-09-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_website_recipients"
down_revision: str | None = "0005_slack_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "website_recipients",
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name=op.f("fk_website_recipients_website_id_websites"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_website_recipients_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "website_id", "user_id", name=op.f("pk_website_recipients")
        ),
    )
    # Default true keeps every existing site alerting exactly as before.
    op.add_column(
        "websites",
        sa.Column(
            "inherit_project_recipients",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("websites", "inherit_project_recipients")
    op.drop_table("website_recipients")
