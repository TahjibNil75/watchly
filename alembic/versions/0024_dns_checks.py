"""websites: DNS checks, watching one record of a domain at several resolvers

A site can now be a DNS check (`check_type` dns): the domain in `url`, the
record in `dns_record_type`, and optionally the values every resolver must
return. `dns_records` remembers what the resolvers last agreed on, so a change
can be noticed, and each check keeps every resolver's answer in `dns`.

`url` alone was unique. A domain can now be pinged and have several of its
records looked up at once, so uniqueness is over the url, the check type and
the record type, which is null except for DNS checks.

Revision ID: 0024_dns_checks
Revises: 0023_ping_checks
Create Date: 2026-09-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_dns_checks"
down_revision: str | None = "0023_ping_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT")


def upgrade() -> None:
    # A value added to an enum cannot be used in the transaction that adds it;
    # nothing below does.
    op.execute("ALTER TYPE check_type ADD VALUE IF NOT EXISTS 'dns'")

    postgresql.ENUM(*RECORD_TYPES, name="dns_record_type").create(
        op.get_bind(), checkfirst=True
    )
    op.add_column(
        "websites",
        sa.Column(
            "dns_record_type",
            postgresql.ENUM(*RECORD_TYPES, name="dns_record_type", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "websites",
        sa.Column(
            "dns_expected_values",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column("websites", sa.Column("dns_records", postgresql.ARRAY(sa.Text()), nullable=True))
    op.add_column("website_checks", sa.Column("dns", postgresql.JSONB(), nullable=True))

    # NULLS NOT DISTINCT (PostgreSQL 15+) so the null record type of every
    # other check still makes its url unique within its type.
    op.drop_constraint("websites_url_key", "websites", type_="unique")
    op.create_unique_constraint(
        "uq_websites_target",
        "websites",
        ["url", "check_type", "dns_record_type"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    # A DNS check has a bare domain where an HTTP site needs a URL, and may
    # share it with a ping check, so it cannot carry on as either: it and its
    # history go.
    op.execute("DELETE FROM websites WHERE check_type = 'dns'")

    op.drop_constraint("uq_websites_target", "websites", type_="unique")
    op.create_unique_constraint("websites_url_key", "websites", ["url"])

    op.drop_column("website_checks", "dns")
    for name in ("dns_records", "dns_expected_values", "dns_record_type"):
        op.drop_column("websites", name)
    postgresql.ENUM(name="dns_record_type").drop(op.get_bind(), checkfirst=True)

    # PostgreSQL cannot drop a value from an enum: rebuild the type without it.
    op.execute("ALTER TABLE websites ALTER COLUMN check_type DROP DEFAULT")
    op.execute("ALTER TYPE check_type RENAME TO check_type_old")
    op.execute("CREATE TYPE check_type AS ENUM ('http', 'ping')")
    op.execute(
        "ALTER TABLE websites ALTER COLUMN check_type TYPE check_type "
        "USING check_type::text::check_type"
    )
    op.execute("ALTER TABLE websites ALTER COLUMN check_type SET DEFAULT 'http'::check_type")
    op.execute("DROP TYPE check_type_old")
