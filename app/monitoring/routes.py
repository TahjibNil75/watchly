"""Aggregates the monitoring sub-routers into one router the app mounts."""

from fastapi import APIRouter

from app.monitoring.docker.routes import ingest_router as docker_ingest_router
from app.monitoring.docker.routes import router as docker_router
from app.monitoring.infra.aws.routes import router as infra_aws_router
from app.monitoring.notifications.routes import router as notifications_router
from app.monitoring.projects.routes import router as projects_router
from app.monitoring.websites.routes import router as websites_router

router = APIRouter()
router.include_router(projects_router)
router.include_router(websites_router)
router.include_router(notifications_router)
# Answers 404 throughout while INFRA_AWS_ENABLED is off.
router.include_router(infra_aws_router)
# Both answer 404 while DOCKER_ENABLED is off. The ingest endpoint takes an
# agent token instead of a session.
router.include_router(docker_router)
router.include_router(docker_ingest_router)

__all__ = ["router"]
