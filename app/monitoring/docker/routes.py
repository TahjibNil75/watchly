"""Docker monitoring over HTTP.

`POST /docker/ingest` is where agents push, with their host's token instead of
a user's session; its contract is the agent's docs/protocol.md. Everything
under /monitoring/docker is the dashboard's. All of it answers 404 while
DOCKER_ENABLED is off, which an agent takes as "wrong URL" and retries slowly.
"""

import zlib

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.rate_limit import RATE_LIMITED, _count
from app.core.security import hash_token
from app.db.models.user import User
from app.db.session import get_db
from app.monitoring.docker.ingest import DockerIngestService
from app.monitoring.docker.monitor import DockerHistoryService
from app.monitoring.docker.schemas import (
    ContainerList,
    ContainerRead,
    ContainerStats,
    ContainerUpdate,
    EventList,
    HostCreate,
    HostCreated,
    HostList,
    HostRead,
    HostUpdate,
    Ingest,
    IngestResponse,
    TokenRotated,
)
from app.monitoring.docker.service import (
    DockerConflictError,
    DockerError,
    DockerForbiddenError,
    DockerInvalidError,
    DockerNotFoundError,
    DockerService,
)
from app.monitoring.websites.schemas import StatsRange

#: An agent's push is a few KB gzip'd; this is far beyond any real host.
MAX_BODY_BYTES = 1 << 20
MAX_INFLATED_BYTES = 8 << 20


def require_enabled() -> None:
    if not settings.DOCKER_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Docker monitoring is off: set DOCKER_ENABLED=true.")


ingest_router = APIRouter(prefix="/docker", tags=["docker agent"], dependencies=[Depends(require_enabled)])
router = APIRouter(
    prefix="/monitoring/docker", tags=["monitoring: docker"], dependencies=[Depends(require_enabled)]
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


async def _read_body(request: Request) -> bytes:
    """The request body, inflated if gzip'd, refusing anything oversized."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Push too large.")
    if request.headers.get("content-encoding", "").strip().lower() != "gzip":
        return bytes(body)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = inflater.decompress(bytes(body), MAX_INFLATED_BYTES + 1)
    except zlib.error as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Body is not valid gzip: {exc}") from exc
    if len(data) > MAX_INFLATED_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Push too large once inflated.")
    if not inflater.eof:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is truncated gzip.")
    return data


@ingest_router.post(
    "/ingest",
    response_model=IngestResponse,
    summary="Push from a Watchly Docker agent",
    responses={
        401: {"description": "Missing or unknown agent token"},
        413: {"description": "Push too large"},
        422: {"description": "Not a schema-1 push; the agent drops it"},
        **RATE_LIMITED,
    },
)
async def ingest(
    request: Request,
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_db),
) -> IngestResponse | JSONResponse:
    """The state of every container on the agent's host, its resource use (on
    a heartbeat) and the Docker events since its last push. Answers with the
    agent's settings: its interval and the containers it should skip."""
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized("Send the host's agent token as `Authorization: Bearer <token>`.")
    if settings.RATE_LIMIT_ENABLED:
        # Counted per token, not per address: many hosts may share one NAT.
        await _count("docker_ingest", f"token:{hash_token(token)[:32]}", settings.RATE_LIMIT_DOCKER_INGEST)
    service = DockerIngestService(session)
    host = await service.authenticate(token)
    if host is None:
        raise _unauthorized("Unknown agent token: it may have been rotated, or its host deleted.")
    body = await _read_body(request)
    try:
        push = Ingest.model_validate_json(body)
    except ValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": exc.errors(include_url=False, include_context=False, include_input=False)},
        )
    try:
        return await service.ingest(host.id, push)
    except LookupError as exc:
        raise _unauthorized("This host was deleted.") from exc


def get_service(session: AsyncSession = Depends(get_db)) -> DockerService:
    return DockerService(session)


def _translate(exc: DockerError) -> HTTPException:
    match exc:
        case DockerNotFoundError():
            code = status.HTTP_404_NOT_FOUND
        case DockerForbiddenError():
            code = status.HTTP_403_FORBIDDEN
        case DockerConflictError():
            code = status.HTTP_409_CONFLICT
        case DockerInvalidError():
            code = status.HTTP_422_UNPROCESSABLE_ENTITY
        case _:
            code = status.HTTP_400_BAD_REQUEST
    return HTTPException(code, str(exc))


