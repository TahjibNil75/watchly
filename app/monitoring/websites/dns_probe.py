"""DNS record probes: ask several resolvers for one record of a domain, and
judge whether they agree with each other and with what is expected.

Each resolver in DNS_RESOLVERS is queried directly and in parallel, so every
answer is that resolver's own view: a public resolver is its own anycast
network with its own caches. Together they show whether the world sees the
same records — a hijack, a poisoned cache or a change still propagating shows
up as resolvers that disagree.

A check is judged in two steps:

1. **Resolution**: at least half the resolvers must return the record. A name
   that does not exist (NXDOMAIN), has no such record, fails (SERVFAIL, as a
   validating resolver answers for broken DNSSEC) or times out at most of them
   is down.
2. **Expected values**, when any are pinned: every resolver that answers must
   return exactly them. One resolver serving other records, or saying the name
   does not exist, is down. Resolvers that time out or fail are tolerated,
   since that says more about the resolver than about the domain.

With nothing pinned, the records the resolvers agree on are what
`MonitoringService` compares from one check to the next.

A resolver that cannot be reached is an answer like any other, not an
exception: this module never raises for the domain being broken.
"""

import asyncio
import ipaddress
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

import anyio
import dns.asyncquery
import dns.exception
import dns.message
import dns.name
import dns.rcode
import dns.rdatatype

from app.monitoring.websites.models import DnsRecordType

#: EDNS buffer size to advertise: large enough for most answers over UDP, small
#: enough not to fragment (the 2020 DNS flag day value). Bigger answers are
#: truncated and fetched again over TCP.
EDNS_PAYLOAD = 1232

#: Answers that say, with authority, that there is nothing to return. Unlike a
#: timeout or SERVFAIL, one of these from any resolver contradicts pinned values.
DEFINITIVE_ERRORS = frozenset({"nxdomain", "no_records"})

#: Most telling first: when resolvers fail in different ways, the check's
#: error_type is the most common failure, ties going to the earlier here.
_ERROR_ORDER = (
    "nxdomain",
    "no_records",
    "servfail",
    "refused",
    "timeout",
    "network_error",
    "dns_error",
)


@dataclass(frozen=True, slots=True)
class Resolver:
    """A DNS resolver to ask, e.g. `Resolver("Cloudflare", "1.1.1.1")`."""

    name: str
    address: str


def configured_resolvers(pairs: Sequence[tuple[str, str]]) -> list[Resolver]:
    return [Resolver(name, address) for name, address in pairs]


# ---------------------------------------------------------------------------
# One record as text. `schemas.dns_value` writes expected values the same way,
# so the two compare as strings.
# ---------------------------------------------------------------------------


def _name_text(name: dns.name.Name) -> str:
    """`mx1.example.com`: lower case, no final dot; the root stays `.`."""
    return name.to_text(omit_final_dot=True).lower()


def record_text(record_type: DnsRecordType, rdata) -> str:
    """How one rdata from an answer is stored and shown."""
    match record_type:
        case DnsRecordType.A | DnsRecordType.AAAA:
            return ipaddress.ip_address(rdata.address).compressed
        case DnsRecordType.CNAME:
            return _name_text(rdata.target)
        case DnsRecordType.MX:
            return f"{rdata.preference} {_name_text(rdata.exchange)}"
        case DnsRecordType.TXT:
            # A long value is split into strings of up to 255 bytes; they are
            # one value, as SPF and DKIM read them.
            return b"".join(rdata.strings).decode("utf-8", errors="replace")
    raise ValueError(f"Unsupported record type {record_type}")


