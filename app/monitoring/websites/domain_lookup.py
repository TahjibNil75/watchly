"""Domain registration lookups: when a site's domain expires, with whom it
is registered, and which nameservers it is delegated to, from the registry
itself.

RDAP first: WHOIS's successor, the same registration data as JSON over HTTPS,
which every gTLD registry and many ccTLD ones serve. IANA's bootstrap file
(RDAP_BOOTSTRAP_URL) says which server answers for each top-level domain; it
is fetched once a day and kept in memory. A top-level domain it does not list
(`.io`, `.de`, …) is looked up over plain WHOIS instead, at the server IANA's
own WHOIS names for it, and the free-text answer searched for the lines most
registries use.

Which part of a host name is the registered domain is not knowable from the
name alone (`example.co.uk`, but `example.com`), so a lookup asks the registry
about the shortest candidate first, then longer ones: for `www.example.com`,
`example.com`; for `www.example.co.uk`, `co.uk` (not registered) and then
`example.co.uk`. The first with an expiry date wins, so a typical host takes
one request.

A registry that cannot be reached, has neither service, or does not publish
an expiry date (`.de`) is an answer like any other: this module never raises
for it.
"""

import asyncio
import contextlib
import ipaddress
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Per request. Registries are slower than the sites Watchly checks, and a
#: lookup runs once a day, so this is not the site's own timeout.
TIMEOUT_SECONDS = 10.0

#: How long the bootstrap file is trusted before it is fetched again.
BOOTSTRAP_TTL_SECONDS = 86_400

_HEADERS = {
    "Accept": "application/rdap+json, application/json",
    "User-Agent": "Watchly-Monitor/1.0 (+domain-expiry)",
}


@dataclass(slots=True)
class DomainInfo:
    """What a lookup learned. `error` says why something is missing: the
    registry may know the domain (`name` set) and still give no date."""

    #: The registered domain, e.g. `example.co.uk`.
    name: str | None = None
    expires_at: datetime | None = None
    registrar: str | None = None
    #: The nameservers the registry delegates the domain to, lower case and
    #: sorted; empty when it did not say.
    nameservers: list[str] = field(default_factory=list)
    error: str | None = None


def _nameservers(names) -> list[str]:
    """Host names as `DomainInfo.nameservers` keeps them."""
    return sorted({str(n).strip().rstrip(".").lower() for n in names if n and str(n).strip()})


def lookup_host(host: str | None) -> str | None:
    """The name to look up for `host`, in lower case and ASCII (punycode);
    None when there is none: an IP address, or a name without a dot, like
    `localhost`."""
    if not host:
        return None
    host = host.strip().rstrip(".").lower()
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host.strip("[]"))
        return None
    if "." not in host:
        return None
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


class _Bootstrap:
    """IANA's map of top-level domain -> RDAP base URL, fetched lazily and
    kept for BOOTSTRAP_TTL_SECONDS. A failed refresh keeps the old map."""

    def __init__(self) -> None:
        self._services: dict[str, str] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def base_url(self, tld: str, client: httpx.AsyncClient) -> str | None:
        if self._stale():
            async with self._lock:
                # Another lookup may have refreshed it while this one waited.
                if self._stale():
                    await self._refresh(client)
        return (self._services or {}).get(tld)

    def _stale(self) -> bool:
        return (
            self._services is None
            or time.monotonic() - self._fetched_at > BOOTSTRAP_TTL_SECONDS
        )

    async def _refresh(self, client: httpx.AsyncClient) -> None:
        try:
            response = await client.get(
                settings.RDAP_BOOTSTRAP_URL, headers=_HEADERS, timeout=TIMEOUT_SECONDS
            )
            response.raise_for_status()
            services: dict[str, str] = {}
            for tlds, urls in response.json()["services"]:
                # Prefer HTTPS; a few registries list plain HTTP as well.
                base = next((u for u in urls if u.startswith("https://")), urls[0])
                for tld in tlds:
                    services[tld.lower()] = base if base.endswith("/") else f"{base}/"
        except Exception:
            if self._services is None:
                raise
            logger.warning("RDAP bootstrap refresh failed; keeping the old one.", exc_info=True)
            return
        self._services = services
        self._fetched_at = time.monotonic()

    def clear(self) -> None:
        """Forget the map, so the next lookup fetches it again. For tests."""
        self._services = None
        self._fetched_at = 0.0


