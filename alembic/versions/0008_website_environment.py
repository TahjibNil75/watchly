"""add websites.environment

Which deployment a site is — development, testing, uat, staging or production.
Nullable: sites registered before this migration have no environment until
someone sets one.

Revision ID: 0008_website_environment
Revises: 0007_notifications
Create Date: 2026-09-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_website_environment"
down_revision: str | None = "0007_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENVIRONMENTS = ("development", "testing", "uat", "staging", "production")


def upgrade() -> None:
    # add_column does not create a native enum's type the way create_table
    # does, so create it explicitly and tell the column not to.
    postgresql.ENUM(*ENVIRONMENTS, name="website_environment").create(
        op.get_bind(), checkfirst=True
    )
    op.add_column(
        "websites",
        sa.Column(
            "environment",
            postgresql.ENUM(*ENVIRONMENTS, name="website_environment", create_type=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("websites", "environment")
    postgresql.ENUM(name="website_environment").drop(op.get_bind(), checkfirst=True)
