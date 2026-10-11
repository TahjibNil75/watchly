"""websites: mail checks, which log in to an SMTP server and read its quota

A site can now be a mail check (`check_type` mail): the SMTP server in `url`
as `host:port`, who runs it in `mail_provider` (smtp, ses, resend), how the
session is encrypted in `mail_tls` (starttls, implicit, none), and the login
in `mail_username` and `mail_password` (encrypted). For SES and Resend the
sending quota is read too: with `mail_api_key` (Resend, encrypted), or an AWS
account (`mail_aws_account_id`) or access key of the check's own (SES). The
last read is kept in `mail_quota`, what was warned about in
`mail_quota_alerts`, and each check keeps how its session went in `mail`.

Revision ID: 0049_mail_checks
Revises: 0048_docker_monitoring
Create Date: 2026-10-11

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049_mail_checks"
down_revision: str | None = "0048_docker_monitoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVIDERS = ("smtp", "ses", "resend")
TLS_MODES = ("starttls", "implicit", "none")


def upgrade() -> None:
    # A value added to an enum cannot be used in the transaction that adds it;
    # nothing below does.
    op.execute("ALTER TYPE check_type ADD VALUE IF NOT EXISTS 'mail'")

    postgresql.ENUM(*PROVIDERS, name="mail_provider").create(op.get_bind(), checkfirst=True)
    postgresql.ENUM(*TLS_MODES, name="mail_tls").create(op.get_bind(), checkfirst=True)
    op.add_column(
        "websites",
        sa.Column(
            "mail_provider",
            postgresql.ENUM(*PROVIDERS, name="mail_provider", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "websites",
        sa.Column(
            "mail_tls",
            postgresql.ENUM(*TLS_MODES, name="mail_tls", create_type=False),
            nullable=True,
        ),
    )
    op.add_column("websites", sa.Column("mail_username", sa.String(255), nullable=True))
    op.add_column("websites", sa.Column("mail_password", sa.Text(), nullable=True))
    op.add_column("websites", sa.Column("mail_api_key", sa.Text(), nullable=True))
    op.add_column(
        "websites",
        sa.Column(
            "mail_aws_account_id",
            sa.Integer(),
            sa.ForeignKey("aws_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("websites", sa.Column("mail_aws_access_key_id", sa.String(128), nullable=True))
    op.add_column("websites", sa.Column("mail_aws_secret_access_key", sa.Text(), nullable=True))
    op.add_column("websites", sa.Column("mail_region", sa.String(32), nullable=True))
    op.add_column("websites", sa.Column("mail_quota", postgresql.JSONB(), nullable=True))
    op.add_column(
        "websites",
        sa.Column("mail_quota_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("websites", sa.Column("mail_quota_error", sa.Text(), nullable=True))
    op.add_column(
        "websites",
        sa.Column(
            "mail_quota_alerts",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("website_checks", sa.Column("mail", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    # A mail check's `host:port` and login suit no other check type: it and
    # its history go.
    op.execute("DELETE FROM websites WHERE check_type::text = 'mail'")

    op.drop_column("website_checks", "mail")
    for column in (
        "mail_quota_alerts",
        "mail_quota_error",
        "mail_quota_checked_at",
        "mail_quota",
        "mail_region",
        "mail_aws_secret_access_key",
        "mail_aws_access_key_id",
        "mail_aws_account_id",
        "mail_api_key",
        "mail_password",
        "mail_username",
        "mail_tls",
        "mail_provider",
    ):
        op.drop_column("websites", column)
    postgresql.ENUM(name="mail_tls").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="mail_provider").drop(op.get_bind(), checkfirst=True)

    # PostgreSQL cannot drop a value from an enum: rebuild the type without it.
    op.execute("ALTER TABLE websites ALTER COLUMN check_type DROP DEFAULT")
    op.execute("ALTER TYPE check_type RENAME TO check_type_old")
    op.execute("CREATE TYPE check_type AS ENUM ('http', 'ping', 'dns', 'database')")
    op.execute(
        "ALTER TABLE websites ALTER COLUMN check_type TYPE check_type "
        "USING check_type::text::check_type"
    )
    op.execute("ALTER TABLE websites ALTER COLUMN check_type SET DEFAULT 'http'::check_type")
    op.execute("DROP TYPE check_type_old")
