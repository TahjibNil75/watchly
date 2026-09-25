"""users: a session version, so ending all sessions revokes access tokens too

Existing rows start at 0, which is also what an access token issued before this
counts as, so deploying it signs nobody out.

Revision ID: 0020_session_version
Revises: 0019_content_rules
Create Date: 2026-09-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_session_version"
down_revision: str | None = "0019_content_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "session_version", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "session_version")
