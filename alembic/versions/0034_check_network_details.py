"""checks: redirect chain and body size; websites: cipher, ALPN and cert chain

Each HTTP check now keeps the redirects it followed and the size of what came
back. The certificate read also keeps the cipher suite, whether the server
speaks HTTP/2 (ALPN), and every certificate in the chain it sent.

The columns start empty and fill from the next check or certificate read.
Nothing is backfilled, and nothing alerts on them.

Revision ID: 0034_check_network_details
Revises: 0033_account_requests
Create Date: 2026-10-02

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_check_network_details"
down_revision: str | None = "0033_account_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_COLUMNS = (
    sa.Column("redirects", postgresql.JSONB(), nullable=True),
    sa.Column("content_length", sa.Integer(), nullable=True),
)
_WEBSITE_COLUMNS = (
    sa.Column("ssl_cipher", sa.String(96), nullable=True),
    sa.Column("ssl_alpn", sa.String(16), nullable=True),
    sa.Column("ssl_chain", postgresql.JSONB(), nullable=True),
)


def upgrade() -> None:
    for column in _CHECK_COLUMNS:
        op.add_column("website_checks", column)
    for column in _WEBSITE_COLUMNS:
        op.add_column("websites", column)


def downgrade() -> None:
    for column in reversed(_WEBSITE_COLUMNS):
        op.drop_column("websites", column.name)
    for column in reversed(_CHECK_COLUMNS):
        op.drop_column("website_checks", column.name)
