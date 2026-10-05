"""aws_vpcs, aws_vpc_projects: where infrastructure checks may reach

Infrastructure monitoring watches AWS resources, apart from websites. A VPC is
registered by its id, with the CIDRs AWS lists for it, and granted to
projects. Starts empty.

Revision ID: 0038_aws_vpcs
Revises: 0037_server_info_step_timings
Create Date: 2026-10-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038_aws_vpcs"
down_revision: str | None = "0037_server_info_step_timings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aws_vpcs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("aws_vpc_id", sa.String(length=32), nullable=False),
        sa.Column("cidrs", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("is_watchly_vpc", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("unreachable_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name=op.f("uq_aws_vpcs_name")),
        sa.UniqueConstraint("region", "aws_vpc_id", name="uq_aws_vpcs_vpc"),
    )

    op.create_table(
        "aws_vpc_projects",
        sa.Column("vpc_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["vpc_id"], ["aws_vpcs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("vpc_id", "project_id"),
    )


def downgrade() -> None:
    op.drop_table("aws_vpc_projects")
    op.drop_table("aws_vpcs")
