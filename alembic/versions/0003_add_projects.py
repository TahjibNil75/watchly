"""add projects and attach websites to them

Revision ID: 0003_add_projects
Revises: 0002_create_monitoring_tables
Create Date: 2026-08-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_add_projects"
down_revision: str | None = "0002_create_monitoring_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK_NAME = "fk_websites_project_id_projects"
#: Websites registered before projects existed are parked here rather than
#: dropped, so the NOT NULL below cannot fail on an existing database.
ORPHAN_PROJECT_NAME = "Unassigned"


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("owner_id", sa.Integer(), nullable=True),
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
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_projects_owner_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("name", name=op.f("uq_projects_name")),
    )
    op.create_index(op.f("ix_projects_owner_id"), "projects", ["owner_id"])

    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_members_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_project_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", "user_id", name=op.f("pk_project_members")),
    )

    # Nullable first, so existing rows can be backfilled before the constraint.
    op.add_column("websites", sa.Column("project_id", sa.Integer(), nullable=True))

    connection = op.get_bind()
    orphans = connection.scalar(
        sa.text("SELECT count(*) FROM websites WHERE project_id IS NULL")
    )
    if orphans:
        project_id = connection.scalar(
            sa.text(
                "INSERT INTO projects (name, description) VALUES (:name, :note) "
                "RETURNING id"
            ),
            {
                "name": ORPHAN_PROJECT_NAME,
                "note": "Sites registered before projects existed. "
                "Move them to a real project and add its members.",
            },
        )
        connection.execute(
            sa.text("UPDATE websites SET project_id = :pid WHERE project_id IS NULL"),
            {"pid": project_id},
        )

    op.alter_column("websites", "project_id", nullable=False)
    op.create_index(op.f("ix_websites_project_id"), "websites", ["project_id"])
    op.create_foreign_key(
        FK_NAME, "websites", "projects", ["project_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    op.drop_constraint(FK_NAME, "websites", type_="foreignkey")
    op.drop_index(op.f("ix_websites_project_id"), table_name="websites")
    op.drop_column("websites", "project_id")
    op.drop_table("project_members")
    op.drop_index(op.f("ix_projects_owner_id"), table_name="projects")
    op.drop_table("projects")
