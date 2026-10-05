"""aws_accounts: several AWS accounts, each with its own credentials

A VPC now belongs to one AWS account, and every call about it is made with
that account's credentials: Watchly's own (the instance role, `default`) or a
stored access key, either of which may assume a role in the account. The
secret access key is encrypted at rest. VPCs registered before this live in
an account named "Watchly's own credentials", made here only if there are any.

Revision ID: 0041_aws_accounts
Revises: 0040_aws_scaling_notifications
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_aws_accounts"
down_revision: str | None = "0040_aws_scaling_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTH = ("default", "access_key")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*AUTH, name="aws_account_auth").create(bind, checkfirst=True)
    op.create_table(
        "aws_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "auth_type", postgresql.ENUM(*AUTH, name="aws_account_auth", create_type=False), nullable=False
        ),
        sa.Column("access_key_id", sa.String(length=128), nullable=True),
        sa.Column("secret_access_key", sa.Text(), nullable=True),
        sa.Column("role_arn", sa.String(length=2048), nullable=True),
        sa.Column("external_id", sa.String(length=1224), nullable=True),
        sa.Column("default_region", sa.String(length=32), nullable=True),
        sa.Column("aws_account_id", sa.String(length=12), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name=op.f("uq_aws_accounts_name")),
    )

    op.add_column("aws_vpcs", sa.Column("account_id", sa.Integer(), nullable=True))
    op.execute(
        """
        INSERT INTO aws_accounts (name, description, auth_type)
        SELECT 'Watchly''s own credentials',
               'The instance role of the server Watchly runs on.',
               'default'
        WHERE EXISTS (SELECT 1 FROM aws_vpcs)
        """
    )
    op.execute("UPDATE aws_vpcs SET account_id = (SELECT min(id) FROM aws_accounts)")
    op.alter_column("aws_vpcs", "account_id", nullable=False)
    op.create_foreign_key(
        "aws_vpcs_account_id_fkey",
        "aws_vpcs",
        "aws_accounts",
        ["account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_aws_vpcs_account_id"), "aws_vpcs", ["account_id"])
    op.drop_constraint("uq_aws_vpcs_vpc", "aws_vpcs", type_="unique")
    op.create_unique_constraint("uq_aws_vpcs_vpc", "aws_vpcs", ["account_id", "region", "aws_vpc_id"])


def downgrade() -> None:
    # Every VPC goes back to Watchly's own credentials; two accounts' VPCs
    # with the same id in the same region cannot both stay.
    op.drop_constraint("uq_aws_vpcs_vpc", "aws_vpcs", type_="unique")
    op.create_unique_constraint("uq_aws_vpcs_vpc", "aws_vpcs", ["region", "aws_vpc_id"])
    op.drop_index(op.f("ix_aws_vpcs_account_id"), table_name="aws_vpcs")
    op.drop_constraint("aws_vpcs_account_id_fkey", "aws_vpcs", type_="foreignkey")
    op.drop_column("aws_vpcs", "account_id")
    op.drop_table("aws_accounts")
    postgresql.ENUM(name="aws_account_auth").drop(op.get_bind(), checkfirst=True)