def format_records(
    record_type: DnsRecordType | str, records: Sequence[str], limit: int = 3
) -> str:
    """`203.0.113.10, 203.0.113.11 (+2 more)`; TXT values quoted and cut
    short, since they may hold commas and run to hundreds of characters."""
    if not records:
        return "none"
    if DnsRecordType(record_type) is DnsRecordType.TXT:
        shown = [f'"{r[:60]}…"' if len(r) > 60 else f'"{r}"' for r in records[:limit]]
    else:
        shown = list(records[:limit])
    text = ", ".join(shown)
    if len(records) > limit:
        text += f" (+{len(records) - limit} more)"
    return text


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResolverAnswer:
    """What one resolver said about the record."""

    resolver: str
    address: str
    #: The records, sorted; None when the resolver returned none, and `error`
    #: then says why.
    records: list[str] | None = None
    #: Seconds the resolver may keep serving these from its cache.
    ttl: int | None = None
    #: How long the answer took; None when none came.
    time_ms: int | None = None
    error: str | None = None
    #: `nxdomain`, `no_records`, `servfail`, `refused`, `timeout`,
    #: `network_error` or `dns_error`.
    error_type: str | None = None

    @property
    def label(self) -> str:
        """`Cloudflare (1.1.1.1)`, or just the address when it is the name."""
        if self.resolver == self.address:
            return self.address
        return f"{self.resolver} ({self.address})"

    def as_dict(self) -> dict:
        return {
            "resolver": self.resolver,
            "address": self.address,
            "records": self.records,
            "ttl": self.ttl,
            "time_ms": self.time_ms,
            "error": self.error,
            "error_type": self.error_type,
        }


@dataclass(slots=True)
class DnsResult:
    """One check's answers, and the verdict on them.

    `error` and `error_type` are set when the check failed: the record did not
    resolve at most resolvers, or a resolver contradicted the pinned values.
    """

    name: str
    record_type: DnsRecordType
    answers: list[ResolverAnswer]
    #: The pinned values, normalized and sorted; empty when none are.
    expected: list[str] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None

    @property
    def is_up(self) -> bool:
        return self.error_type is None

    @property
    def answered(self) -> list[ResolverAnswer]:
        """The resolvers that returned the record."""
        return [a for a in self.answers if a.records is not None]

    @property
    def consistent(self) -> bool:
        """Every resolver that returned the record returned the same set, and
        none said, with authority, that there is none. Timeouts and failures
        do not count against it."""
        sets = {tuple(a.records) for a in self.answered}
        denied = any(a.error_type in DEFINITIVE_ERRORS for a in self.answers)
        return len(sets) <= 1 and not (sets and denied)

    @property
    def records(self) -> list[str] | None:
        """The records the resolvers agree on: returned consistently by at
        least half of them. None when they disagree, or too few answered to
        trust the answer."""
        answered = self.answered
        if not answered or not self.consistent or 2 * len(answered) < len(self.answers):
            return None
        return list(answered[0].records)

    @property
    def response_time_ms(self) -> int | None:
        """The average time the resolvers took to answer, whatever they said."""
        times = [a.time_ms for a in self.answers if a.time_ms is not None]
        return round(sum(times) / len(times)) if times else None

    @property
    def summary(self) -> str:
        """One line, e.g. `A 203.0.113.10` or `A: resolvers disagree (2 answers)`."""
        if not self.is_up:
            return self.error or "DNS lookup failed"
        kind = self.record_type.value
        records = self.records
        if records is None:
            # A resolver saying there is no such record is an answer too.
            said = {tuple(a.records) for a in self.answered} | {
                a.error_type for a in self.answers if a.error_type in DEFINITIVE_ERRORS
            }
            return f"{kind}: resolvers disagree ({len(said)} different answers)"
        text = f"{kind} {format_records(self.record_type, records)}"
        if len(self.answered) < len(self.answers):
            text += f" at {_of_resolvers(len(self.answered), len(self.answers))}"
        return text

    def as_dict(self) -> dict:
        """What a check row keeps, and the API returns."""
        return {
            "record_type": self.record_type.value,
            "expected": self.expected,
            "records": self.records,
            "consistent": self.consistent,
            "answers": [a.as_dict() for a in self.answers],
        }


def _of_resolvers(count: int, total: int) -> str:
    return f"{count} of {total} resolver{'' if total == 1 else 's'}"


def _failure(answer: ResolverAnswer, rcode: int) -> ResolverAnswer:
    match rcode:
        case dns.rcode.NXDOMAIN:
            answer.error, answer.error_type = "does not exist (NXDOMAIN)", "nxdomain"
        case dns.rcode.SERVFAIL:
            answer.error, answer.error_type = "server failure (SERVFAIL)", "servfail"
        case dns.rcode.REFUSED:
            answer.error, answer.error_type = "refused the query (REFUSED)", "refused"
        case _:
            answer.error = f"answered {dns.rcode.to_text(rcode)}"
            answer.error_type = "dns_error"
    return answer


