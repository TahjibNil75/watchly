"""websites: certificate details and domain registration; website_events: domain expiry

The certificate read already parsed the whole certificate but kept only its
end date. The site's page now also shows who it was issued to and by, the
names it covers, when it began and the TLS version, so those are kept too.

Each site's domain registration is looked up over RDAP, like the
certificate, on its own interval, and warns before it lapses. The feed
records those warnings with the end date, as it does a certificate's.

All columns start empty: the next certificate read and domain lookup fill
them in.

Revision ID: 0030_ssl_details_domain_expiry
Revises: 0029_rate_limits
Create Date: 2026-09-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_ssl_details_domain_expiry"
down_revision: str | None = "0029_rate_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WEBSITE_COLUMNS = (
    sa.Column("ssl_subject", sa.String(length=255), nullable=True),
    sa.Column("ssl_issuer", sa.String(length=255), nullable=True),
    sa.Column("ssl_sans", postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column("ssl_valid_from", sa.DateTime(timezone=True), nullable=True),
    sa.Column("ssl_tls_version", sa.String(length=16), nullable=True),
    sa.Column("domain_name", sa.String(length=255), nullable=True),
    sa.Column("domain_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("domain_registrar", sa.String(length=255), nullable=True),
    sa.Column("domain_checked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("domain_error", sa.Text(), nullable=True),
    sa.Column("domain_alert_bucket", sa.Integer(), nullable=True),
)


def upgrade() -> None:
    for column in _WEBSITE_COLUMNS:
        op.add_column("websites", column)
    op.add_column(
        "website_events",
        sa.Column("domain_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The existing certificate state is kept, but without these details it
    # would wait up to SSL_CHECK_INTERVAL_SECONDS to fill them. Forgetting
    # when it was read makes the next check read it again; the alert bucket
    # stays, so nobody is warned twice.
    op.execute("UPDATE websites SET ssl_checked_at = NULL")


def downgrade() -> None:
    op.drop_column("website_events", "domain_expires_at")
    for column in reversed(_WEBSITE_COLUMNS):
        op.drop_column("websites", column.name)
