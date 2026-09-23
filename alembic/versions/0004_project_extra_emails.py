"""add projects.extra_emails

Alert addresses that belong to a whole project but are not user accounts —
a client contact, a shared on-call inbox.

Revision ID: 0004_project_extra_emails
Revises: 0003_add_projects
Create Date: 2026-08-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_project_extra_emails"
down_revision: str | None = "0003_add_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "extra_emails",
            postgresql.ARRAY(sa.String(length=255)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "extra_emails")
