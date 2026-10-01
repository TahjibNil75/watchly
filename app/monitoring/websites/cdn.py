"""Whether a site is served through a CDN, and which one.

A CDN leaves two kinds of trace, and both are read here:

* the response headers it adds (`cf-ray`, `x-amz-cf-id`, `x-vercel-id`, ...),
  and its name in `server`, `via` or `x-cache`;
* the DNS name the site's host is an alias (CNAME) of, like
  `d111.cloudfront.net`.

Neither is proof of a *working* setup, and a CDN that strips its headers and
sits behind an A record (some Cloudflare and Akamai setups) can still go
unseen. So the result says what was found and why, and "none detected" means
only that.

Read from the same successful response as the check itself, every
CDN_CHECK_INTERVAL_SECONDS; see `detect`.
"""

import ipaddress
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import dns.asyncresolver
import dns.exception
import dns.rdatatype

logger = logging.getLogger(__name__)

#: Longest header value quoted as evidence.
MAX_EVIDENCE_VALUE = 80

#: Seconds the CNAME lookup may take in all.
CNAME_LOOKUP_TIMEOUT = 3.0


@dataclass(frozen=True, slots=True)
class Signature:
    """How one provider shows itself."""

    name: str
    #: Headers whose mere presence names the provider.
    headers: tuple[str, ...] = ()
    #: `(header, text)`: the header's value contains the text, any case.
    contains: tuple[tuple[str, str], ...] = ()
    #: CNAME targets ending in one of these.
    cnames: tuple[str, ...] = ()


SIGNATURES = (
    Signature(
        "Cloudflare",
        headers=("cf-ray", "cf-cache-status"),
        contains=(("server", "cloudflare"),),
        cnames=(".cdn.cloudflare.net", ".cloudflare.net"),
    ),
    Signature(
        "Amazon CloudFront",
        headers=("x-amz-cf-id", "x-amz-cf-pop"),
        contains=(("via", "cloudfront"), ("x-cache", "cloudfront")),
        cnames=(".cloudfront.net",),
    ),
    Signature(
        "Fastly",
        headers=("x-fastly-request-id", "fastly-debug-digest"),
        contains=(("x-served-by", "cache-"),),
        cnames=(".fastly.net", ".fastlylb.net", ".global.ssl.fastly.net"),
    ),
    Signature(
        "Akamai",
        headers=("x-akamai-transformed", "x-akamai-request-id", "akamai-grn"),
        contains=(("server", "akamai"),),
        cnames=(
            ".akamaiedge.net",
            ".akamai.net",
            ".edgekey.net",
            ".edgesuite.net",
            ".akamaized.net",
        ),
    ),
    Signature(
        "Azure Front Door / CDN",
        headers=("x-azure-ref", "x-msedge-ref"),
        cnames=(".azurefd.net", ".azureedge.net", ".z01.azurefd.net"),
    ),
    Signature(
        "Google Cloud CDN / load balancer",
        contains=(("via", "1.1 google"),),
        cnames=(".googlehosted.com", ".ghs.googlehosted.com"),
    ),
    Signature(
        "Vercel",
        headers=("x-vercel-id", "x-vercel-cache"),
        contains=(("server", "vercel"),),
        cnames=(".vercel-dns.com", ".vercel.app"),
    ),
    Signature(
        "Netlify",
        headers=("x-nf-request-id",),
        contains=(("server", "netlify"),),
        cnames=(".netlify.app", ".netlify.com"),
    ),
    Signature(
        "Bunny CDN",
        headers=("cdn-pullzone", "cdn-requestid"),
        contains=(("server", "bunnycdn"),),
        cnames=(".b-cdn.net",),
    ),
    Signature(
        "KeyCDN",
        contains=(("server", "keycdn"),),
        cnames=(".kxcdn.com",),
    ),
    Signature(
        "CDN77",
        headers=("x-77-pop",),
        contains=(("server", "cdn77"),),
        cnames=(".cdn77.org", ".cdn77-ssl.net"),
    ),
    Signature(
        "StackPath / Highwinds",
        headers=("x-hw",),
        cnames=(".stackpathdns.com", ".hwcdn.net"),
    ),
    Signature(
        "Imperva (Incapsula)",
        headers=("x-iinfo",),
        contains=(("x-cdn", "imperva"), ("x-cdn", "incapsula")),
        cnames=(".incapdns.net",),
    ),
    Signature(
        "Sucuri",
        headers=("x-sucuri-id", "x-sucuri-cache"),
        contains=(("server", "sucuri"),),
        cnames=(".sucuri.net",),
    ),
)

