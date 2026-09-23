"""Project CRUD and membership. No HTTP concerns."""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.permissions import can_manage_project, can_view_all_projects
from app.db.models.user import User
from app.monitoring.projects.models import Project, member_project_ids
from app.monitoring.projects.schemas import (
    ProjectCreate,
    ProjectUpdate,
    check_alert_channels,
)


class ProjectError(Exception):
    """Base class for project failures."""


class ProjectNotFoundError(ProjectError):
    def __init__(self, project_id: int) -> None:
        self.project_id = project_id
        super().__init__(f"No project with id {project_id}.")


class DuplicateProjectError(ProjectError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"A project named {name!r} already exists.")


class UnknownMembersError(ProjectError):
    def __init__(self, user_ids: list[int]) -> None:
        self.user_ids = user_ids
        listed = ", ".join(str(i) for i in user_ids)
        super().__init__(f"No such user(s): {listed}.")


class NoAlertChannelError(ProjectError):
    """The change would leave the project with no way to raise an alert."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class ProjectForbiddenError(ProjectError):
    """The actor may read this project but not change it."""

    def __init__(self) -> None:
        super().__init__(
            "You may only manage projects you own. Ask an admin or DevOps user."
        )


async def resolve_users(session: AsyncSession, user_ids: list[int]) -> list[User]:
    """Load users by id, rejecting any that do not exist.

    Shared by project membership and per-site recipients.
    """
    if not user_ids:
        return []
    wanted = list(dict.fromkeys(user_ids))
    users = list(await session.scalars(select(User).where(User.id.in_(wanted))))
    missing = sorted(set(wanted) - {user.id for user in users})
    if missing:
        raise UnknownMembersError(missing)
    return users


class ProjectService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _assert_still_alertable(project: Project) -> None:
        """Guard the invariant after a change, before it is committed."""
        try:
            check_alert_channels(
                has_email=project.has_email_alerting,
                slack_token=project.slack_bot_token,
                slack_channel=project.slack_channel_id,
            )
        except ValueError as exc:
            raise NoAlertChannelError(str(exc)) from exc

    async def get(self, project_id: int) -> Project:
        project = await self.session.get(Project, project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)
        return project

    async def get_visible(self, project_id: int, actor: User) -> Project:
        """Fetch a project the actor is allowed to *see*.

        A viewer or developer who is not a member gets `ProjectNotFoundError`
        rather than a 403 — a project they have no access to should not be
        distinguishable from one that does not exist.
        """
        project = await self.get(project_id)
        if can_view_all_projects(actor.role):
            return project
        if any(member.id == actor.id for member in project.members):
            return project
        raise ProjectNotFoundError(project_id)

    async def get_for_write(self, project_id: int, actor: User) -> Project:
        """Fetch a project the actor is allowed to modify."""
        project = await self.get(project_id)
        if not can_manage_project(actor.role, actor.id, project.owner_id):
            raise ProjectForbiddenError
        return project

    async def list(
        self,
        actor: User,
        limit: int = 50,
        offset: int = 0,
        is_active: bool | None = None,
        owner_id: int | None = None,
    ) -> tuple[list[Project], int]:
        """One page of the projects `actor` is allowed to see."""
        filters = []
        if is_active is not None:
            filters.append(Project.is_active.is_(is_active))
        if owner_id is not None:
            filters.append(Project.owner_id == owner_id)
        if not can_view_all_projects(actor.role):
            # Viewers and developers see only what they belong to — an empty
            # list until someone adds them to a project.
            filters.append(Project.id.in_(member_project_ids(actor.id)))

        total = await self.session.scalar(
            select(func.count()).select_from(Project).where(*filters)
        )
        rows = await self.session.scalars(
            select(Project).where(*filters).order_by(Project.id).limit(limit).offset(offset)
        )
        return list(rows), total or 0

    async def create(self, payload: ProjectCreate, owner: User) -> Project:
        members = await resolve_users(self.session, payload.member_ids)
        project = Project(
            name=payload.name,
            description=payload.description,
            is_active=payload.is_active,
            extra_emails=[str(email) for email in payload.extra_emails],
            slack_bot_token=(
                encrypt_secret(payload.slack_bot_token)
                if payload.slack_bot_token
                else None
            ),
            slack_channel_id=payload.slack_channel_id,
            owner_id=owner.id,
        )
        project.members = members
        self.session.add(project)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise DuplicateProjectError(payload.name) from exc
        await self.session.refresh(project)
        return project

    async def update(
        self, project_id: int, payload: ProjectUpdate, actor: User
    ) -> Project:
        project = await self.get_for_write(project_id, actor)
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("extra_emails") is not None:
            changes["extra_emails"] = [str(e) for e in changes["extra_emails"]]
        if "slack_bot_token" in changes:
            # Explicit null clears it; anything else is encrypted before storage.
            token = changes["slack_bot_token"]
            changes["slack_bot_token"] = encrypt_secret(token) if token else None
        if changes.get("slack_enabled") is None:
            # `slack_enabled` is optional in the payload but NOT NULL in the
            # table, so an omitted-as-null must not be written.
            changes.pop("slack_enabled", None)
        for field, value in changes.items():
            setattr(project, field, value)

        # Slack is a unit: explicitly nulling either half means "turn Slack
        # off", so clear both rather than rejecting it as a half-configuration.
        # Supplying only one half as a *value* is still an error.
        if any(
            field in changes and changes[field] is None
            for field in ("slack_bot_token", "slack_channel_id")
        ):
            project.slack_bot_token = None
            project.slack_channel_id = None

        try:
            self._assert_still_alertable(project)
        except NoAlertChannelError:
            # Discard the half-applied change so the session is not left dirty.
            await self.session.rollback()
            raise

        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise DuplicateProjectError(payload.name or project.name) from exc
        await self.session.refresh(project)
        return project

    async def delete(self, project_id: int, actor: User) -> None:
        """Deleting a project also stops monitoring every site under it."""
        project = await self.get_for_write(project_id, actor)
        await self.session.delete(project)
        await self.session.commit()

    async def add_members(
        self, project_id: int, member_ids: list[int], actor: User
    ) -> Project:
        project = await self.get_for_write(project_id, actor)
        existing = {member.id for member in project.members}
        for user in await resolve_users(self.session, member_ids):
            if user.id not in existing:
                project.members.append(user)
        await self.session.commit()
        await self.session.refresh(project)
        return project

    async def remove_member(
        self, project_id: int, user_id: int, actor: User
    ) -> Project:
        project = await self.get_for_write(project_id, actor)
        project.members = [m for m in project.members if m.id != user_id]
        # Removing the last member is only allowed if something else can alert.
        try:
            self._assert_still_alertable(project)
        except NoAlertChannelError:
            await self.session.rollback()
            raise
        await self.session.commit()
        await self.session.refresh(project)
        return project
