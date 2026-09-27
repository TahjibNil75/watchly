"""Background loop that drives the checks.

A plain asyncio task rather than a scheduler library: the only schedule we need
is "wake every MONITOR_TICK_SECONDS and probe whatever is due", and each
website carries its own interval.

Multi-worker safety: `uvicorn --workers N` would otherwise run N copies of this
loop and send N copies of every alert, so each tick takes a Postgres advisory
lock and skips the tick if another worker already holds it.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import AuthService
from app.core.config import settings
from app.core.rate_limit import purge_closed_windows
from app.db.session import AsyncSessionLocal, engine
from app.monitoring.notifications.reports import ReportService
from app.monitoring.service import MonitoringService
from app.monitoring.websites.history import HistoryService
from app.monitoring.websites.service import WebsiteService

logger = logging.getLogger(__name__)

#: Arbitrary but fixed application-wide key for the tick lock.
TICK_LOCK_KEY = 0x7A7C_4C59


async def _run_guarded(what: str, job: Callable[[AsyncSession], Awaitable[object]]) -> None:
    """Run `job` in a session of its own. A failure is logged and swallowed: it
    must not take the tick's check results, or the jobs after it, down too."""
    try:
        async with AsyncSessionLocal() as session:
            await job(session)
    except Exception:
        logger.exception("%s failed; continuing.", what)


async def run_tick() -> int:
    """Run one round of due checks, then the housekeeping that follows them:
    rolling checks up by the hour, any monthly reports that are due, purging
    checks, feed events and ended maintenance past their retention, and
    deleting expired refresh tokens and closed rate-limit windows.

    Returns how many websites were probed.
    """
    async with AsyncSessionLocal() as session:
        outcomes = await MonitoringService(session).run_due_checks()
    await _run_guarded("Hourly rollup", lambda s: HistoryService(s).rollup())
    await _run_guarded("Monthly report run", lambda s: ReportService(s).send_due_reports())
    # After the rollup, which the purge relies on to have copied what it deletes.
    await _run_guarded("Check purge", lambda s: HistoryService(s).purge())
    await _run_guarded("Event purge", lambda s: WebsiteService(s).purge_old_events())
    await _run_guarded(
        "Maintenance purge", lambda s: WebsiteService(s).purge_old_maintenance()
    )
    await _run_guarded("Session purge", lambda s: AuthService(s).purge_expired_sessions())
    await _run_guarded("Rate limit purge", purge_closed_windows)
    return len(outcomes)


async def run_tick_locked() -> int:
    """`run_tick` guarded by an advisory lock; returns -1 if another worker has it."""
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        acquired = await conn.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": TICK_LOCK_KEY}
        )
        if not acquired:
            logger.debug("Another worker holds the monitoring lock; skipping tick.")
            return -1
        try:
            return await run_tick()
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": TICK_LOCK_KEY}
            )


async def _loop() -> None:
    interval = settings.MONITOR_TICK_SECONDS
    logger.info("Monitoring loop started; ticking every %ss.", interval)
    while True:
        try:
            await run_tick_locked()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one bad tick kill the loop.
            logger.exception("Monitoring tick failed; continuing.")
        await asyncio.sleep(interval)


class MonitorScheduler:
    """Owns the background task's lifecycle across app startup and shutdown."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not settings.MONITORING_ENABLED:
            logger.info("MONITORING_ENABLED is false; scheduler not started.")
            return
        if self.is_running:
            return
        self._task = asyncio.create_task(_loop(), name="watchly-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("Monitoring loop stopped.")


scheduler = MonitorScheduler()
