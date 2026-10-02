"""Performs a single probe of a website: an HTTP request, for a ping check a
round of ICMP echo requests (see `pinger.py`), or for a DNS check a record
looked up at several resolvers (see `dns_probe.py`).

Every failure mode — DNS, TLS, connect, timeout, unexpected status, a silent
host, a record that is missing or wrong — comes back as a :class:`CheckResult`. This module never raises for a
site being down; that is a normal outcome, not an error. The one exception is
`IcmpUnavailableError`: this server being unable to ping at all says nothing
about the host.
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
from app.monitoring.websites.cdn import CdnInfo
from app.monitoring.websites.cdn import detect as detect_cdn
from app.monitoring.websites.dns_probe import DnsResult, configured_resolvers, lookup
from app.monitoring.websites.models import CheckType, DnsRecordType, Website
from app.monitoring.websites.pinger import PingStats, ping
from app.monitoring.websites.server_info import ServerInfo
from app.monitoring.websites.server_info import lookup as lookup_server
from app.monitoring.websites.security_headers import capture as capture_security_headers
from app.monitoring.websites.domain_lookup import DomainInfo, lookup_domain, lookup_host

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
    "content-encoding",
    "server-timing",
    "alt-svc",
)

#: Hops kept of a redirect chain, and certificates kept of a presented chain.
MAX_REDIRECT_HOPS = 20
MAX_CHAIN_CERTS = 6

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
        #: The address the probe's first connection went to; None when it
        #: reused one opened earlier.
        self.address: str | None = None

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


def _peer_address(stream) -> str | None:
    """The IP address a connected stream is joined to, if it says."""
    try:
        peer = stream.get_extra_info("server_addr")
    except Exception:  # noqa: BLE001 - a hint; some streams do not offer it
        return None
    return str(peer[0]) if peer else None


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
                if stopwatch.address is None:
                    stopwatch.address = _peer_address(stream)
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
    #: Who it was issued to: the common name, else the first SAN.
    subject: str | None = None
    #: The names it covers (DNS names, then IP addresses), up to MAX_SANS.
    sans: list[str] = field(default_factory=list)
    valid_from: datetime | None = None
    #: What the handshake settled on, e.g. `TLSv1.3`.
    tls_version: str | None = None
    #: The cipher suite it settled on, e.g. `TLS_AES_256_GCM_SHA384 (256 bit)`.
    cipher: str | None = None
    #: The protocol agreed through ALPN: `h2`, `http/1.1`, or None when the
    #: server did not answer the offer.
    alpn: str | None = None
    #: Every certificate the server sent, leaf first, as `chain_entry` makes them.
    chain: list[dict] = field(default_factory=list)
    error: str | None = None


#: SANs kept per certificate. A shared CDN certificate can list hundreds.
MAX_SANS = 100

#: A failed domain lookup is tried again after this long, rather than a day.
DOMAIN_RETRY_SECONDS = 3600


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
    #: Each hop of a redirect chain, as `redirect_chain` writes them; empty
    #: when the first response was the answer.
    redirects: list[dict] = field(default_factory=list)
    timings: Timings = field(default_factory=Timings)
    #: Set only on the checks that also read the certificate; see
    #: `certificate_check_due`. None means "not looked at", not "no certificate".
    cert: CertInfo | None = None
    #: Set only on the checks that also looked up the domain; see
    #: `domain_check_due`. None means "not looked up this time".
    domain: DomainInfo | None = None
    #: HTTP checks that came up: the final response's security headers, as
    #: `security_headers.capture` stores them.
    security_headers: dict | None = None
    #: Set only on the HTTP checks that came up and were due to look for a
    #: CDN; see `cdn_check_due`. None means "not looked at".
    cdn: CdnInfo | None = None
    #: HTTP checks that got a response: the address its host answered on (the
    #: first hop's, when redirected). None when unknown.
    ip_address: str | None = None
    #: Set only on the HTTP checks that came up and were due to look up the
    #: server; see `server_check_due`. None means "not looked at".
    server: ServerInfo | None = None
    #: Set only on ping checks that got as far as sending.
    ping: PingStats | None = None
    #: Set on every DNS check: what each resolver answered.
    dns: DnsResult | None = None

    @property
    def summary(self) -> str:
        """One-line human description, used as the alert headline."""
        if self.is_up and self.ping is not None:
            return self.ping.summary
        if self.is_up and self.dns is not None:
            return self.dns.summary
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
    if (
        not settings.SSL_CHECK_ENABLED
        or website.check_type is not CheckType.HTTP
        or not website.url.lower().startswith("https://")
    ):
        return False
    if website.ssl_checked_at is None:
        return True
    elapsed = ((now or datetime.now(UTC)) - website.ssl_checked_at).total_seconds()
    return elapsed >= settings.SSL_CHECK_INTERVAL_SECONDS


def domain_host(website: Website) -> str | None:
    """The host whose domain registration to look up: an HTTP check's host, a
    ping check's host name, a DNS check's domain. None for an IP address."""
    if website.check_type is CheckType.HTTP:
        return lookup_host(urlsplit(website.url).hostname)
    return lookup_host(website.url)


def cdn_check_due(website: Website, now: datetime | None = None) -> bool:
    """Whether this check should also look for a CDN: the first time, then
    every CDN_CHECK_INTERVAL_SECONDS. It rides on a successful response, so a
    down site simply waits."""
    if not settings.CDN_CHECK_ENABLED or website.check_type is not CheckType.HTTP:
        return False
    if website.cdn_checked_at is None:
        return True
    elapsed = ((now or datetime.now(UTC)) - website.cdn_checked_at).total_seconds()
    return elapsed >= settings.CDN_CHECK_INTERVAL_SECONDS


def server_check_due(website: Website, ip: str | None, now: datetime | None = None) -> bool:
    """Whether this check should also look up the server behind `ip`: the first
    time, whenever an address not seen before answers, then every
    SERVER_CHECK_INTERVAL_SECONDS. It rides on a successful response."""
    if (
        not settings.SERVER_CHECK_ENABLED
        or website.check_type is not CheckType.HTTP
        or ip is None
    ):
        return False
    if website.server_checked_at is None or not website.server:
        return True
    if ip not in website.server.get("ips_seen", []):
        return True
    elapsed = ((now or datetime.now(UTC)) - website.server_checked_at).total_seconds()
    return elapsed >= settings.SERVER_CHECK_INTERVAL_SECONDS


def domain_check_due(website: Website, now: datetime | None = None) -> bool:
    """Whether this check should also look up the site's domain.

    Once a day (DOMAIN_CHECK_INTERVAL_SECONDS) is plenty for a date that moves
    by the year; a failed lookup is retried after DOMAIN_RETRY_SECONDS.
    """
    if not settings.DOMAIN_CHECK_ENABLED or domain_host(website) is None:
        return False
    if website.domain_checked_at is None:
        return True
    interval = settings.DOMAIN_CHECK_INTERVAL_SECONDS
    if website.domain_error:
        interval = min(interval, DOMAIN_RETRY_SECONDS)
    elapsed = ((now or datetime.now(UTC)) - website.domain_checked_at).total_seconds()
    return elapsed >= interval


def _name_attribute(name: x509.Name, *oids) -> str | None:
    for oid in oids:
        attributes = name.get_attributes_for_oid(oid)
        if attributes:
            return str(attributes[0].value)[:255]
    return None


def _issuer_name(cert: x509.Certificate) -> str | None:
    return _name_attribute(cert.issuer, NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME)


def _alternative_names(cert: x509.Certificate) -> list[str]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    names = [
        *san.get_values_for_type(x509.DNSName),
        *(str(ip) for ip in san.get_values_for_type(x509.IPAddress)),
    ]
    return names[:MAX_SANS]


def chain_entry(cert: x509.Certificate) -> dict:
    """One certificate of a presented chain, for the site's page."""
    return {
        "subject": _name_attribute(cert.subject, NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME),
        "issuer": _issuer_name(cert),
        "expires_at": cert.not_valid_after_utc.isoformat(),
    }


