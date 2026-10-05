"""What every probe shares: its input, its result, and the step trace.

A probe never raises for its target being down; that is a result. The one
exception is `IcmpUnavailableError`: this server being unable to ping says
nothing about the host. Each probe also records its steps (resolve, policy,
connect, ...), which a scheduled check ignores and diagnose shows.
"""

import errno
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import anyio

from app.monitoring import egress
from app.monitoring.infra.aws import client
from app.monitoring.infra.aws.models import InfraCheckType, ResourceKind

#: Error types that mean the packets never got there (a security group, a
#: NACL, a route): what marks a whole VPC unreachable when most checks show it.
CONNECT_FAILURES = frozenset(
    {"connect_timeout", "no_route", "no_reply", "unreachable", "network_error"}
)
#: Failures that never tried to connect, and so say nothing either way about
#: whether the VPC can be reached.
NOT_ABOUT_REACH = frozenset(
    {"blocked_address", "no_address", "dns_error", "no_instances", "aws_error", "db_not_found", "db_unavailable"}
)


@dataclass(slots=True)
class Step:
    step: str
    ok: bool | None = None
    skipped: bool = True
    time_ms: int | None = None
    error_type: str | None = None
    detail: str | None = None


class Trace:
    """The steps of one probe, in order. Steps after a failure stay skipped."""

    def __init__(self, names: list[str]) -> None:
        self.steps = [Step(step=name) for name in names]

    def _get(self, name: str) -> Step:
        return next(step for step in self.steps if step.step == name)

    def ok(self, name: str, detail: str | None = None, ms: int | None = None) -> None:
        step = self._get(name)
        step.ok, step.skipped, step.detail, step.time_ms = True, False, detail, ms

    def fail(self, name: str, error_type: str, detail: str, ms: int | None = None) -> None:
        step = self._get(name)
        step.ok, step.skipped = False, False
        step.error_type, step.detail, step.time_ms = error_type, detail, ms

    @property
    def failed_step(self) -> str | None:
        return next((step.step for step in self.steps if step.ok is False), None)


@dataclass(slots=True)
class ProbeTarget:
    """Everything one probe needs, read from the database beforehand so the
    probe itself touches no session."""

    check_type: InfraCheckType
    settings: dict
    timeout: float
    kind: ResourceKind
    resource_name: str
    #: Instance id, load balancer ARN, Auto Scaling group name, or DB
    #: instance identifier.
    aws_id: str
    #: Private IP (or the public one, when the check asks), or DNS name, as
    #: AWS last said; none for an Auto Scaling group, whose instances are read
    #: at each run.
    address: str | None
    aws_detail: dict
    #: Its VPC's account and region, for the checks that ask AWS.
    region: client.Region
    scope: egress.Scope

    @property
    def missing_address(self) -> str:
        """Why there is no address to connect to."""
        if self.settings.get("use_public_ip"):
            return "This instance has no public IP: AWS gives it none while it is stopped, or without an Elastic IP."
        return "AWS gave this resource no address."


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    checked_at: datetime
    summary: str
    response_time_ms: int | None = None
    dns_ms: int | None = None
    connect_ms: int | None = None
    tls_ms: int | None = None
    error: str | None = None
    error_type: str | None = None
    #: The IP address connected to.
    address: str | None = None
    #: The type's group: `ping`, `tcp`, `http`, `targets`, `instances`,
    #: `status` or `metrics`.
    detail: dict | None = None
    #: The latest figures, for the resource's page.
    snapshot: dict | None = None
    #: Problems this run found, as kind -> detail; a problem counts once it
    #: shows INFRA_PROBLEM_CHECKS runs in a row.
    problems: dict[str, str] = field(default_factory=dict)
    #: The type's own figure, rolled up hourly.
    metric: float | None = None
    steps: list[Step] = field(default_factory=list)

    @property
    def connect_failure(self) -> bool:
        return not self.ok and self.error_type in CONNECT_FAILURES

    @property
    def tells_reach(self) -> bool:
        """Whether this result says anything about the VPC being reachable."""
        return self.ok or self.error_type not in NOT_ABOUT_REACH

    def as_last_result(self) -> dict:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at.isoformat(),
            "response_time_ms": self.response_time_ms,
            "summary": self.summary,
            "error": self.error,
            "error_type": self.error_type,
            "connect_failure": self.connect_failure,
        }


