import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth.routes import router as auth_router
from app.core.config import settings
from app.core.handlers import register_exception_handlers
from app.db.session import engine
from app.monitoring.routes import router as monitoring_router
from app.monitoring.scheduler import scheduler
from app.user.routes import router as user_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.SECRET_KEY == "dev-secret-change-me":
        logger.warning(
            "SECRET_KEY is still the default value — set a real one "
            "(openssl rand -hex 32) before deploying."
        )
    if settings.MONITORING_ENABLED and not settings.SMTP_HOST:
        logger.warning(
            "Monitoring is on but SMTP_HOST is unset — outages will be detected "
            "and recorded, but no email alerts will go out."
        )
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    debug=settings.DEBUG,
    lifespan=lifespan,
)

register_exception_handlers(app)

app.include_router(auth_router, prefix=settings.API_V1_PREFIX)
app.include_router(user_router, prefix=settings.API_V1_PREFIX)
app.include_router(monitoring_router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok", "monitoring": "on" if scheduler.is_running else "off"}
