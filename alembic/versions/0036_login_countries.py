"""login_countries: the countries each user has signed in from

When COUNTRY_HEADER is set, each sign-in records the country the proxy in front
of Watchly reported, one row per user and country. A sign-in from a country
with no row yet (and the user has others) emails them. Starts empty.

Revision ID: 0036_login_countries
Revises: 0035_cdn_detection
Create Date: 2026-10-02

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_login_countries"
down_revision: str | None = "0035_cdn_detection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_countries",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sign_ins", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "country"),
    )


def downgrade() -> None:
    op.drop_table("login_countries")
