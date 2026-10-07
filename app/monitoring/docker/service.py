"""Docker hosts and containers as the API shows them, and who may see or
change what: the project rules websites follow. A host belongs to a Docker
project; whoever may manage the project manages its hosts."""

from collections.abc import Iterable

from sqlalchemy import and_, desc, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import can_manage_project, can_view_all_projects
from app.core.security import hash_token, new_link_token
from app.db.models.user import User
from app.monitoring.docker.models import REMOVED, DockerContainer, DockerEvent, DockerHost, HostStatus
from app.monitoring.docker.schemas import (
    ContainerCounts,
    ContainerRead,
    EventRead,
    HostBrief,
    HostCreate,
    HostRead,
    HostUpdate,
    ProjectBrief,
)
from app.monitoring.projects.models import Project, ProjectMonitors, visible_project_ids
from app.monitoring.projects.service import (
    ProjectForbiddenError,
    ProjectNotFoundError,
    ProjectService,
)

#: Every agent token starts with this, so a leaked one is recognisable.
TOKEN_PREFIX = "wdk_"


class DockerError(Exception):
    pass


class DockerNotFoundError(DockerError):
    def __init__(self, what: str, ident: int) -> None:
        super().__init__(f"No {what} with id {ident}.")


class DockerForbiddenError(DockerError):
    pass


class DockerConflictError(DockerError):
    pass


class DockerInvalidError(DockerError):
    pass


def new_token() -> tuple[str, str, str]:
    """A fresh agent token: (token, its hash, its hint)."""
    token = TOKEN_PREFIX + new_link_token()
    return token, hash_token(token), f"{token[:8]}…{token[-4:]}"


def condition(container: DockerContainer, host: DockerHost) -> str:
    """How Watchly sees a container now; see `ContainerRead.condition`."""
    if container.state == REMOVED:
        return "removed"
    if host.status != HostStatus.ONLINE.value:
        return "unknown"
    if container.state == "running":
        return "unhealthy" if container.health == "unhealthy" else "running"
    if container.state == "paused":
        return "paused"
    if container.state == "restarting":
        return "restarting"
    if container.down_since is not None:
        return "down"
    return "stopped"


def count(conditions: Iterable[str]) -> ContainerCounts:
    counts = ContainerCounts()
    for value in conditions:
        if value == "removed":
            continue
        counts.total += 1
        if value in ("running", "paused"):
            counts.running += 1
        elif value == "unhealthy":
            counts.running += 1
            counts.unhealthy += 1
        elif value in ("down", "restarting"):
            counts.down += 1
        elif value == "stopped":
            counts.stopped += 1
    return counts


def open_problems(container: DockerContainer) -> dict[str, dict]:
    return {
        key: {"since": entry["since"], "detail": entry.get("detail")}
        for key, entry in (container.problems or {}).items()
        if isinstance(entry, dict) and entry.get("since")
    }


