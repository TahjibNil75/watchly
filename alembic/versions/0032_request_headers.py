"""websites: headers an HTTP check sends with its request

Each value is encrypted by the app before it is stored, like a bot token.

Revision ID: 0032_request_headers
Revises: 0031_nameservers_sec_headers
Create Date: 2026-09-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032_request_headers"
down_revision: str | None = "0031_nameservers_sec_headers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "websites",
        sa.Column(
            "request_headers",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("websites", "request_headers")
