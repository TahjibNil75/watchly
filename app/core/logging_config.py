"""Log files, one per part of the backend.

Every module already logs with `logging.getLogger(__name__)`; this decides where
those records land. Each record goes to the console (so `docker compose logs`
keeps working) and to the file of the part of the app it came from, in LOG_DIR:

    main.log                  startup, shutdown, the health endpoint
    access.log                one line per HTTP request, with its request id
    auth.log                  sign-in, sign-up, tokens, password resets
    user.log                  profiles, roles, email changes
    invitations.log           invitations sent, previewed, accepted, revoked
    account_requests.log      requests for an account from the sign-in page
    core.log                  mail, rate limits, encryption, request validation
    db.log                    database and migrations
    monitoring.log            the scheduler and the check/alert orchestration
    monitoring_websites.log   the checks themselves: HTTP, ping, DNS, SSL, CDN...
    monitoring_alerts.log     email, Slack, Telegram, WhatsApp, webhook delivery
    monitoring_notifications.log  notification rules and monthly reports
    server.log                uvicorn's own messages
    errors.log                every ERROR and above from anywhere, with tracebacks

Only the last LOG_RETENTION_HOURS of each file are kept: lines older than that
are dropped, at startup and then every few minutes while the API runs. This is
not safe across processes, which is fine: the API runs one worker, because the
monitoring scheduler lives inside it.

A line looks like

    2026-10-04 09:15:02 | ERROR    | 3f9c1a7b2d4e | app.auth.service | message

where the third column is the id of the request being served (`-` outside one),
also sent back to the client as `X-Request-ID`, so a failure the user reports
can be found by id in every file at once.
"""

import calendar
import logging
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path

from fastapi import Request, Response

from app.core.config import settings

#: Logger-name prefix -> log file (without `.log`). A record goes to the file of
#: the longest matching prefix, so `app.monitoring.alerts.slack` lands in
#: monitoring_alerts and not in monitoring. Anything unmatched has no file of
#: its own; it still reaches the console and, from ERROR up, errors.log.
MODULE_LOGS: dict[str, str] = {
    "app.main": "main",
    "app.auth": "auth",
    "app.user": "user",
    "app.invitations": "invitations",
    "app.account_requests": "account_requests",
    "app.core": "core",
    "app.db": "db",
    "sqlalchemy": "db",
    "alembic": "db",
    "app.monitoring": "monitoring",
    "app.monitoring.websites": "monitoring_websites",
    "app.monitoring.alerts": "monitoring_alerts",
    "app.monitoring.notifications": "monitoring_notifications",
}

_FORMAT = "%(asctime)s | %(levelname)-8s | %(request_id)s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Id of the request being served, set by `log_requests`. A ContextVar so that
#: every log line written while handling a request carries it, whichever
#: module wrote the line.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

access_logger = logging.getLogger("app.access")
#: Not in MODULE_LOGS, so its tracebacks go to the console and errors.log only.
unhandled_logger = logging.getLogger("app.unhandled")
logger = logging.getLogger(__name__)