class ProbeFailure(Exception):
    """Raised inside a probe to end it with a failed result."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def now() -> datetime:
    return datetime.now(UTC)


def split_host_port(address: str, default_port: int | None = None) -> tuple[str, int | None]:
    """`host:port`, `[v6]:port` or a bare host."""
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
    if address.count(":") == 1:
        host, _, port = address.partition(":")
        if port.isdigit():
            return host, int(port)
    return address, default_port


async def resolve_and_vet(
    host: str, scope: egress.Scope, timeout: float, trace: Trace
) -> tuple[list[str], int | None]:
    """The addresses of `host` the policy lets this probe reach, and the time
    the lookup took (None for an IP address). Fills the resolve and policy
    steps; raises ProbeFailure on either."""
    dns_ms = None
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
        addresses = [host]
        trace.ok("resolve", f"{host} is an IP address")
    except OSError:
        started = time.perf_counter()
        try:
            with anyio.fail_after(min(timeout, 5)):
                infos = await anyio.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except TimeoutError as exc:
            trace.fail("resolve", "dns_error", f"{host} did not resolve in time", ms_since(started))
            raise ProbeFailure("dns_error", f"DNS lookup of {host} timed out.") from exc
        except OSError as exc:
            trace.fail("resolve", "dns_error", f"{host}: {exc}", ms_since(started))
            raise ProbeFailure("dns_error", f"DNS lookup of {host} failed: {exc}") from exc
        dns_ms = ms_since(started)
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
        trace.ok("resolve", f"{host} → {', '.join(addresses)}", dns_ms)
    try:
        allowed = egress.vet(addresses, scope)
    except egress.BlockedAddressError as exc:
        trace.fail("policy", "blocked_address", exc.reason)
        raise ProbeFailure("blocked_address", f"Refused by the egress policy: {exc.reason}") from exc
    trace.ok("policy", policy_note(allowed[0], scope))
    return allowed, dns_ms


def policy_note(address: str, scope: egress.Scope) -> str:
    """Why the policy let `address` through, for the policy step."""
    if egress.is_private(address):
        return f"{address} is inside {scope.label}"
    return f"{address} is a public address, allowed for a check over the internet"


def classify_connect_error(exc: BaseException) -> tuple[str, str]:
    """(error_type, explanation) for a failed TCP connect."""
    if isinstance(exc, TimeoutError):
        return "connect_timeout", "No answer: the packets were dropped (security group, NACL or route)."
    if isinstance(exc, ConnectionRefusedError):
        return "connect_refused", "Connection refused: the host answered, but nothing listens on that port."
    if isinstance(exc, ssl.SSLError):
        return "tls_error", f"TLS handshake failed: {exc}"
    if isinstance(exc, OSError) and exc.errno in {errno.EHOSTUNREACH, errno.ENETUNREACH}:
        return "no_route", f"No route to the host: {exc.strerror or exc}"
    if isinstance(exc, OSError):
        return "connect_error", f"Could not connect: {exc.strerror or exc}"
    return type(exc).__name__, str(exc) or type(exc).__name__


def tls_context(verify: bool = False) -> ssl.SSLContext:
    """TLS without verification unless asked: servers and load balancers
    present certificates for names the probe does not connect by."""
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def failed(
    trace: Trace,
    checked_at: datetime,
    started: float,
    error_type: str,
    message: str,
    **extra,
) -> ProbeResult:
    return ProbeResult(
        ok=False,
        checked_at=checked_at,
        summary=message,
        response_time_ms=ms_since(started),
        error=message,
        error_type=error_type,
        steps=trace.steps,
        **extra,
    )