bootstrap = _Bootstrap()


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _registrar(data: dict) -> str | None:
    """The registrar's name, from the vCard of the entity in that role."""
    for entity in data.get("entities") or []:
        if "registrar" not in (entity.get("roles") or []):
            continue
        vcard = entity.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) == 2:
            for prop in vcard[1]:
                if isinstance(prop, list) and len(prop) >= 4 and prop[0] == "fn" and prop[3]:
                    return str(prop[3]).strip()[:255]
    return None


def parse_domain(data: dict, queried: str) -> DomainInfo:
    """A registry's RDAP domain object as a `DomainInfo`."""
    expires_at = next(
        (
            date
            for event in data.get("events") or []
            if event.get("eventAction") == "expiration"
            and (date := _parse_date(event.get("eventDate"))) is not None
        ),
        None,
    )
    name = str(data.get("ldhName") or queried).rstrip(".").lower()
    return DomainInfo(
        name=name,
        expires_at=expires_at,
        registrar=_registrar(data),
        nameservers=_nameservers(
            ns.get("ldhName") for ns in data.get("nameservers") or [] if isinstance(ns, dict)
        ),
    )


# --- WHOIS, for registries without RDAP -----------------------------------

#: Where to ask which WHOIS server serves a top-level domain.
IANA_WHOIS = "whois.iana.org"
WHOIS_PORT = 43
#: A WHOIS answer is a page of text; anything longer is not one.
_WHOIS_MAX_BYTES = 65_536

