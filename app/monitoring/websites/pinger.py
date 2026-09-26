"""ICMP echo probes ("ping"): whether a host answers at the IP level, how fast,
and how many packets go missing on the way.

A check sends `count` echo requests PACKET_INTERVAL apart, listening all the
while, then waits up to `timeout` after the last one for the replies still
out. A silent host so costs about `(count - 1) * PACKET_INTERVAL + timeout`,
not the `count * timeout` of waiting out each request before sending the next.
A reply slower than `timeout` counts as lost, whichever request it answers.

Pings go over unprivileged ICMP (datagram) sockets unless `privileged` is set.
Linux allows those only to the groups in `net.ipv4.ping_group_range`, which
docker-compose.yml opens for the API container; macOS allows them to anyone.
Privileged pings use raw sockets, which need root or CAP_NET_RAW.

A host that is silent, does not resolve or is unreachable is a normal outcome,
returned as a `PingResult`. The one thing that raises is this server being
unable to open an ICMP socket at all (`IcmpUnavailableError`): that says
nothing about the host, so it must not be recorded as the host being down.
"""

import asyncio
import contextlib
import ipaddress
import socket
import time
from dataclasses import dataclass

import anyio
from icmplib import AsyncSocket, ICMPRequest, ICMPv4Socket, ICMPv6Socket
from icmplib.exceptions import ICMPError, ICMPSocketError, SocketPermissionError, TimeExceeded
from icmplib.utils import unique_identifier

#: Seconds between one check's echo requests. Gentle enough that a router
#: rate-limiting ICMP does not look like one dropping packets.
PACKET_INTERVAL = 0.5
#: Bytes of payload per request, as the `ping` command sends by default.
PAYLOAD_SIZE = 56
#: Seconds allowed for resolving a host name, apart from the reply timeout.
DNS_TIMEOUT = 5.0
#: ICMP echo *request* types (v4, v6). A raw socket sees its own when a host
#: pings itself, and anyone else's pinging this server.
_ECHO_REQUESTS = frozenset({8, 128})


class IcmpUnavailableError(Exception):
    """This server cannot send pings at all: a Watchly setup problem, not the host's."""


def is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def format_rtt(ms: float) -> str:
    """`0.42 ms`, `12.3 ms`, `184 ms`: about three significant figures."""
    if ms < 10:
        return f"{ms:.2f} ms"
    if ms < 100:
        return f"{ms:.1f} ms"
    return f"{ms:.0f} ms"


def _pings(count: int) -> str:
    return "1 ping" if count == 1 else f"{count} pings"


@dataclass(slots=True)
class PingStats:
    """What one check's echo requests found, round trips in milliseconds."""

    #: The IP address pinged: the host itself, or what its name resolved to.
    address: str
    #: Echo requests attempted, including any the network stack refused to send.
    sent: int
    #: Requests answered within the timeout.
    received: int
    min_ms: float | None = None
    avg_ms: float | None = None
    max_ms: float | None = None
    #: Mean difference between consecutive round trips; None under two replies.
    jitter_ms: float | None = None

    @property
    def loss_percent(self) -> float:
        """Share of requests that went unanswered."""
        if not self.sent:
            return 100.0
        return round(100 * (self.sent - self.received) / self.sent, 1)

    @property
    def summary(self) -> str:
        """One line, e.g. `Replied in 12.3 ms, 40% packet loss`."""
        if self.avg_ms is None:
            return f"No reply to {_pings(self.sent)}"
        text = f"Replied in {format_rtt(self.avg_ms)}"
        if self.received < self.sent:
            text += f", {self.loss_percent:g}% packet loss"
        return text


def _stats(address: str, sent: int, rtts: list[float]) -> PingStats:
    """Figures from the round trips received, in the order they were sent."""
    if not rtts:
        return PingStats(address=address, sent=sent, received=0)
    jitter = (
        sum(abs(later - earlier) for earlier, later in zip(rtts, rtts[1:])) / (len(rtts) - 1)
        if len(rtts) > 1
        else None
    )
    return PingStats(
        address=address,
        sent=sent,
        received=len(rtts),
        min_ms=round(min(rtts), 3),
        avg_ms=round(sum(rtts) / len(rtts), 3),
        max_ms=round(max(rtts), 3),
        jitter_ms=round(jitter, 3) if jitter is not None else None,
    )


@dataclass(slots=True)
class PingResult:
    """One check's outcome.

    `stats` is None when nothing could be sent: the name did not resolve, or no
    socket could be opened for its address family. `error` says why no reply
    came back, and is set only when none did.
    """

    stats: PingStats | None = None
    #: Time spent resolving the host name; None when it was an IP address.
    dns_ms: int | None = None
    error: str | None = None
    error_type: str | None = None

    @property
    def is_up(self) -> bool:
        return self.stats is not None and self.stats.received > 0


