"""projects.monitors; AWS accounts belong to an infrastructure project

A project now monitors either websites or infrastructure, chosen when it is
created. An infrastructure project owns its AWS accounts (at least one), and a
VPC belongs to the project of its account, so `aws_vpc_projects` goes.

Existing data: each account goes to a project that used it, through a VPC
granted to the project or a resource in one. An account used by several
projects is copied for each, with copies of the VPCs that project used, and
its resources and events move to the copies. A VPC no project used is
dropped, and an account no project used goes with it. A project with AWS
accounts and no websites becomes `infrastructure`; every other stays
`websites`.

Account names are now unique per project, and VPC names per account. A
project's deletion cascades to its accounts, their VPCs and resources.

Revision ID: 0042_project_monitors
Revises: 0041_aws_accounts
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042_project_monitors"
down_revision: str | None = "0041_aws_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONITORS = ("websites", "infrastructure")

ACCOUNT_COLUMNS = (
    "name, description, auth_type, access_key_id, secret_access_key, role_arn, external_id, "
    "default_region, aws_account_id, verified_at, last_error, created_by_id"
)
VPC_COLUMNS = (
    "name, description, region, aws_vpc_id, cidrs, is_watchly_vpc, unreachable_since, synced_at, "
    "last_test_at, last_test, created_by_id"
)


def _move_accounts_to_projects(bind) -> None:
    run = lambda sql, **params: bind.execute(sa.text(sql), params)  # noqa: E731
    for (account_id,) in run("SELECT id FROM aws_accounts ORDER BY id").all():
        vpc_ids = [row[0] for row in run("SELECT id FROM aws_vpcs WHERE account_id = :a", a=account_id)]
        pairs = set()
        if vpc_ids:
            pairs = {
                (vpc, project)
                for vpc, project in run(
                    "SELECT vpc_id, project_id FROM aws_vpc_projects WHERE vpc_id = ANY(:v) "
                    "UNION SELECT DISTINCT vpc_id, project_id FROM aws_resources WHERE vpc_id = ANY(:v)",
                    v=vpc_ids,
                )
            }
        projects = sorted({project for _, project in pairs})
        if not projects:
            run("DELETE FROM aws_vpcs WHERE account_id = :a", a=account_id)
            run("DELETE FROM aws_accounts WHERE id = :a", a=account_id)
            continue
        first, *others = projects
        run("UPDATE aws_accounts SET project_id = :p WHERE id = :a", p=first, a=account_id)
        for project in others:
            copy = run(
                f"INSERT INTO aws_accounts (project_id, {ACCOUNT_COLUMNS}) "
                f"SELECT :p, {ACCOUNT_COLUMNS} FROM aws_accounts WHERE id = :a RETURNING id",
                p=project,
                a=account_id,
            ).scalar_one()
            for vpc in vpc_ids:
                if (vpc, project) not in pairs:
                    continue
                vpc_copy = run(
                    f"INSERT INTO aws_vpcs (account_id, {VPC_COLUMNS}) "
                    f"SELECT :c, {VPC_COLUMNS} FROM aws_vpcs WHERE id = :v RETURNING id",
                    c=copy,
                    v=vpc,
                ).scalar_one()
                for table in ("aws_resources", "aws_events"):
                    run(
                        f"UPDATE {table} SET vpc_id = :n WHERE vpc_id = :v AND project_id = :p",
                        n=vpc_copy,
                        v=vpc,
                        p=project,
                    )
        for vpc in vpc_ids:
            if (vpc, first) not in pairs:
                run("DELETE FROM aws_vpcs WHERE id = :v", v=vpc)
    run(
        "UPDATE projects SET monitors = 'infrastructure' "
        "WHERE id IN (SELECT project_id FROM aws_accounts) "
        "AND NOT EXISTS (SELECT 1 FROM websites WHERE websites.project_id = projects.id)"
    )


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*MONITORS, name="project_monitors").create(bind, checkfirst=True)
    op.add_column(
        "projects",
        sa.Column(
            "monitors",
            postgresql.ENUM(*MONITORS, name="project_monitors", create_type=False),
            server_default="websites",
            nullable=False,
        ),
    )

    # Names are unique per project and per account from here; copies share them.
    op.drop_constraint("uq_aws_accounts_name", "aws_accounts", type_="unique")
    op.drop_constraint("uq_aws_vpcs_name", "aws_vpcs", type_="unique")
    op.add_column("aws_accounts", sa.Column("project_id", sa.Integer(), nullable=True))

    _move_accounts_to_projects(bind)

    op.alter_column("aws_accounts", "project_id", nullable=False)
    op.create_foreign_key(
        "aws_accounts_project_id_fkey", "aws_accounts", "projects", ["project_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index(op.f("ix_aws_accounts_project_id"), "aws_accounts", ["project_id"])
    op.create_unique_constraint("uq_aws_accounts_project_name", "aws_accounts", ["project_id", "name"])
    op.create_unique_constraint("uq_aws_vpcs_account_name", "aws_vpcs", ["account_id", "name"])
    op.drop_table("aws_vpc_projects")

    # Deleting a project removes its accounts, their VPCs and resources. The
    # service still refuses to remove an account or VPC that is in use.
    for table, column, target in (("aws_vpcs", "account_id", "aws_accounts"), ("aws_resources", "vpc_id", "aws_vpcs")):
        op.drop_constraint(f"{table}_{column}_fkey", table, type_="foreignkey")
        op.create_foreign_key(f"{table}_{column}_fkey", table, target, [column], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    for table, column, target in (("aws_vpcs", "account_id", "aws_accounts"), ("aws_resources", "vpc_id", "aws_vpcs")):
        op.drop_constraint(f"{table}_{column}_fkey", table, type_="foreignkey")
        op.create_foreign_key(f"{table}_{column}_fkey", table, target, [column], ["id"], ondelete="RESTRICT")

    op.create_table(
        "aws_vpc_projects",
        sa.Column("vpc_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["vpc_id"], ["aws_vpcs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("vpc_id", "project_id"),
    )
    op.execute(
        "INSERT INTO aws_vpc_projects (vpc_id, project_id) "
        "SELECT v.id, a.project_id FROM aws_vpcs v JOIN aws_accounts a ON a.id = v.account_id"
    )
    op.drop_constraint("uq_aws_vpcs_account_name", "aws_vpcs", type_="unique")
    op.drop_constraint("uq_aws_accounts_project_name", "aws_accounts", type_="unique")
    # Names were unique everywhere before: suffix the ones that now clash.
    for table in ("aws_vpcs", "aws_accounts"):
        op.execute(
            f"UPDATE {table} t SET name = t.name || ' #' || t.id "
            f"WHERE EXISTS (SELECT 1 FROM {table} o WHERE o.name = t.name AND o.id < t.id)"
        )
    op.create_unique_constraint("uq_aws_vpcs_name", "aws_vpcs", ["name"])
    op.create_unique_constraint("uq_aws_accounts_name", "aws_accounts", ["name"])
    op.drop_index(op.f("ix_aws_accounts_project_id"), table_name="aws_accounts")
    op.drop_constraint("aws_accounts_project_id_fkey", "aws_accounts", type_="foreignkey")
    op.drop_column("aws_accounts", "project_id")
    op.drop_column("projects", "monitors")
    postgresql.ENUM(name="project_monitors").drop(op.get_bind(), checkfirst=True)