#: Lines that give the expiry date, as the common registry software writes them.
_WHOIS_EXPIRY = re.compile(
    r"^\s*(?:registry expiry date|registrar registration expiration date|"
    r"expir(?:y|ation) date|expiration time|expires(?: on)?|paid-till|renewal date)"
    r"\s*:\s*(\S.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
#: One nameserver per line; some servers put its address after the name.
_WHOIS_NAMESERVER = re.compile(
    r"^\s*(?:name server|nameserver|nserver)\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE
)
_WHOIS_REGISTRAR = re.compile(
    r"^\s*(?:registrar|registrar name|sponsoring registrar)\s*:\s*(\S.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
#: How registries say a name is not registered.
_WHOIS_UNREGISTERED = re.compile(
    r"^\s*(?:no match|not found|no entries found|no data found|domain not found|"
    r"status:\s*(?:free|available)|the queried object does not exist)",
    re.IGNORECASE | re.MULTILINE,
)
#: Date layouts seen besides ISO 8601, which is tried first.
_WHOIS_DATE_FORMATS = ("%d-%b-%Y", "%Y.%m.%d", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%Y")

#: Top-level domain -> its WHOIS server, or None for one without; never expires,
#: as these move far less often than a process lives.
_whois_servers: dict[str, str | None] = {}


class _WhoisUnregistered(Exception):
    """The server says the name is not registered."""


async def _whois(server: str, query: str) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(server, WHOIS_PORT), TIMEOUT_SECONDS
    )
    try:
        writer.write(f"{query}\r\n".encode("ascii"))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(_WHOIS_MAX_BYTES), TIMEOUT_SECONDS)
        # Servers answer in chunks and then close; read the rest.
        while chunk := await asyncio.wait_for(reader.read(_WHOIS_MAX_BYTES), TIMEOUT_SECONDS):
            data += chunk
            if len(data) >= _WHOIS_MAX_BYTES:
                break
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return data.decode("utf-8", errors="replace")


async def _whois_server(tld: str) -> str | None:
    if tld not in _whois_servers:
        answer = await _whois(IANA_WHOIS, tld)
        match = re.search(r"^whois:\s*(\S+)", answer, re.IGNORECASE | re.MULTILINE)
        _whois_servers[tld] = match.group(1).lower() if match else None
    return _whois_servers[tld]


def _parse_whois_date(value: str) -> datetime | None:
    if parsed := _parse_date(value):
        return parsed
    token = value.split()[0]
    for layout in _WHOIS_DATE_FORMATS:
        with contextlib.suppress(ValueError):
            return datetime.strptime(token, layout).replace(tzinfo=UTC)
    # e.g. `2027-03-08 (YYYY-MM-DD)`.
    return _parse_date(token)


def parse_whois(text: str, queried: str) -> DomainInfo:
    """A WHOIS answer as a `DomainInfo`. Raises `_WhoisUnregistered` when it
    says the name is not registered."""
    if _WHOIS_UNREGISTERED.search(text):
        raise _WhoisUnregistered(queried)
    expires_at = next(
        (
            date
            for match in _WHOIS_EXPIRY.finditer(text)
            if (date := _parse_whois_date(match.group(1))) is not None
        ),
        None,
    )
    registrar = _WHOIS_REGISTRAR.search(text)
    return DomainInfo(
        name=queried,
        expires_at=expires_at,
        registrar=registrar.group(1)[:255] if registrar else None,
        nameservers=_nameservers(_WHOIS_NAMESERVER.findall(text)),
    )


async def _whois_candidate(server: str, name: str) -> DomainInfo | None:
    """None when `name` is not registered."""
    try:
        return parse_whois(await _whois(server, name), name)
    except _WhoisUnregistered:
        return None


# --- lookups --------------------------------------------------------------


def _explain(exc: Exception) -> str:
    match exc:
        case httpx.HTTPStatusError():
            return f"The registry's RDAP server answered HTTP {exc.response.status_code}."
        case httpx.TimeoutException() | TimeoutError():
            return "The registry did not answer in time."
        case httpx.TransportError() | OSError():
            return f"Could not reach the registry: {exc or type(exc).__name__}"
        case _:
            return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def _rdap_candidate(base: str, client: httpx.AsyncClient, name: str) -> DomainInfo | None:
    """None when `name` is not registered."""
    response = await client.get(
        f"{base}domain/{name}", headers=_HEADERS, timeout=TIMEOUT_SECONDS
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return parse_domain(response.json(), name)


async def _lookup(host: str, client: httpx.AsyncClient) -> DomainInfo:
    labels = host.split(".")
    tld = labels[-1]
    # Each asks about one candidate name; None when it is not registered.
    ask: Callable[[str], Awaitable[DomainInfo | None]]
    if base := await bootstrap.base_url(tld, client):
        ask = partial(_rdap_candidate, base, client)
    elif server := await _whois_server(tld):
        ask = partial(_whois_candidate, server)
    else:
        return DomainInfo(
            error=f"The .{tld} registry publishes neither RDAP nor WHOIS data, so when "
            "the domain expires cannot be looked up."
        )

    known: DomainInfo | None = None
    for size in range(2, len(labels) + 1):
        info = await ask(".".join(labels[-size:]))
        if info is None:
            continue
        if info.expires_at is not None:
            return info
        # Known, but no date: a longer name will not be registered either,
        # save in a registry that lists public suffixes (`co.uk`) as domains.
        known = known or info
    if known is not None:
        known.error = f"The .{tld} registry does not publish when {known.name} expires."
        return known
    return DomainInfo(error=f"The registry has no record of {host} or a domain above it.")


async def lookup_domain(host: str, client: httpx.AsyncClient | None = None) -> DomainInfo:
    """Look up the registered domain `host` belongs to. `host` is as
    `lookup_host` returns it. Never raises."""
    try:
        if client is not None:
            return await _lookup(host, client)
        async with httpx.AsyncClient(follow_redirects=True) as own:
            return await _lookup(host, own)
    except Exception as exc:  # noqa: BLE001 - a failed lookup is data, not a crash
        logger.info("Domain lookup failed for %s: %s", host, exc)
        return DomainInfo(error=_explain(exc))
