import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.account_requests.routes import router as account_requests_router
from app.auth.routes import router as auth_router
from app.core.config import settings
from app.core.geo import block_countries
from app.core.handlers import register_exception_handlers
from app.core.logging_config import log_requests, setup_logging
from app.db.session import engine
from app.invitations.routes import router as invitations_router
from app.monitoring.routes import router as monitoring_router
from app.monitoring.scheduler import scheduler
from app.user.routes import router as user_router

setup_logging()
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
    logger.info(
        "Watchly %s starting (monitoring %s, log level %s, logs in %s)",
        __version__,
        "on" if settings.MONITORING_ENABLED else "off",
        settings.LOG_LEVEL.upper(),
        settings.LOG_DIR,
    )
    scheduler.start()
    try:
        yield
    finally:
        logger.info("Watchly shutting down")
        await scheduler.stop()
        await engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=__version__,
    debug=settings.DEBUG,
    lifespan=lifespan,
)

register_exception_handlers(app)
app.middleware("http")(block_countries)
# Added last so it is outermost: it also times and logs the requests the
# country block turns away.
app.middleware("http")(log_requests)

app.include_router(auth_router, prefix=settings.API_V1_PREFIX)
app.include_router(user_router, prefix=settings.API_V1_PREFIX)
app.include_router(invitations_router, prefix=settings.API_V1_PREFIX)
app.include_router(account_requests_router, prefix=settings.API_V1_PREFIX)
app.include_router(monitoring_router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "version": __version__,
        "monitoring": "on" if scheduler.is_running else "off",
    }