async def _ask(
    resolver: Resolver, name: str, record_type: DnsRecordType, timeout: float
) -> ResolverAnswer:
    """Ask one resolver, recursion desired, over UDP and then TCP if the
    answer does not fit. Every outcome comes back as an answer."""
    answer = ResolverAnswer(resolver=resolver.name, address=resolver.address)
    query = dns.message.make_query(
        name, dns.rdatatype.from_text(record_type.value), use_edns=0, payload=EDNS_PAYLOAD
    )
    started = time.perf_counter()
    try:
        # The query's own timeout is per attempt; this caps UDP plus TCP.
        with anyio.fail_after(timeout):
            response, _ = await dns.asyncquery.udp_with_fallback(
                query, resolver.address, timeout=timeout
            )
    except (TimeoutError, dns.exception.Timeout):
        answer.error, answer.error_type = f"timed out after {timeout:g}s", "timeout"
        return answer
    except OSError as exc:  # e.g. no route, or no IPv6 on this server
        answer.error, answer.error_type = f"network error: {exc}", "network_error"
        return answer
    except dns.exception.DNSException as exc:  # a malformed or mismatched reply
        answer.error, answer.error_type = f"bad response: {exc}", "dns_error"
        return answer
    answer.time_ms = round((time.perf_counter() - started) * 1000)

    rcode = response.rcode()
    if rcode != dns.rcode.NOERROR:
        return _failure(answer, rcode)
    try:
        # Follows a CNAME chain to the type asked for, as a stub resolver does.
        chain = response.resolve_chaining()
    except dns.exception.DNSException as exc:  # e.g. a chain that never ends
        answer.error, answer.error_type = f"bad response: {exc}", "dns_error"
        return answer
    if chain.answer is None:
        answer.error = f"has no {record_type.value} record"
        answer.error_type = "no_records"
        return answer
    answer.records = sorted({record_text(record_type, rdata) for rdata in chain.answer})
    answer.ttl = chain.answer.ttl
    return answer


def _grouped(answers: Sequence[ResolverAnswer], record_type: DnsRecordType) -> str:
    """`Cloudflare, Google returned 198.51.100.7; Quad9: does not exist
    (NXDOMAIN)`: resolvers that said the same thing, together."""
    said: dict[str, list[str]] = {}
    for a in answers:
        text = (
            f"returned {format_records(record_type, a.records)}"
            if a.records is not None
            else a.error or "no answer"
        )
        said.setdefault(text, []).append(a.resolver)
    return "; ".join(
        f"{', '.join(names)} {text}" if text.startswith("returned") else f"{', '.join(names)}: {text}"
        for text, names in said.items()
    )


def judge(result: DnsResult) -> DnsResult:
    """Set `error` and `error_type` on a failed check; see the module doc."""
    answers, answered = result.answers, result.answered
    kind = result.record_type.value
    if 2 * len(answered) < len(answers):
        failed = [a for a in answers if a.records is None]
        counts = Counter(a.error_type for a in failed)
        result.error_type = min(
            counts, key=lambda t: (-counts[t], _ERROR_ORDER.index(t) if t in _ERROR_ORDER else 99)
        )
        result.error = (
            f"{kind} record did not resolve at {_of_resolvers(len(failed), len(answers))} — "
            f"{_grouped(failed, result.record_type)}."
        )
        return result
    if result.expected:
        wrong = [
            a
            for a in answers
            if (a.records is not None and a.records != result.expected)
            or a.error_type in DEFINITIVE_ERRORS
        ]
        if wrong:
            result.error_type = "dns_mismatch"
            result.error = (
                f"Expected {kind} {format_records(result.record_type, result.expected)}; "
                f"{_grouped(wrong, result.record_type)}."
            )
    return result


async def lookup(
    name: str,
    record_type: DnsRecordType,
    *,
    resolvers: Sequence[Resolver],
    timeout: float,
    expected: Sequence[str] = (),
) -> DnsResult:
    """Ask every resolver for `record_type` of `name` at once, and judge the
    answers against each other and `expected` (already normalized)."""
    answers = await asyncio.gather(
        *(_ask(resolver, name, record_type, timeout) for resolver in resolvers)
    )
    return judge(
        DnsResult(
            name=name,
            record_type=record_type,
            answers=list(answers),
            expected=sorted(set(expected)),
        )
    )