def redirect_chain(response: httpx.Response) -> list[dict]:
    """The hops that led to `response`, in order, each as the URL asked for,
    the status it answered and where it pointed. Empty without redirects."""
    return [
        {
            "url": str(hop.url),
            "status": hop.status_code,
            "location": hop.headers.get("location"),
        }
        for hop in response.history[:MAX_REDIRECT_HOPS]
    ]


def cert_info(
    cert: x509.Certificate,
    tls_version: str | None = None,
    *,
    cipher: str | None = None,
    alpn: str | None = None,
    chain: list[dict] | None = None,
) -> CertInfo:
    """What the site's page and alerts show of a certificate."""
    sans = _alternative_names(cert)
    return CertInfo(
        expires_at=cert.not_valid_after_utc,
        valid_from=cert.not_valid_before_utc,
        issuer=_issuer_name(cert),
        subject=_name_attribute(cert.subject, NameOID.COMMON_NAME)
        or (sans[0] if sans else None),
        sans=sans,
        tls_version=tls_version,
        cipher=cipher,
        alpn=alpn,
        chain=chain or [],
    )


def _read_chain(ssl_object) -> list[dict]:
    """The certificates the server sent, leaf first. Empty where the runtime
    cannot say (before Python 3.13) or the read fails."""
    try:
        return [
            chain_entry(x509.load_der_x509_certificate(der))
            for der in ssl_object.get_unverified_chain()[:MAX_CHAIN_CERTS]
        ]
    except Exception:  # noqa: BLE001 - the chain is a bonus, never a failed read
        return []


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
    # Offering HTTP/2 only learns whether the server speaks it; the checks
    # themselves stay on HTTP/1.1.
    context.set_alpn_protocols(["h2", "http/1.1"])
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
            tls_version = ssl_object.version() if ssl_object else None
            cipher = None
            if ssl_object and (suite := ssl_object.cipher()):
                cipher = f"{suite[0]} ({suite[2]} bit)"
            alpn = ssl_object.selected_alpn_protocol() if ssl_object else None
            chain = _read_chain(ssl_object) if ssl_object else []
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if not der:
            return CertInfo(error="The server presented no certificate.")
        return cert_info(
            x509.load_der_x509_certificate(der),
            tls_version,
            cipher=cipher,
            alpn=alpn,
            chain=chain,
        )
    except Exception as exc:  # noqa: BLE001 - a failed read is data, not a crash
        logger.info("Certificate read failed for %s: %s", url, exc)
        return CertInfo(error=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)