#: Headers that show a cache answered, whoever runs it, tried in this order.
CACHE_STATUS_HEADERS = (
    "cf-cache-status",
    "x-vercel-cache",
    "x-cache",
    "x-cache-status",
    "x-proxy-cache",
)

#: `cached` is True when the status holds one of the first, False for the
#: second, and None (unknown) for anything else.
_HIT_WORDS = ("hit",)
_MISS_WORDS = ("miss", "bypass", "dynamic", "expired", "revalidated", "uncacheable")


@dataclass(slots=True)
class CdnProvider:
    name: str
    #: What gave it away, e.g. `header cf-ray: 8a1b…` or `CNAME x.cloudfront.net`.
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CdnInfo:
    """What `detect` found, as the site's page and API show it."""

    #: The host whose response and DNS aliases were read.
    host: str
    providers: list[CdnProvider] = field(default_factory=list)
    #: The names the host is an alias of, in order; empty for an address record.
    cname_chain: list[str] = field(default_factory=list)
    #: A cache header's raw value, e.g. `HIT` or `Miss from cloudfront`.
    cache_status: str | None = None
    #: Whether that says a cache answered: True, False, or None when unknown.
    cached: bool | None = None
    #: Seconds the response sat in a cache, from the `Age` header.
    age_seconds: int | None = None
    #: A cache or proxy answered, but nothing named it: `via`, `x-cache` or
    #: `age` was sent with no provider recognised.
    unidentified_cache: bool = False

    @property
    def detected(self) -> bool:
        return bool(self.providers) or self.unidentified_cache

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "providers": [{"name": p.name, "evidence": p.evidence} for p in self.providers],
            "cname_chain": self.cname_chain,
            "cache_status": self.cache_status,
            "cached": self.cached,
            "age_seconds": self.age_seconds,
            "unidentified_cache": self.unidentified_cache,
        }


def _clip(value: str) -> str:
    return value if len(value) <= MAX_EVIDENCE_VALUE else value[:MAX_EVIDENCE_VALUE] + "…"


def _cached(status: str | None) -> bool | None:
    if not status:
        return None
    lowered = status.lower()
    if any(word in lowered for word in _HIT_WORDS) and "miss" not in lowered:
        return True
    if any(word in lowered for word in _MISS_WORDS):
        return False
    return None


def _is_address(host: str) -> bool:
    """An IP address has no DNS aliases to look up."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return not host
    return True


async def lookup_cname_chain(host: str) -> list[str]:
    """The names `host` is an alias of, in order, from the system resolver.
    Empty when it has none, or the lookup fails: this is a hint, not a check."""
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = CNAME_LOOKUP_TIMEOUT
    try:
        answer = await resolver.resolve(host, "A", raise_on_no_answer=False)
    except (dns.exception.DNSException, OSError) as exc:
        logger.info("CNAME lookup failed for %s: %s", host, exc)
        return []
    chain: list[str] = []
    for rrset in answer.response.answer:
        if rrset.rdtype == dns.rdatatype.CNAME:
            chain.extend(str(rdata.target).rstrip(".").lower() for rdata in rrset)
    return chain


def read_headers(host: str, headers, cname_chain: list[str]) -> CdnInfo:
    """What the response headers and CNAME chain say. `headers` is a
    case-insensitive mapping, like httpx's."""
    info = CdnInfo(host=host, cname_chain=cname_chain)

    for signature in SIGNATURES:
        evidence: list[str] = []
        for name in signature.headers:
            if (value := headers.get(name)) is not None:
                evidence.append(f"header {name}: {_clip(value)}" if value else f"header {name}")
        for name, text in signature.contains:
            value = headers.get(name)
            if value and text in value.lower():
                evidence.append(f"header {name}: {_clip(value)}")
        for target in cname_chain:
            if any(target.endswith(suffix) for suffix in signature.cnames):
                evidence.append(f"CNAME {target}")
        if evidence:
            info.providers.append(CdnProvider(signature.name, evidence))

    for name in CACHE_STATUS_HEADERS:
        if (value := headers.get(name)):
            info.cache_status = _clip(value)
            info.cached = _cached(value)
            break
    age = headers.get("age")
    if age and age.strip().isdigit():
        info.age_seconds = int(age)

    # Something cached or proxied the answer even though no provider matched.
    info.unidentified_cache = not info.providers and (
        info.cache_status is not None or info.age_seconds is not None or "via" in headers
    )
    return info


async def detect(final_url: str, headers) -> CdnInfo:
    """Read a successful response for CDN traces. Never raises."""
    host = urlsplit(final_url).hostname or ""
    chain = [] if _is_address(host) else await lookup_cname_chain(host)
    return read_headers(host, headers, chain)
