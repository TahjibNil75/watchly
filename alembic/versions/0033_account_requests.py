"""account requests: ask for an account from the sign-in page

An admin or DevOps user approves one, which sends an invitation as a Viewer,
or rejects it.

Revision ID: 0033_account_requests
Revises: 0032_request_headers
Create Date: 2026-10-01

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_account_requests"
down_revision: str | None = "0032_request_headers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"],
            ["users.id"],
            name=op.f("fk_account_requests_decided_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_requests")),
    )
    op.create_index(op.f("ix_account_requests_email"), "account_requests", ["email"])
    op.create_index(
        "uq_account_requests_open_email",
        "account_requests",
        ["email"],
        unique=True,
        postgresql_where=sa.text("approved_at IS NULL AND rejected_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_account_requests_open_email", table_name="account_requests")
    op.drop_index(op.f("ix_account_requests_email"), table_name="account_requests")
    op.drop_table("account_requests")