@router.get("/hosts", response_model=HostList)
async def list_hosts(
    project_id: int | None = Query(None),
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
) -> HostList:
    """The Docker hosts the caller may see, with their containers counted."""
    return HostList(items=await service.list_hosts(actor, project_id))


@router.post("/hosts", response_model=HostCreated, status_code=status.HTTP_201_CREATED)
async def create_host(
    payload: HostCreate,
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
) -> HostCreated:
    """Add a host to a Docker project. The response holds the agent token,
    once: only its hash is kept."""
    try:
        host, token = await service.create_host(payload, actor)
    except DockerError as exc:
        raise _translate(exc) from exc
    return HostCreated(host=host, token=token)


@router.get("/hosts/{host_id}", response_model=HostRead)
async def get_host(
    host_id: int = Path(...),
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
) -> HostRead:
    try:
        return await service.get_host(host_id, actor)
    except DockerError as exc:
        raise _translate(exc) from exc


@router.patch("/hosts/{host_id}", response_model=HostRead)
async def update_host(
    payload: HostUpdate,
    host_id: int = Path(...),
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
) -> HostRead:
    """Rename it, or change what its agent is told: its interval and the
    containers to skip. The agent picks them up with its next push."""
    try:
        return await service.update_host(host_id, payload, actor)
    except DockerError as exc:
        raise _translate(exc) from exc


@router.post("/hosts/{host_id}/token", response_model=TokenRotated)
async def rotate_token(
    host_id: int = Path(...),
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
) -> TokenRotated:
    """A new agent token; the old one stops working at once."""
    try:
        token, hint = await service.rotate_token(host_id, actor)
    except DockerError as exc:
        raise _translate(exc) from exc
    return TokenRotated(token=token, token_hint=hint)


@router.delete("/hosts/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_host(
    host_id: int = Path(...),
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
) -> Response:
    """Delete the host, its containers and their history. Its agent's token
    stops working."""
    try:
        await service.delete_host(host_id, actor)
    except DockerError as exc:
        raise _translate(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/containers", response_model=ContainerList)
async def list_containers(
    host_id: int | None = Query(None),
    project_id: int | None = Query(None),
    q: str | None = Query(None, max_length=200, description="Matches name, image or compose project."),
    include_removed: bool = Query(False),
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
) -> ContainerList:
    items, counts = await service.list_containers(
        actor, host_id=host_id, project_id=project_id, q=q, include_removed=include_removed
    )
    return ContainerList(items=items, counts=counts)


@router.get("/containers/{container_id}", response_model=ContainerRead)
async def get_container(
    container_id: int = Path(...),
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
) -> ContainerRead:
    try:
        return await service.get_container(container_id, actor)
    except DockerError as exc:
        raise _translate(exc) from exc


@router.patch("/containers/{container_id}", response_model=ContainerRead)
async def update_container(
    payload: ContainerUpdate,
    container_id: int = Path(...),
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
) -> ContainerRead:
    """Mute or unmute it: a muted container is tracked but never alerts."""
    try:
        return await service.set_muted(container_id, payload.muted, actor)
    except DockerError as exc:
        raise _translate(exc) from exc


@router.get("/containers/{container_id}/stats", response_model=ContainerStats)
async def container_stats(
    container_id: int = Path(...),
    range_: StatsRange = Query(StatsRange.DAY, alias="range"),
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
    session: AsyncSession = Depends(get_db),
) -> ContainerStats:
    """CPU, memory and network over the range: five-minute buckets for the
    last day, then hourly (a week) or daily (30 and 90 days)."""
    try:
        container = await service.container_for_stats(container_id, actor)
    except DockerError as exc:
        raise _translate(exc) from exc
    return await DockerHistoryService(session).stats(container, range_)


@router.get("/events", response_model=EventList)
async def list_events(
    host_id: int | None = Query(None),
    container_id: int | None = Query(None),
    project_id: int | None = Query(None),
    source: str | None = Query(None, pattern="^(docker|watchly)$"),
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, description="For the next page: the last id of the previous one."),
    actor: User = Depends(get_current_user),
    service: DockerService = Depends(get_service),
) -> EventList:
    """The Docker feed, newest first: Docker's events as the agents reported
    them (`source=docker`), and what Watchly made of them (`watchly`)."""
    return EventList(
        items=await service.events(
            actor,
            host_id=host_id,
            container_id=container_id,
            project_id=project_id,
            source=source,
            limit=limit,
            before_id=before_id,
        )
    )
