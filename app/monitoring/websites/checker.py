"""Performs a single HTTP probe of a website.

Every failure mode — DNS, TLS, connect, timeout, unexpected status — comes back
as a :class:`CheckResult`. This module never raises for a site being down; that
is a normal outcome, not an error.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app.monitoring.websites.models import Website

logger = logging.getLogger(__name__)

#: Identify ourselves so site owners can spot the traffic in their logs.
USER_AGENT = "Watchly-Monitor/1.0 (+uptime-checker)"

#: Response headers worth putting in an alert.
DIAGNOSTIC_HEADERS = (
    "server",
    "content-type",
    "cache-control",
    "retry-after",
    "x-request-id",
    "cf-ray",
)


@dataclass(slots=True)
class CheckResult:
    """Everything we learned from one probe, for storage and for alert bodies."""

    is_up: bool
    checked_at: datetime
    status_code: int | None = None
    reason: str | None = None
    response_time_ms: int | None = None
    error: str | None = None
    error_type: str | None = None
    final_url: str | None = None
    redirected: bool = False
    content_length: int | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """One-line human description, used as the alert headline."""
        if self.is_up:
            return f"HTTP {self.status_code} in {self.response_time_ms} ms"
        if self.status_code is not None:
            return f"HTTP {self.status_code} {self.reason or ''}".strip()
        return self.error or "Unreachable"


def _classify(exc: Exception) -> tuple[str, str]:
    """Turn an httpx exception into (error_type, human explanation)."""
    match exc:
        case httpx.ConnectTimeout():
            return "connect_timeout", "Timed out establishing a TCP connection."
        case httpx.ReadTimeout():
            return "read_timeout", "Connected, but the server sent no response in time."
        case httpx.WriteTimeout() | httpx.PoolTimeout():
            return "timeout", f"Request timed out ({type(exc).__name__})."
        case httpx.ConnectError():
            # Covers DNS failure and refused connections.
            return "connect_error", f"Could not connect: {exc}"
        case httpx.TooManyRedirects():
            return "too_many_redirects", "Redirect loop — the chain never resolved."
        case httpx.InvalidURL():
            return "invalid_url", f"Malformed URL: {exc}"
        case httpx.ProtocolError():
            return "protocol_error", f"Bad HTTP response: {exc}"
        case _:
            return type(exc).__name__, str(exc) or repr(exc)


async def check_website(
    website: Website, client: httpx.AsyncClient | None = None
) -> CheckResult:
    """Probe `website` once and report what happened.

    A caller running many checks should pass a shared `client` so connections
    and DNS lookups are reused.
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, http2=False)

    started = time.perf_counter()
    checked_at = datetime.now(UTC)
    try:
        response = await client.request(
            website.method.upper(),
            website.url,
            timeout=website.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    except Exception as exc:  # noqa: BLE001 - every failure is a "down" signal
        error_type, message = _classify(exc)
        logger.info("Check failed for %s: %s", website.url, message)
        return CheckResult(
            is_up=False,
            checked_at=checked_at,
            response_time_ms=int((time.perf_counter() - started) * 1000),
            error=message,
            error_type=error_type,
        )
    else:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        is_up = response.status_code == website.expected_status
        return CheckResult(
            is_up=is_up,
            checked_at=checked_at,
            status_code=response.status_code,
            reason=response.reason_phrase,
            response_time_ms=elapsed_ms,
            error=(
                None
                if is_up
                else f"Expected HTTP {website.expected_status}, got {response.status_code}."
            ),
            error_type=None if is_up else "unexpected_status",
            final_url=str(response.url),
            redirected=str(response.url) != website.url,
            content_length=len(response.content) if response.content else 0,
            headers={
                name: response.headers[name]
                for name in DIAGNOSTIC_HEADERS
                if name in response.headers
            },
        )
    finally:
        if owns_client:
            await client.aclose()
