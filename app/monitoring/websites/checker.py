"""Performs a single HTTP probe of a website.

Every failure mode — DNS, TLS, connect, timeout, unexpected status — comes back
as a :class:`CheckResult`. This module never raises for a site being down; that
is a normal outcome, not an error.
"""

import asyncio
import contextlib
import logging
import socket
import ssl
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import anyio
import httpcore
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

#: Wait this long on one address before also trying the next, as anyio does.
HAPPY_EYEBALLS_DELAY = 0.25

#: httpcore trace steps timed from the outside, and the `Timings` field each
#: fills. The lookup and the TCP connect happen inside one traced step, so
#: `_TimedBackend` times those two itself.
_TRACED_STEPS = {"start_tls": "tls_ms", "receive_response_headers": "first_byte_ms"}


@dataclass(slots=True)
class Timings:
    """Where one check's time went, step by step, in whole milliseconds.

    Summed over every hop of a redirect chain. None means the step did not
    finish on this check: the request failed before or during it, or a reused
    connection left nothing to look up, connect or negotiate.
    """

    dns_ms: int | None = None
    connect_ms: int | None = None
    tls_ms: int | None = None
    #: From the request being sent to the response headers arriving.
    first_byte_ms: int | None = None


class _Stopwatch:
    """Adds up one probe's step times as they happen."""

    def __init__(self) -> None:
        self._totals: dict[str, float] = {}
        self._started: dict[str, float] = {}

    def add(self, step: str, seconds: float) -> None:
        self._totals[step] = self._totals.get(step, 0.0) + seconds

    async def trace(self, event: str, info: dict) -> None:
        """httpcore's `trace` extension. `event` is e.g. `connection.start_tls.complete`."""
        where, _, state = event.rpartition(".")
        step = _TRACED_STEPS.get(where.rpartition(".")[2])
        if step is None:
            return
        if state == "started":
            self._started[step] = time.perf_counter()
        elif state == "complete" and step in self._started:
            self.add(step, time.perf_counter() - self._started.pop(step))

    def timings(self) -> Timings:
        return Timings(
            **{step: round(seconds * 1000) for step, seconds in self._totals.items()}
        )


#: The stopwatch of the probe running in this task. One backend serves every
#: probe on a client, so this is how it knows whose connection it is timing.
_stopwatch: ContextVar[_Stopwatch | None] = ContextVar("check_stopwatch", default=None)


def _connect_order(infos: list) -> list[str]:
    """Resolved addresses in the order anyio tries them: the first IPv6 one,
    then the first IPv4 one, then the rest as the resolver returned them."""
    ordered: list[str] = []
    v6_found = v4_found = False
    for family, *_, sockaddr in infos:
        address = str(sockaddr[0])
        if family == socket.AF_INET6 and not v6_found:
            v6_found = True
            ordered.insert(0, address)
        elif family == socket.AF_INET and v6_found and not v4_found:
            v4_found = True
            ordered.insert(1, address)
        else:
            ordered.append(address)
    return ordered


class _TimedBackend(httpcore.AsyncNetworkBackend):
    """httpcore's anyio backend, except that it resolves the hostname itself.

    httpcore looks up and connects in one `connect_tcp` call, which gives no
    way to tell a slow resolver from a slow server. Resolving here times the
    two apart; the connect then goes to the resolved addresses the way anyio
    would have gone to them, Happy Eyeballs included.
    """

    def __init__(self) -> None:
        self._anyio = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        # Outside a probe nobody reads the times, so a throwaway takes them.
        stopwatch = _stopwatch.get() or _Stopwatch()
        try:
            # One deadline for the lookup and the connect, as anyio had.
            with anyio.fail_after(timeout):
                started = time.perf_counter()
                infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                resolved = time.perf_counter()
                stopwatch.add("dns_ms", resolved - started)
                stream = await self._connect_first(
                    _connect_order(infos), port, local_address, socket_options
                )
                stopwatch.add("connect_ms", time.perf_counter() - resolved)
                return stream
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(str(exc)) from exc
        except OSError as exc:  # includes socket.gaierror: the name did not resolve
            raise httpcore.ConnectError(str(exc)) from exc

    async def _connect_first(
        self,
        addresses: list[str],
        port: int,
        local_address: str | None,
        socket_options,
    ) -> httpcore.AsyncNetworkStream:
        """Connect to whichever address answers first (RFC 8305), as anyio does:
        start on the next address once the previous one has failed or has been
        trying for HAPPY_EYEBALLS_DELAY, and keep the first to connect."""
        winner: httpcore.AsyncNetworkStream | None = None
        errors: list[httpcore.ConnectError] = []

        async def attempt(address: str, failed: anyio.Event) -> None:
            nonlocal winner
            try:
                stream = await self._anyio.connect_tcp(
                    address,
                    port,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                errors.append(exc)
                failed.set()
                return
            if winner is not None:
                await stream.aclose()  # another address got there first
                return
            winner = stream
            tg.cancel_scope.cancel()

        async with anyio.create_task_group() as tg:
            for address in addresses:
                failed = anyio.Event()
                tg.start_soon(attempt, address, failed)
                with anyio.move_on_after(HAPPY_EYEBALLS_DELAY):
                    await failed.wait()
        if winner is None:
            raise errors[-1]
        return winner

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options=None
    ) -> httpcore.AsyncNetworkStream:
        return await self._anyio.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._anyio.sleep(seconds)


def new_client() -> httpx.AsyncClient:
    """An HTTP client for probes, one that can time a check's DNS lookup and
    TCP connect. Share one across many checks so connections are reused."""
    transport = httpx.AsyncHTTPTransport()
    # httpx has no setting for the network backend. Its connection pool reads
    # this attribute each time it opens a connection.
    transport._pool._network_backend = _TimedBackend()
    return httpx.AsyncClient(transport=transport, follow_redirects=True)


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
    timings: Timings = field(default_factory=Timings)
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


def _caused_by(exc: BaseException | None, kind: type[BaseException]) -> bool:
    """Whether `kind` is anywhere in the chain of exceptions behind `exc`."""
    while exc is not None:
        if isinstance(exc, kind):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _classify(exc: Exception) -> tuple[str, str]:
    """Turn an httpx exception into (error_type, human explanation)."""
    match exc:
        case httpx.ConnectTimeout():
            return "connect_timeout", "Timed out establishing a TCP connection."
        case httpx.ReadTimeout():
            return "read_timeout", "Connected, but the server sent no response in time."
        case httpx.WriteTimeout() | httpx.PoolTimeout():
            return "timeout", f"Request timed out ({type(exc).__name__})."
        case httpx.ConnectError() if _caused_by(exc, socket.gaierror):
            return "dns_error", f"DNS lookup failed: {exc}"
        case httpx.ConnectError() if _caused_by(exc, ssl.SSLError):
            return "tls_error", f"TLS handshake failed: {exc}"
        case httpx.ConnectError():
            # Refused, reset, or no route to the host.
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

    A caller running many checks should pass one shared `new_client()`, so
    connections are reused. When the site's certificate is due for a read, the
    same call also fills `result.cert`.
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
        client = new_client()

    stopwatch = _Stopwatch()
    token = _stopwatch.set(stopwatch)
    started = time.perf_counter()
    checked_at = datetime.now(UTC)
    try:
        response = await client.request(
            website.method.upper(),
            website.url,
            timeout=website.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            extensions={"trace": stopwatch.trace},
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
            timings=stopwatch.timings(),
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
            timings=stopwatch.timings(),
        )
    finally:
        _stopwatch.reset(token)
        if owns_client:
            await client.aclose()