_configured = False


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _ModuleFilter(logging.Filter):
    """Lets through only the records whose longest matching prefix is `name`."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._log_name = name

    def filter(self, record: logging.LogRecord) -> bool:
        return _log_name_for(record.name) == self._log_name


def _log_name_for(logger_name: str) -> str | None:
    best = None
    for prefix in MODULE_LOGS:
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            if best is None or len(prefix) > len(best):
                best = prefix
    return MODULE_LOGS[best] if best else None


#: How often a file handler checks its file for lines past the retention window.
_PRUNE_INTERVAL_SECONDS = 600
_TIMESTAMP_LENGTH = len("2026-10-04 09:15:02")


def _line_time(line: str) -> float | None:
    """Epoch seconds of a log line, or None for a continuation (traceback) line."""
    try:
        return calendar.timegm(time.strptime(line[:_TIMESTAMP_LENGTH], _DATE_FORMAT))
    except ValueError:
        return None


def prune_log_file(path: Path, max_age_seconds: float) -> None:
    """Rewrite `path` without the records older than `max_age_seconds`.

    A record that spans several lines (a traceback) stays or goes as a whole,
    following the timestamp of its first line.
    """
    cutoff = time.time() - max_age_seconds
    try:
        with open(path, encoding="utf-8", errors="replace") as source:
            lines = source.readlines()
    except OSError:
        return

    keep = False
    kept: list[str] = []
    for line in lines:
        written = _line_time(line)
        if written is not None:
            keep = written >= cutoff
        if keep:
            kept.append(line)
    if len(kept) == len(lines):
        return

    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text("".join(kept), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)


class _RetentionFileHandler(logging.FileHandler):
    """A log file that only keeps the last LOG_RETENTION_HOURS."""

    def __init__(self, path: Path, max_age_seconds: float) -> None:
        super().__init__(path, encoding="utf-8", delay=True)
        self._max_age_seconds = max_age_seconds
        self._next_prune = time.monotonic() + _PRUNE_INTERVAL_SECONDS

    def emit(self, record: logging.LogRecord) -> None:
        # `emit` runs under the handler's lock, so nothing writes while the
        # file is swapped. The stream is reopened by the write that follows.
        if time.monotonic() >= self._next_prune:
            self._next_prune = time.monotonic() + _PRUNE_INTERVAL_SECONDS
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            prune_log_file(Path(self.baseFilename), self._max_age_seconds)
        super().emit(record)


def _remove_stale_files(directory: Path, max_age_seconds: float) -> None:
    """Prune every log file left by an earlier run, and delete the rotated ones
    (`server.log.1`, ...) that an older version of Watchly produced."""
    for path in directory.glob("*.log.*"):
        path.unlink(missing_ok=True)
    for path in directory.glob("*.log"):
        prune_log_file(path, max_age_seconds)


def _file_handler(
    directory: Path, name: str, level: int, formatter: logging.Formatter
) -> logging.FileHandler:
    handler = _RetentionFileHandler(
        directory / f"{name}.log", settings.LOG_RETENTION_HOURS * 3600
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(_RequestIdFilter())
    return handler


def setup_logging() -> None:
    """Attach the console and file handlers. Safe to call more than once."""
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    level = logging.getLevelNamesMapping().get(settings.LOG_LEVEL.upper(), logging.INFO)
    formatter = logging.Formatter(_FORMAT, _DATE_FORMAT)
    formatter.converter = time.gmtime  # UTC, to match the database

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(formatter)
    console.addFilter(_RequestIdFilter())
    root.setLevel(level)
    root.addHandler(console)

    # SQLAlchemy would otherwise inherit INFO and log every statement with its
    # bound parameters: password hashes, tokens, email addresses.
    logging.getLogger("sqlalchemy").setLevel(logging.DEBUG if level == logging.DEBUG else logging.WARNING)
    # Chatty: one INFO line per outgoing request.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    directory = Path(settings.LOG_DIR)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not (directory.is_dir() and os.access(directory, os.W_OK)):
        # An unwritable log directory must not stop the app from starting.
        logger.warning(
            "Cannot write log files to %r; logging to the console only.",
            str(directory),
        )
        return

    _remove_stale_files(directory, settings.LOG_RETENTION_HOURS * 3600)

    for name in dict.fromkeys(MODULE_LOGS.values()):
        handler = _file_handler(directory, name, level, formatter)
        handler.addFilter(_ModuleFilter(name))
        root.addHandler(handler)

    errors = _file_handler(directory, "errors", logging.ERROR, formatter)
    root.addHandler(errors)

    # uvicorn does not pass its records up to the root logger, so give its own
    # file and the error file to it directly.
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.propagate = False
    uvicorn_logger.addHandler(_file_handler(directory, "server", level, formatter))
    uvicorn_logger.addHandler(errors)

    # Requests go to their own file only: uvicorn already prints them.
    access_logger.addHandler(_file_handler(directory, "access", level, formatter))
    access_logger.propagate = False
    access_logger.setLevel(level)


async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Middleware: give the request an id, log it, and log what breaks it.

    Only the path is logged, never the query string, which can carry tokens.
    """
    token = request_id_var.set(uuid.uuid4().hex[:12])
    started = time.perf_counter()
    client = request.client.host if request.client else "-"
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000
        unhandled_logger.exception(
            "Unhandled error serving %s %s from %s after %.0f ms",
            request.method,
            request.url.path,
            client,
            elapsed_ms,
        )
        access_logger.error(
            "%s %s -> 500 (unhandled error) %.0f ms from %s",
            request.method,
            request.url.path,
            elapsed_ms,
            client,
        )
        raise
    else:
        elapsed_ms = (time.perf_counter() - started) * 1000
        status = response.status_code
        # The container healthcheck calls this every few seconds, and every
        # Docker agent pushes every half minute.
        if status < 400 and (request.url.path == "/health" or request.url.path.endswith("/docker/ingest")):
            response.headers["X-Request-ID"] = request_id_var.get()
            return response
        access_logger.log(
            logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO,
            "%s %s -> %d %.0f ms from %s",
            request.method,
            request.url.path,
            status,
            elapsed_ms,
            client,
        )
        response.headers["X-Request-ID"] = request_id_var.get()
        return response
    finally:
        request_id_var.reset(token)