async def check_website(
    website: Website, client: httpx.AsyncClient | None = None
) -> CheckResult:
    """Probe `website` and report what happened.

    A failed probe is repeated up to `website.retries_on_failure` times,
    CHECK_RETRY_DELAY_SECONDS apart, and only the last result is returned, so a
    one-off blip neither counts as a failed check nor raises an alert.

    A caller running many checks should pass one shared `new_client()`, so
    connections are reused; ping and DNS checks do not use it. When the site's
    certificate is due for a read, the same call also fills `result.cert`, and
    when its domain is due for a lookup, `result.domain`.
    """
    probe = _PROBES[website.check_type]
    result = await probe(website, client)
    for _ in range(website.retries_on_failure):
        if result.is_up:
            break
        await asyncio.sleep(settings.CHECK_RETRY_DELAY_SECONDS)
        result = await probe(website, client)
    if certificate_check_due(website, result.checked_at):
        result.cert = await probe_certificate(website.url, website.timeout_seconds)
    if domain_check_due(website, result.checked_at):
        result.domain = await lookup_domain(domain_host(website), client)
    return result


#: How much of a response body the content rules search, so one huge page
#: cannot cost more than a bounded slice of memory and time.
MAX_SEARCHED_BYTES = 1_048_576


def content_problem(website: Website, response: httpx.Response) -> tuple[str, str] | None:
    """`(error_type, explanation)` when the body breaks the site's content
    rules, else None. Case-sensitive, over the first MAX_SEARCHED_BYTES."""
    if not (website.must_contain or website.must_not_contain):
        return None
    body = response.content[:MAX_SEARCHED_BYTES].decode(
        response.encoding or "utf-8", errors="replace"
    )
    if website.must_contain and website.must_contain not in body:
        return "content_missing", f"Response did not contain {_quoted(website.must_contain)}."
    if website.must_not_contain and website.must_not_contain in body:
        return "content_forbidden", f"Response contained {_quoted(website.must_not_contain)}."
    return None


def _quoted(text: str) -> str:
    return f'"{text[:80]}…"' if len(text) > 80 else f'"{text}"'


