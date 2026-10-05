"""Does a port accept connections? Optionally a TLS handshake, and a banner.

A timeout means something dropped the packets; "connection refused" means the
host answered and nothing listens. The difference is where to look, so the
two are told apart.
"""

import asyncio
import contextlib
import time

from app.monitoring.infra.aws.probes.base import (
    ProbeFailure,
    ProbeResult,
    ProbeTarget,
    Trace,
    classify_connect_error,
    failed,
    ms_since,
    now,
    resolve_and_vet,
    split_host_port,
    tls_context,
)

STEPS = ["resolve", "policy", "connect", "tls", "banner"]
#: Bytes of banner read and kept.
BANNER_BYTES = 256


def target_host_port(target: ProbeTarget) -> tuple[str, int | None]:
    """The host to connect to, and the check's port."""
    host, _ = split_host_port(target.address or "")
    return host, target.settings.get("port")


async def probe(target: ProbeTarget) -> ProbeResult:
    trace = Trace(STEPS)
    checked_at, started = now(), time.perf_counter()
    host, port = target_host_port(target)
    if not host or not port:
        message = target.missing_address if not host else "No port set."
        trace.fail("resolve", "no_address", message)
        return failed(trace, checked_at, started, "no_address", message)

    try:
        addresses, dns_ms = await resolve_and_vet(host, target.scope, target.timeout, trace)
    except ProbeFailure as exc:
        return failed(trace, checked_at, started, exc.error_type, exc.message)

    address = addresses[0]
    group = {"address": address, "port": port, "connect_ms": None, "tls_ms": None, "banner": None}
    connect_started = time.perf_counter()
    writer = None
    try:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), target.timeout
            )
        except (OSError, TimeoutError) as exc:
            error_type, message = classify_connect_error(exc)
            trace.fail("connect", error_type, f"{address}:{port}: {message}", ms_since(connect_started))
            return failed(
                trace, checked_at, started, error_type, f"TCP {port}: {message}",
                dns_ms=dns_ms, address=address, detail={"tcp": group},
            )
        connect_ms = ms_since(connect_started)
        group["connect_ms"] = connect_ms
        trace.ok("connect", f"{address}:{port} accepted the connection", connect_ms)

        tls_ms = None
        if target.settings.get("tls"):
            tls_started = time.perf_counter()
            try:
                await asyncio.wait_for(
                    writer.start_tls(
                        tls_context(), server_hostname=host if host != address else None
                    ),
                    target.timeout,
                )
            except (OSError, TimeoutError) as exc:
                error_type, message = classify_connect_error(exc)
                error_type = "tls_error" if error_type != "connect_timeout" else "tls_timeout"
                trace.fail("tls", error_type, message, ms_since(tls_started))
                return failed(
                    trace, checked_at, started, error_type, f"TLS on {port}: {message}",
                    dns_ms=dns_ms, connect_ms=connect_ms, address=address, detail={"tcp": group},
                )
            tls_ms = ms_since(tls_started)
            group["tls_ms"] = tls_ms
            trace.ok("tls", "handshake completed", tls_ms)

        expected = target.settings.get("expect_banner")
        if expected:
            try:
                data = await asyncio.wait_for(reader.read(BANNER_BYTES), target.timeout)
            except (OSError, TimeoutError):
                data = b""
            banner = data.decode("utf-8", errors="replace").strip()
            group["banner"] = banner[:BANNER_BYTES] or None
            if expected not in banner:
                message = f"Banner did not contain “{expected}”" + (f": “{banner[:80]}”" if banner else ".")
                trace.fail("banner", "unexpected_banner", message)
                return failed(
                    trace, checked_at, started, "unexpected_banner", message,
                    dns_ms=dns_ms, connect_ms=connect_ms, tls_ms=tls_ms, address=address,
                    detail={"tcp": group},
                )
            trace.ok("banner", banner[:80])

        total = ms_since(started)
        return ProbeResult(
            ok=True,
            checked_at=checked_at,
            summary=f"TCP {port} open in {connect_ms} ms",
            response_time_ms=total,
            dns_ms=dns_ms,
            connect_ms=connect_ms,
            tls_ms=tls_ms,
            address=address,
            detail={"tcp": group},
            snapshot={"address": address, "port": port, "connect_ms": connect_ms},
            steps=trace.steps,
        )
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