async def _resolve(host: str) -> str:
    """The address to ping for a host name: the first that getaddrinfo gives.

    AI_ADDRCONFIG leaves out IPv6 addresses when this server has no IPv6 of its
    own (and IPv4 likewise), so a dual-stack name is not pinged over a protocol
    this server cannot use.
    """
    infos = await anyio.getaddrinfo(
        host, None, type=socket.SOCK_DGRAM, flags=socket.AI_ADDRCONFIG
    )
    return str(infos[0][4][0])


def _permission_hint(privileged: bool) -> str:
    if privileged:
        return (
            "This server cannot open raw ICMP sockets: they need root or "
            "CAP_NET_RAW. Grant it, or set PING_PRIVILEGED=false to use "
            "unprivileged ICMP sockets."
        )
    return (
        "This server does not allow unprivileged ICMP sockets. On Linux, add the "
        "API's group to the net.ipv4.ping_group_range sysctl (docker-compose.yml "
        "does this for the container), or set PING_PRIVILEGED=true and grant "
        "CAP_NET_RAW."
    )


async def ping(
    host: str, *, count: int, timeout: float, privileged: bool = False
) -> PingResult:
    """Ping `host`, an IP address or a name, `count` times.

    Raises `IcmpUnavailableError` only when this server cannot send pings at
    all; every problem with the host itself comes back in the result.
    """
    address = host
    dns_ms = None
    if not is_ip_address(host):
        started = time.perf_counter()
        try:
            with anyio.fail_after(DNS_TIMEOUT):
                address = await _resolve(host)
        except TimeoutError:
            return PingResult(
                error=f"DNS lookup timed out after {DNS_TIMEOUT:g}s.", error_type="dns_error"
            )
        except OSError as exc:  # socket.gaierror: the name did not resolve
            return PingResult(error=f"DNS lookup failed: {exc}", error_type="dns_error")
        dns_ms = round((time.perf_counter() - started) * 1000)

    socket_class = ICMPv6Socket if ":" in address else ICMPv4Socket
    try:
        icmp_socket = socket_class(privileged=privileged)
    except SocketPermissionError as exc:
        raise IcmpUnavailableError(_permission_hint(privileged)) from exc
    except ICMPSocketError as exc:
        # E.g. an IPv6 address, and no IPv6 on this server at all.
        return PingResult(
            dns_ms=dns_ms,
            error=f"Could not open an ICMP socket for {address}: {exc}",
            error_type="network_error",
        )

    requests: dict[int, ICMPRequest] = {}
    rtts: dict[int, float] = {}
    #: The last ICMP error a router or the host sent back instead of a reply.
    refusal: ICMPError | None = None
    send_error: ICMPSocketError | None = None
    answered = asyncio.Event()

    async def listen(sock: AsyncSocket) -> None:
        """Match replies to requests until cancelled."""
        nonlocal refusal
        budget = count * PACKET_INTERVAL + timeout + 1
        while True:
            try:
                reply = await sock.receive(None, budget)
            except ICMPSocketError:  # includes TimeoutExceeded
                return
            request = requests.get(reply.sequence)
            if (
                request is None
                or reply.id != request.id
                or reply.type in _ECHO_REQUESTS
                or reply.sequence in rtts  # a duplicate
            ):
                continue
            try:
                reply.raise_for_status()
            except ICMPError as exc:
                refusal = exc
                continue
            rtt_ms = (reply.time - request.time) * 1000
            if rtt_ms <= timeout * 1000:
                rtts[reply.sequence] = rtt_ms
                answered.set()

    ident = unique_identifier()
    with AsyncSocket(icmp_socket) as sock:
        listener = asyncio.create_task(listen(sock))
        try:
            for sequence in range(count):
                if sequence:
                    await asyncio.sleep(PACKET_INTERVAL)
                request = ICMPRequest(
                    destination=address,
                    id=ident,
                    sequence=sequence,
                    payload_size=PAYLOAD_SIZE,
                )
                try:
                    # Unprivileged on Linux, this also swaps `request.id` for
                    # the one the kernel put on the wire.
                    sock.send(request)
                except ICMPSocketError as exc:  # e.g. no route to the host
                    send_error = exc
                    continue
                requests[sequence] = request

            # Wait out the last request's timeout, or less once all are in.
            deadline = time.monotonic() + timeout
            while len(rtts) < len(requests):
                answered.clear()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(answered.wait(), remaining)
                except TimeoutError:
                    break
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

    stats = _stats(address, count, [rtts[sequence] for sequence in sorted(rtts)])
    result = PingResult(stats=stats, dns_ms=dns_ms)
    if stats.received:
        return result
    # Nothing came back: say why, as precisely as the network let on.
    if refusal is not None:
        result.error = f"{refusal}."
        result.error_type = "ttl_exceeded" if isinstance(refusal, TimeExceeded) else "unreachable"
    elif send_error is not None and not requests:
        result.error = f"Could not send to {address}: {send_error}"
        result.error_type = "network_error"
    else:
        result.error = f"No reply to {_pings(count)} within {timeout:g}s."
        result.error_type = "no_reply"
    return result
