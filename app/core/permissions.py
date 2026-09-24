"""Who may administer whom.

One module so the rules live in a single readable place instead of being
scattered across routes. Pure functions over roles — no HTTP, no database.
"""

from app.db.models.user import UserRole

#: Roles that may change *other users' roles*.
ROLE_MANAGERS: frozenset[UserRole] = frozenset({UserRole.ADMIN, UserRole.DEVOPS})

#: Roles with any user-management power at all. These may read the user
#: directory; what each may actually do to a given user is decided by
#: :func:`can_change_role` and :func:`can_suspend_user`.
USER_MANAGERS: frozenset[UserRole] = ROLE_MANAGERS | {UserRole.PROJECT_MANAGER}

#: Which roles each actor may suspend or reinstate. Absent actor => none.
#: Note this is a table, not a hierarchy: DevOps and project managers can each
#: suspend the other.
SUSPENDABLE_BY: dict[UserRole, frozenset[UserRole]] = {
    UserRole.ADMIN: frozenset(UserRole),
    UserRole.DEVOPS: frozenset(
        {UserRole.VIEWER, UserRole.DEVELOPER, UserRole.PROJECT_MANAGER}
    ),
    UserRole.PROJECT_MANAGER: frozenset(
        {UserRole.DEVOPS, UserRole.DEVELOPER, UserRole.VIEWER}
    ),
}

#: Which roles each actor may hand out when inviting someone. Absent actor =>
#: may not invite at all. A table like SUSPENDABLE_BY, so adding a row is all it
#: takes to let another role invite — including the invitation endpoints' guard,
#: which is built from these keys.
INVITABLE_BY: dict[UserRole, frozenset[UserRole]] = {
    UserRole.ADMIN: frozenset(UserRole),
    UserRole.DEVOPS: frozenset(
        {
            UserRole.VIEWER,
            UserRole.DEVELOPER,
            UserRole.PROJECT_MANAGER,
            UserRole.DEVOPS,
        }
    ),
}

#: Roles that may send and manage invitations.
INVITERS: frozenset[UserRole] = frozenset(INVITABLE_BY)


#: Roles that may create monitoring projects and register sites under them.
PROJECT_CREATORS: frozenset[UserRole] = frozenset(
    {UserRole.ADMIN, UserRole.DEVOPS, UserRole.PROJECT_MANAGER}
)

#: Roles that see every project and every monitored site. Everyone else —
#: viewers and developers — sees only the projects they are a member of, and
#: nothing at all until someone adds them to one. Currently the same set as
#: PROJECT_CREATORS, but kept separate: reading and creating are different
#: questions and may well diverge.
GLOBAL_VIEWERS: frozenset[UserRole] = frozenset(
    {UserRole.ADMIN, UserRole.DEVOPS, UserRole.PROJECT_MANAGER}
)


def is_role_manager(role: UserRole) -> bool:
    return role in ROLE_MANAGERS


def can_create_project(actor_role: UserRole) -> bool:
    return actor_role in PROJECT_CREATORS


def can_view_all_projects(actor_role: UserRole) -> bool:
    """Whether this role sees the whole estate, or only what it belongs to."""
    return actor_role in GLOBAL_VIEWERS


def can_manage_project(
    actor_role: UserRole, actor_id: int, project_owner_id: int | None
) -> bool:
    """May this user edit a project, its members, and its monitored sites?

    - Admin and DevOps may manage every project.
    - A project manager may manage the projects they created.
    - Everyone else may only read.

    Scoping project managers to their own projects is the conservative choice;
    to let any project manager manage any project, return True for that branch.
    """
    if actor_role in ROLE_MANAGERS:
        return True
    if actor_role is UserRole.PROJECT_MANAGER:
        return project_owner_id is not None and actor_id == project_owner_id
    return False


def can_change_role(actor_role: UserRole, target_role: UserRole) -> bool:
    """May a user with `actor_role` change `target_role`'s role?

    - Admin may re-role anyone.
    - DevOps may re-role ordinary users, but not an admin or another DevOps user.
    - Everyone else, project managers included, may re-role nobody.

    The DevOps restriction is what makes suspension rules meaningful: if DevOps
    could demote an admin, every "cannot suspend an admin" rule below could be
    sidestepped by demoting the target first.
    """
    if actor_role is UserRole.ADMIN:
        return True
    if actor_role is UserRole.DEVOPS:
        return target_role not in ROLE_MANAGERS
    return False


def can_suspend_user(actor_role: UserRole, target_role: UserRole) -> bool:
    """May a user with `actor_role` suspend or reinstate `target_role`?"""
    return target_role in SUSPENDABLE_BY.get(actor_role, frozenset())


def can_invite_role(actor_role: UserRole, role: UserRole) -> bool:
    """May a user with `actor_role` invite someone *as* `role`?

    - Admin may grant any role.
    - DevOps may grant any role except Admin. Unlike `can_change_role`, this
      includes DevOps itself: the question is what an invitation hands out, not
      what the recipient already is — they have no account yet.
    - Everyone else may invite nobody.

    The same test decides who may withdraw an invitation, so an actor can never
    undo one they could not have sent.
    """
    return role in INVITABLE_BY.get(actor_role, frozenset())
