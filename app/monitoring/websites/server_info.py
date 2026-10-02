"""Where a site is served from: the address its host answered on, who runs
that network, and what the address is called.

* The address comes from the check's own connection (see `checker`).
* The network owner comes from Team Cymru's IP-to-ASN mapping, which is
  answered over plain DNS: no key, no download, and nothing leaves Watchly
  but a TXT query that carries the server's address.
* The name comes from the address's reverse (PTR) record.

The country is the one the address block was *registered* in, not where the
machine stands: an anycast or CDN address can be answered from anywhere.

Read from a successful response whenever the address is one not seen before,
and otherwise every SERVER_CHECK_INTERVAL_SECONDS; see `lookup`. A hint for the
site's page, never a check: every failure here is swallowed.
"""

import ipaddress
import logging
from dataclasses import dataclass, field

import dns.asyncresolver
import dns.exception
import dns.resolver
import dns.reversename

logger = logging.getLogger(__name__)

#: Seconds each lookup may take.
LOOKUP_TIMEOUT = 3.0

#: Distinct addresses remembered per site. Round-robin DNS and CDNs hand out
#: several, so a new one is not a change.
MAX_ADDRESSES_SEEN = 8

CYMRU_ORIGIN = {4: "origin.asn.cymru.com", 6: "origin6.asn.cymru.com"}
CYMRU_ASN = "asn.cymru.com"


@dataclass(slots=True)
class ServerInfo:
    """What `lookup` found, as the site's page and API show it."""

    #: The address the host answered on.
    ip: str
    #: 4 or 6.
    version: int
    #: Its reverse DNS name, if it has one.
    ptr: str | None = None
    #: Autonomous system number and name, e.g. 13335 and `CLOUDFLARENET, US`.
    asn: int | None = None
    as_name: str | None = None
    #: The announced network the address sits in, e.g. `104.16.0.0/12`.
    prefix: str | None = None
    #: Two-letter country the block is registered in, and the registry (`arin`).
    country: str | None = None
    registry: str | None = None
    #: The server advertised HTTP/3 (`alt-svc: h3=…`) on this response.
    h3: bool = False
    #: Recently seen addresses, newest first, this one included.
    ips_seen: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ip": self.ip,
            "version": self.version,
            "ptr": self.ptr,
            "asn": self.asn,
            "as_name": self.as_name,
            "prefix": self.prefix,
            "country": self.country,
            "registry": self.registry,
            "h3": self.h3,
            "ips_seen": self.ips_seen,
        }


def advertises_h3(alt_svc: str | None) -> bool:
    """Whether an `Alt-Svc` header offers HTTP/3 (`h3=":443"`, `h3-29=…`)."""
    if not alt_svc:
        return False
    return any(
        entry.strip().lower().startswith(("h3=", "h3-"))
        for entry in alt_svc.split(",")
    )


def _txt(answer) -> list[str]:
    """The strings of a TXT answer, one per record."""
    return [b"".join(rdata.strings).decode("ascii", "replace") for rdata in answer]


def _fields(record: str) -> list[str]:
    """`13335 | 104.16.0.0/12 | US | arin | 2010-07-14` as its trimmed parts."""
    return [part.strip() for part in record.split("|")]


def _cymru_name(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """`4.3.2.1.origin.asn.cymru.com`; for IPv6 the nibbles, reversed."""
    arpa = dns.reversename.from_address(str(address)).to_text().rstrip(".")
    suffix = "in-addr.arpa" if address.version == 4 else "ip6.arpa"
    return f"{arpa.removesuffix(suffix).rstrip('.')}.{CYMRU_ORIGIN[address.version]}"


def _resolver() -> dns.asyncresolver.Resolver:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = LOOKUP_TIMEOUT
    return resolver


async def _ptr(address) -> str | None:
    try:
        answer = await _resolver().resolve(
            dns.reversename.from_address(str(address)), "PTR"
        )
    except (dns.exception.DNSException, OSError):
        return None
    return str(next(iter(answer)).target).rstrip(".").lower() or None


async def _network(info: ServerInfo, address) -> None:
    """Fill in the ASN, prefix, country and registry from Team Cymru."""
    resolver = _resolver()
    try:
        origin = _txt(await resolver.resolve(_cymru_name(address), "TXT"))
    except (dns.exception.DNSException, OSError) as exc:
        logger.info("ASN lookup failed for %s: %s", address, exc)
        return
    if not origin:
        return
    # An address announced by several networks lists them all in the first
    # field; the first is the one shown.
    parts = _fields(origin[0])
    if len(parts) < 4 or not parts[0].split()[0].isdigit():
        return
    info.asn = int(parts[0].split()[0])
    info.prefix = parts[1] or None
    info.country = (parts[2] or "").upper() or None
    info.registry = parts[3] or None
    try:
        names = _txt(await resolver.resolve(f"AS{info.asn}.{CYMRU_ASN}", "TXT"))
    except (dns.exception.DNSException, OSError):
        return
    if names and len(parts := _fields(names[0])) >= 5:
        info.as_name = parts[4] or None


async def lookup(ip: str, alt_svc: str | None, seen: list[str] | None = None) -> ServerInfo:
    """Look `ip` up. Never raises: what could not be found stays None.

    A private, loopback or otherwise non-global address belongs to no public
    network and is not sent to anyone.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        address = None
    if address is None:
        return ServerInfo(ip=ip, version=4, h3=advertises_h3(alt_svc), ips_seen=[ip])
    info = ServerInfo(
        ip=str(address),
        version=address.version,
        h3=advertises_h3(alt_svc),
        ips_seen=[str(address), *(a for a in (seen or []) if a != str(address))][
            :MAX_ADDRESSES_SEEN
        ],
    )
    if not address.is_global:
        return info
    info.ptr = await _ptr(address)
    await _network(info, address)
    return info
