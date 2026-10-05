"""websites: database checks, which open a session without logging in

A site can now be a database check (`check_type` database): the endpoint in
`url` as `host:port`, the protocol in `db_engine` (postgresql, mysql, redis,
mongodb), and for Redis and MongoDB whether to use TLS from the start in
`db_tls`. Each check keeps what the server said in `database`.

Revision ID: 0045_database_checks
Revises: 0044_aws_databases
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045_database_checks"
down_revision: str | None = "0044_aws_databases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENGINES = ("postgresql", "mysql", "redis", "mongodb")


def upgrade() -> None:
    # A value added to an enum cannot be used in the transaction that adds it;
    # nothing below does.
    op.execute("ALTER TYPE check_type ADD VALUE IF NOT EXISTS 'database'")

    postgresql.ENUM(*ENGINES, name="db_engine").create(op.get_bind(), checkfirst=True)
    op.add_column(
        "websites",
        sa.Column(
            "db_engine",
            postgresql.ENUM(*ENGINES, name="db_engine", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "websites",
        sa.Column("db_tls", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("website_checks", sa.Column("database", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    # A database check's `host:port` suits no other check type: it and its
    # history go.
    op.execute("DELETE FROM websites WHERE check_type = 'database'")

    op.drop_column("website_checks", "database")
    op.drop_column("websites", "db_tls")
    op.drop_column("websites", "db_engine")
    postgresql.ENUM(name="db_engine").drop(op.get_bind(), checkfirst=True)

    # PostgreSQL cannot drop a value from an enum: rebuild the type without it.
    op.execute("ALTER TABLE websites ALTER COLUMN check_type DROP DEFAULT")
    op.execute("ALTER TYPE check_type RENAME TO check_type_old")
    op.execute("CREATE TYPE check_type AS ENUM ('http', 'ping', 'dns')")
    op.execute(
        "ALTER TABLE websites ALTER COLUMN check_type TYPE check_type "
        "USING check_type::text::check_type"
    )
    op.execute("ALTER TABLE websites ALTER COLUMN check_type SET DEFAULT 'http'::check_type")
    op.execute("DROP TYPE check_type_old")
