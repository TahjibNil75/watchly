"""`GET` a path on a server's private (or public) IP, or a load balancer's DNS
name.

For a load balancer this tests the path a request takes: listener, rule, a
healthy target. Redirects are followed, each hop still within the check's
scope: the VPC's CIDRs, plus public addresses for a check over the internet.
A response slower than `slow_threshold_ms` is the `slow_response` problem.
"""

import time

from app.monitoring.infra.aws.models import ResourceKind
from app.monitoring.infra.aws.probes.base import (
    ProbeFailure,
    ProbeResult,
    ProbeTarget,
    Trace,
    failed,
    now,
    resolve_and_vet,
    split_host_port,
)
from app.monitoring.websites.checker import new_client, timed_request

STEPS = ["resolve", "policy", "connect", "tls", "response"]
#: Body searched for `must_contain`.
MAX_SEARCHED_BYTES = 1_048_576
#: Which step an HTTP failure belongs to.
_FAILED_STEP = {
    "connect_timeout": "connect",
    "connect_refused": "connect",
    "connect_error": "connect",
    "no_route": "connect",
    "blocked_address": "policy",
    "dns_error": "resolve",
    "tls_error": "tls",
}


def url_for(target: ProbeTarget) -> tuple[str, str]:
    """The URL requested, and the host in it."""
    scheme = target.settings.get("scheme", "http")
    host, _ = split_host_port(target.address or "")
    port = target.settings.get("port") or (443 if scheme == "https" else 80)
    default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    shown_host = f"[{host}]" if ":" in host else host
    netloc = shown_host if default else f"{shown_host}:{port}"
    return f"{scheme}://{netloc}{target.settings.get('path', '/')}", host


async def probe(target: ProbeTarget) -> ProbeResult:
    trace = Trace(STEPS)
    checked_at, started = now(), time.perf_counter()
    if not target.address:
        trace.fail("resolve", "no_address", target.missing_address)
        return failed(trace, checked_at, started, "no_address", target.missing_address)
    url, host = url_for(target)
    scheme = target.settings.get("scheme", "http")

    # Resolved here too, so diagnose can say which address and which rule;
    # the request itself is vetted again on every hop by the client.
    try:
        await resolve_and_vet(host, target.scope, target.timeout, trace)
    except ProbeFailure as exc:
        return failed(trace, checked_at, started, exc.error_type, exc.message)

    headers = {}
    if target.settings.get("host_header"):
        headers["Host"] = target.settings["host_header"]
    async with new_client(target.scope, verify=target.settings.get("verify_tls", False)) as client:
        outcome = await timed_request(client, "GET", url, headers=headers, timeout=target.timeout)

    timings = outcome.timings
    group = {
        "address": outcome.address,
        "url": url,
        "status_code": None,
        "body_bytes": None,
        "final_url": None,
    }
    common = dict(
        dns_ms=timings.dns_ms,
        connect_ms=timings.connect_ms,
        tls_ms=timings.tls_ms,
        address=outcome.address,
    )
    if outcome.response is None:
        step = _FAILED_STEP.get(outcome.error_type or "", "response")
        for earlier in ("connect", "tls"):
            if earlier == step:
                break
            if earlier == "tls" and scheme != "https":
                continue
            trace.ok(earlier, ms=getattr(timings, f"{earlier}_ms"))
        trace.fail(step, outcome.error_type or "error", outcome.error or "")
        result = failed(
            trace, checked_at, started, outcome.error_type or "error", outcome.error or "No response.",
            detail={"http": group}, **common,
        )
        result.response_time_ms = outcome.elapsed_ms
        return result

    response = outcome.response
    trace.ok("connect", outcome.address, timings.connect_ms)
    if scheme == "https":
        trace.ok("tls", ms=timings.tls_ms)
    group.update(
        status_code=response.status_code,
        body_bytes=len(response.content),
        final_url=str(response.url) if str(response.url) != url else None,
    )
    expected = target.settings.get("expected_status", 200)
    problem = None
    if response.status_code != expected:
        problem = ("unexpected_status", f"Expected HTTP {expected}, got {response.status_code}.")
    elif needle := target.settings.get("must_contain"):
        body = response.content[:MAX_SEARCHED_BYTES].decode(response.encoding or "utf-8", errors="replace")
        if needle not in body:
            problem = ("content_missing", f"Response did not contain “{needle[:80]}”.")
    if problem:
        trace.fail("response", problem[0], f"HTTP {response.status_code}: {problem[1]}", outcome.elapsed_ms)
        result = failed(
            trace, checked_at, started, problem[0],
            f"HTTP {response.status_code} {response.reason_phrase}".strip()
            if problem[0] == "unexpected_status" else problem[1],
            detail={"http": group}, **common,
        )
        result.error = problem[1]
        result.response_time_ms = outcome.elapsed_ms
        return result

    trace.ok("response", f"HTTP {response.status_code} in {outcome.elapsed_ms} ms", timings.first_byte_ms)
    problems = {}
    slow = target.settings.get("slow_threshold_ms")
    if slow and outcome.elapsed_ms > slow:
        problems["slow_response"] = f"Answered in {outcome.elapsed_ms} ms (limit {slow} ms)"
    noun = "load balancer" if target.kind is ResourceKind.LOAD_BALANCER else "service"
    return ProbeResult(
        ok=True,
        checked_at=checked_at,
        summary=f"HTTP {response.status_code} in {outcome.elapsed_ms} ms",
        response_time_ms=outcome.elapsed_ms,
        detail={"http": group},
        snapshot={
            "address": outcome.address,
            "status_code": response.status_code,
            "response_ms": outcome.elapsed_ms,
            "what": noun,
        },
        problems=problems,
        steps=trace.steps,
        **common,
    )