class DockerService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.projects = ProjectService(session)

    # --- access -------------------------------------------------------------

    def _scope(self, actor: User, column):
        """A filter on `column` (a project id) to what `actor` may see."""
        if can_view_all_projects(actor.role):
            return true()
        return column.in_(visible_project_ids(actor.id))

    @staticmethod
    def _can_manage(actor: User, project: Project) -> bool:
        return can_manage_project(actor.role, actor.id, project.owner_id)

    async def _host(self, host_id: int, actor: User, *, write: bool = False) -> DockerHost:
        host = await self.session.get(DockerHost, host_id)
        if host is None:
            raise DockerNotFoundError("Docker host", host_id)
        try:
            if write:
                await self.projects.get_for_write(host.project_id, actor)
            else:
                await self.projects.get_visible(host.project_id, actor)
        except ProjectNotFoundError as exc:
            raise DockerNotFoundError("Docker host", host_id) from exc
        except ProjectForbiddenError as exc:
            raise DockerForbiddenError(str(exc)) from exc
        return host

    async def _container(self, container_id: int, actor: User, *, write: bool = False) -> DockerContainer:
        container = await self.session.get(DockerContainer, container_id)
        if container is None:
            raise DockerNotFoundError("container", container_id)
        try:
            await self._host(container.host_id, actor, write=write)
        except DockerNotFoundError as exc:
            raise DockerNotFoundError("container", container_id) from exc
        return container

    # --- reading ------------------------------------------------------------

    def host_read(self, host: DockerHost, actor: User, containers: list[DockerContainer]) -> HostRead:
        read = HostRead.model_validate(
            {
                **{c.key: getattr(host, c.key) for c in DockerHost.__table__.columns},
                "project": ProjectBrief(id=host.project.id, name=host.project.name),
                "counts": count(condition(c, host) for c in containers),
                "can_manage": self._can_manage(actor, host.project),
            }
        )
        return read

    def container_read(self, container: DockerContainer, actor: User) -> ContainerRead:
        host = container.host
        return ContainerRead.model_validate(
            {
                **{c.key: getattr(container, c.key) for c in DockerContainer.__table__.columns},
                "host": HostBrief(id=host.id, name=host.name, status=host.status),
                "project": ProjectBrief(id=host.project.id, name=host.project.name),
                "problems": open_problems(container),
                "condition": condition(container, host),
                "can_manage": self._can_manage(actor, host.project),
            }
        )

    async def list_hosts(self, actor: User, project_id: int | None = None) -> list[HostRead]:
        query = select(DockerHost).where(self._scope(actor, DockerHost.project_id)).order_by(DockerHost.name)
        if project_id is not None:
            query = query.where(DockerHost.project_id == project_id)
        hosts = list(await self.session.scalars(query))
        containers = await self._containers_of([h.id for h in hosts])
        return [self.host_read(h, actor, containers.get(h.id, [])) for h in hosts]

    async def _containers_of(self, host_ids: list[int]) -> dict[int, list[DockerContainer]]:
        if not host_ids:
            return {}
        rows = await self.session.scalars(
            select(DockerContainer).where(DockerContainer.host_id.in_(host_ids), DockerContainer.state != REMOVED)
        )
        grouped: dict[int, list[DockerContainer]] = {}
        for row in rows:
            grouped.setdefault(row.host_id, []).append(row)
        return grouped

    async def get_host(self, host_id: int, actor: User) -> HostRead:
        host = await self._host(host_id, actor)
        containers = await self._containers_of([host.id])
        return self.host_read(host, actor, containers.get(host.id, []))

    async def list_containers(
        self,
        actor: User,
        *,
        host_id: int | None = None,
        project_id: int | None = None,
        q: str | None = None,
        include_removed: bool = False,
    ) -> tuple[list[ContainerRead], ContainerCounts]:
        query = (
            select(DockerContainer)
            .where(self._scope(actor, DockerContainer.project_id))
            .order_by(DockerContainer.name)
        )
        if host_id is not None:
            query = query.where(DockerContainer.host_id == host_id)
        if project_id is not None:
            query = query.where(DockerContainer.project_id == project_id)
        if not include_removed:
            query = query.where(DockerContainer.state != REMOVED)
        if q and q.strip():
            like = f"%{q.strip().lower()}%"
            query = query.where(
                or_(
                    func.lower(DockerContainer.name).like(like),
                    func.lower(DockerContainer.image).like(like),
                    func.lower(func.coalesce(DockerContainer.compose_project, "")).like(like),
                )
            )
        rows = list(await self.session.scalars(query))
        items = [self.container_read(row, actor) for row in rows]
        return items, count(item.condition for item in items)

    async def get_container(self, container_id: int, actor: User) -> ContainerRead:
        return self.container_read(await self._container(container_id, actor), actor)

    async def container_for_stats(self, container_id: int, actor: User) -> DockerContainer:
        return await self._container(container_id, actor)

    async def events(
        self,
        actor: User,
        *,
        host_id: int | None = None,
        container_id: int | None = None,
        project_id: int | None = None,
        source: str | None = None,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[EventRead]:
        query = (
            select(DockerEvent, DockerHost.name)
            .join(DockerHost, DockerHost.id == DockerEvent.host_id)
            .where(self._scope(actor, DockerEvent.project_id))
            .order_by(desc(DockerEvent.occurred_at), desc(DockerEvent.id))
            .limit(limit)
        )
        filters = []
        if host_id is not None:
            filters.append(DockerEvent.host_id == host_id)
        if container_id is not None:
            filters.append(DockerEvent.container_id == container_id)
        if project_id is not None:
            filters.append(DockerEvent.project_id == project_id)
        if source is not None:
            filters.append(DockerEvent.source == source)
        if before_id is not None:
            filters.append(DockerEvent.id < before_id)
        if filters:
            query = query.where(and_(*filters))
        rows = await self.session.execute(query)
        return [
            EventRead.model_validate(
                {**{c.key: getattr(event, c.key) for c in DockerEvent.__table__.columns}, "host_name": name}
            )
            for event, name in rows
        ]

    # --- changing -----------------------------------------------------------

    async def create_host(self, payload: HostCreate, actor: User) -> tuple[HostRead, str]:
        try:
            project = await self.projects.get_for_write(payload.project_id, actor)
        except ProjectNotFoundError as exc:
            raise DockerNotFoundError("project", payload.project_id) from exc
        except ProjectForbiddenError as exc:
            raise DockerForbiddenError(str(exc)) from exc
        if project.monitors is not ProjectMonitors.DOCKER:
            raise DockerInvalidError(
                f"{project.name} monitors {project.monitors.value}: Docker hosts belong to Docker projects."
            )
        name = payload.name.strip()
        await self._assert_name_free(project.id, name)
        token, digest, hint = new_token()
        host = DockerHost(
            project_id=project.id,
            name=name,
            description=payload.description,
            interval_seconds=payload.interval_seconds,
            ignore_patterns=payload.ignore_patterns,
            token_hash=digest,
            token_hint=hint,
            status=HostStatus.PENDING.value,
            down_alerted=False,
            dropped_events=0,
        )
        self.session.add(host)
        await self.session.commit()
        await self.session.refresh(host)
        return self.host_read(host, actor, []), token

    async def _assert_name_free(self, project_id: int, name: str, except_id: int | None = None) -> None:
        taken = await self.session.scalar(
            select(DockerHost.id).where(
                DockerHost.project_id == project_id,
                func.lower(DockerHost.name) == name.lower(),
                DockerHost.id != (except_id or 0),
            )
        )
        if taken:
            raise DockerConflictError(f"This project already has a Docker host named {name!r}.")

    async def update_host(self, host_id: int, payload: HostUpdate, actor: User) -> HostRead:
        host = await self._host(host_id, actor, write=True)
        fields = payload.model_dump(exclude_unset=True)
        if fields.get("name") is not None:
            name = fields["name"].strip()
            await self._assert_name_free(host.project_id, name, host.id)
            host.name = name
        if "description" in fields:
            host.description = fields["description"]
        if fields.get("interval_seconds") is not None:
            host.interval_seconds = fields["interval_seconds"]
        if fields.get("ignore_patterns") is not None:
            host.ignore_patterns = fields["ignore_patterns"]
        await self.session.commit()
        await self.session.refresh(host)
        return await self.get_host(host.id, actor)

    async def rotate_token(self, host_id: int, actor: User) -> tuple[str, str]:
        """A new token for the host; the old one stops working at once."""
        host = await self._host(host_id, actor, write=True)
        token, digest, hint = new_token()
        host.token_hash = digest
        host.token_hint = hint
        await self.session.commit()
        return token, hint

    async def delete_host(self, host_id: int, actor: User) -> None:
        host = await self._host(host_id, actor, write=True)
        await self.session.delete(host)
        await self.session.commit()

    async def set_muted(self, container_id: int, muted: bool, actor: User) -> ContainerRead:
        container = await self._container(container_id, actor, write=True)
        container.muted = muted
        await self.session.commit()
        await self.session.refresh(container)
        return self.container_read(container, actor)


__all__ = [
    "DockerConflictError",
    "DockerError",
    "DockerForbiddenError",
    "DockerInvalidError",
    "DockerNotFoundError",
    "DockerService",
    "TOKEN_PREFIX",
    "condition",
    "new_token",
]
