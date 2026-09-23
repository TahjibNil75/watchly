"""Aggregates the monitoring sub-routers into one router the app mounts."""

from fastapi import APIRouter

from app.monitoring.notifications.routes import router as notifications_router
from app.monitoring.projects.routes import router as projects_router
from app.monitoring.websites.routes import router as websites_router

router = APIRouter()
router.include_router(projects_router)
router.include_router(websites_router)
router.include_router(notifications_router)

__all__ = ["router"]