async def _ping_probe(
    website: Website, client: httpx.AsyncClient | None = None
) -> CheckResult:
    """Ping the host in `website.url`. Up when any request is answered; the
    ones that are not are packet loss, which `MonitoringService` watches."""
    checked_at = datetime.now(UTC)
    outcome = await ping(
        website.url,
        count=website.ping_count,
        timeout=website.timeout_seconds,
        privileged=settings.PING_PRIVILEGED,
    )
    stats = outcome.stats
    if not outcome.is_up:
        logger.info("Ping failed for %s: %s", website.url, outcome.error)
    return CheckResult(
        is_up=outcome.is_up,
        checked_at=checked_at,
        # The average round trip; a failed ping has none.
        response_time_ms=round(stats.avg_ms) if stats and stats.avg_ms is not None else None,
        error=outcome.error,
        error_type=outcome.error_type,
        timings=Timings(dns_ms=outcome.dns_ms),
        ping=stats,
    )


async def _dns_probe(
    website: Website, client: httpx.AsyncClient | None = None
) -> CheckResult:
    """Look up the record of the domain in `website.url` at every configured
    resolver. See `dns_probe` for when that counts as up."""
    checked_at = datetime.now(UTC)
    outcome = await lookup(
        website.url,
        website.dns_record_type or DnsRecordType.A,
        resolvers=configured_resolvers(settings.dns_resolvers),
        timeout=website.timeout_seconds,
        expected=website.dns_expected_values,
    )
    if not outcome.is_up:
        logger.info("DNS check failed for %s: %s", website.url, outcome.error)
    return CheckResult(
        is_up=outcome.is_up,
        checked_at=checked_at,
        response_time_ms=outcome.response_time_ms,
        error=outcome.error,
        error_type=outcome.error_type,
        dns=outcome,
    )


def _response_address(response: httpx.Response, stopwatch: _Stopwatch) -> str | None:
    """The IP address the site's own host answered on: the first hop's, so a
    redirect to another host does not stand in for it. Read off the response's
    connection, which still works when the connection was reused; failing
    that, off the first connection the probe opened."""
    first = response.history[0] if response.history else response
    stream = first.extensions.get("network_stream")
    return (_peer_address(stream) if stream is not None else None) or stopwatch.address


async def _http_probe(
    website: Website, client: httpx.AsyncClient | None = None
) -> CheckResult:
    owns_client = client is None
    if client is None:
        client = new_client()

    # The site's own headers win, User-Agent included; names match without case.
    headers = httpx.Headers({"User-Agent": USER_AGENT})
    headers.update(website.outgoing_headers())

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
            headers=headers,
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
        status_ok = response.status_code == website.expected_status
        problem = (
            None
            if not status_ok
            else content_problem(website, response)
        )
        is_up = status_ok and problem is None
        ip = _response_address(response, stopwatch)
        return CheckResult(
            is_up=is_up,
            checked_at=checked_at,
            status_code=response.status_code,
            reason=response.reason_phrase,
            response_time_ms=elapsed_ms,
            error=(
                None
                if is_up
                else problem[1]
                if problem
                else f"Expected HTTP {website.expected_status}, got {response.status_code}."
            ),
            error_type=(
                None if is_up else problem[0] if problem else "unexpected_status"
            ),
            final_url=str(response.url),
            redirected=str(response.url) != website.url,
            content_length=len(response.content) if response.content else 0,
            redirects=redirect_chain(response),
            headers={
                name: response.headers[name]
                for name in DIAGNOSTIC_HEADERS
                if name in response.headers
            },
            # Only the page as normally served: an error page from a proxy
            # would make headers seem to come and go.
            security_headers=(
                capture_security_headers(str(response.url), response.headers)
                if is_up
                else None
            ),
            cdn=(
                await detect_cdn(str(response.url), response.headers)
                if is_up and cdn_check_due(website, checked_at)
                else None
            ),
            ip_address=ip,
            server=(
                await lookup_server(
                    ip,
                    response.headers.get("alt-svc"),
                    (website.server or {}).get("ips_seen"),
                )
                if is_up and server_check_due(website, ip, checked_at)
                else None
            ),
            timings=stopwatch.timings(),
        )
    finally:
        _stopwatch.reset(token)
        if owns_client:
            await client.aclose()


_PROBES = {
    CheckType.HTTP: _http_probe,
    CheckType.PING: _ping_probe,
    CheckType.DNS: _dns_probe,
}
