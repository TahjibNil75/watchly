"""Performs a single HTTP probe of a website.

Every failure mode — DNS, TLS, connect, timeout, unexpected status — comes back
as a :class:`CheckResult`. This module never raises for a site being down; that
is a normal outcome, not an error.
"""

import asyncio
import contextlib
import logging
import ssl
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.x509.oid import NameOID

from app.core.config import settings
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
class CertInfo:
    """What a certificate probe learned. `error` is set when none could be read."""

    expires_at: datetime | None = None
    issuer: str | None = None
    error: str | None = None


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
    #: Set only on the checks that also read the certificate; see
    #: `certificate_check_due`. None means "not looked at", not "no certificate".
    cert: CertInfo | None = None

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


def certificate_check_due(website: Website, now: datetime | None = None) -> bool:
    """Whether this check should also read the site's certificate.

    Expiry moves in days, so it is read every SSL_CHECK_INTERVAL_SECONDS rather
    than on every check. The attempt time is stored even when the read fails,
    which keeps a broken handshake from being retried every tick.
    """
    if not settings.SSL_CHECK_ENABLED or not website.url.lower().startswith("https://"):
        return False
    if website.ssl_checked_at is None:
        return True
    elapsed = ((now or datetime.now(UTC)) - website.ssl_checked_at).total_seconds()
    return elapsed >= settings.SSL_CHECK_INTERVAL_SECONDS


def _issuer_name(cert: x509.Certificate) -> str | None:
    for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
        attributes = cert.issuer.get_attributes_for_oid(oid)
        if attributes:
            return str(attributes[0].value)
    return None


async def probe_certificate(url: str, timeout: float) -> CertInfo:
    """Read the certificate a site presents, without trusting it.

    Verification is off on purpose: the point is to learn the end date even of
    a certificate that has already expired or that a browser would reject. An
    untrusted or mismatched certificate is the regular check's business — it
    verifies, so it reports the site down with the reason.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return CertInfo(error="No host in URL.")

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host, parts.port or 443, ssl=context, server_hostname=host
            ),
            timeout,
        )
        try:
            ssl_object = writer.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if not der:
            return CertInfo(error="The server presented no certificate.")
        cert = x509.load_der_x509_certificate(der)
        return CertInfo(expires_at=cert.not_valid_after_utc, issuer=_issuer_name(cert))
    except Exception as exc:  # noqa: BLE001 - a failed read is data, not a crash
        logger.info("Certificate read failed for %s: %s", url, exc)
        return CertInfo(error=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)


async def check_website(
    website: Website, client: httpx.AsyncClient | None = None
) -> CheckResult:
    """Probe `website` once and report what happened.

    A caller running many checks should pass a shared `client` so connections
    and DNS lookups are reused. When the site's certificate is due for a read,
    the same call also fills `result.cert`.
    """
    result = await _http_probe(website, client)
    if certificate_check_due(website, result.checked_at):
        result.cert = await probe_certificate(website.url, website.timeout_seconds)
    return result


async def _http_probe(
    website: Website, client: httpx.AsyncClient | None = None
) -> CheckResult:
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
