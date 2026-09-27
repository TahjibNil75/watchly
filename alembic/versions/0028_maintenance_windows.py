"""maintenance_windows: stretches of time when a site is expected to fail

A deployment takes a site down for a minute or two, and until now that paged
everyone. A window, started by hand or scheduled ahead, stops the site's
checks and alerts until it ends. Starts empty: every site is monitored exactly
as before until someone adds one.

Revision ID: 0028_maintenance_windows
Revises: 0027_whatsapp
Create Date: 2026-09-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_maintenance_windows"
down_revision: str | None = "0027_whatsapp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_windows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("ends_at > starts_at", name="ck_maintenance_windows_order"),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_maintenance_windows_site_end",
        "maintenance_windows",
        ["website_id", "ends_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_maintenance_windows_site_end", table_name="maintenance_windows")
    op.drop_table("maintenance_windows")
