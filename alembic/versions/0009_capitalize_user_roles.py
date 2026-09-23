"""capitalize the user_role labels

`UserRole` values went from `viewer` / `admin` / `project manager` / `developer`
to `Viewer` / `Admin` / `Project Manager` / `Developer` (`DevOps` was already
capitalized). The Postgres enum has to follow, or the app cannot read a single
user row.

`ALTER TYPE ... RENAME VALUE` renames the label in place. Rows store the
label's identity rather than its text, so existing users keep their roles, and
so does the column default (`'viewer'::user_role` becomes `'Viewer'`). It runs
inside a transaction, unlike ADD VALUE on older Postgres.

Revision ID: 0009_capitalize_user_roles
Revises: 0008_website_environment
Create Date: 2026-09-24

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0009_capitalize_user_roles"
down_revision: str | None = "0008_website_environment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: old label -> new label. `DevOps` is unchanged, so it is not listed.
RENAMES = {
    "viewer": "Viewer",
    "admin": "Admin",
    "project manager": "Project Manager",
    "developer": "Developer",
}


def upgrade() -> None:
    for old, new in RENAMES.items():
        op.execute(f"ALTER TYPE user_role RENAME VALUE '{old}' TO '{new}'")


def downgrade() -> None:
    for old, new in RENAMES.items():
        op.execute(f"ALTER TYPE user_role RENAME VALUE '{new}' TO '{old}'")
