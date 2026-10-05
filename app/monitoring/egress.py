"""Which addresses a probe may connect to.

Every probe resolves its target, then each resolved address is judged here
before anything connects to it, and only the ones that pass are connected
to. Connecting to the address that was judged, rather than resolving again,
is what keeps DNS rebinding from swapping it afterwards.

Two kinds of probe, two scopes:

- website checks (`websites/checker.py`, `websites/pinger.py`) reach public
  addresses only, unless WEBSITE_PRIVATE_TARGETS=allow;
- infrastructure probes (`infra/aws/probes`) reach the CIDRs of their
  resource's VPC, and public addresses only when the check goes over the
  internet (an internet-facing load balancer, a server's public IP).

Whatever the scope, loopback, link-local (the instance metadata service),
multicast, this server's own addresses and EGRESS_DENY_CIDRS are refused.
Without that, a check of `http://169.254.169.254/` would read the instance
role's credentials, and one of `10.20.0.25:5432` Watchly's own database.

DNS resolvers are not judged: they come from configuration, never a check.
"""

import ipaddress
import logging
import socket
from collections.abc import Iterable
from dataclasses import dataclass

from app.core.config import settings
from app.core.instance_metadata import read_instance_metadata

logger = logging.getLogger(__name__)

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Refused to every probe, with why, as GET /monitoring/websites/blocked and
#: the error of a refused check name them.
ALWAYS_BLOCKED: tuple[tuple[IPNetwork, str], ...] = tuple(
    (ipaddress.ip_network(cidr), why)
    for cidr, why in (
        ("0.0.0.0/8", "“this network”"),
        ("127.0.0.0/8", "loopback"),
        ("169.254.0.0/16", "link-local: instance metadata, ECS and Amazon DNS"),
        ("224.0.0.0/4", "multicast"),
        ("240.0.0.0/4", "reserved"),
        ("::/128", "unspecified"),
        ("::1/128", "loopback"),
        ("fe80::/10", "link-local"),
        ("fd00:ec2::/32", "AWS instance metadata and DNS over IPv6"),
        ("ff00::/8", "multicast"),
    )
)

#: Private address space: what only an infrastructure probe, inside its VPC,
#: may reach.
PRIVATE: tuple[IPNetwork, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
)

#: This server's own addresses, filled in at startup by `load_own_addresses`.
_own: set[IPAddress] = set()


class BlockedAddressError(OSError):
    """No resolved address of a target may be connected to. An OSError, so
    the code that maps connect failures sees it as one; the message says why."""

    def __init__(self, reason: str, address: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        #: The first address refused.
        self.address = address


@dataclass(frozen=True, slots=True)
class Scope:
    """Where one probe may connect, on top of what is always refused."""

    #: Private ranges this probe may reach: its VPC's CIDRs.
    private_cidrs: tuple[IPNetwork, ...] = ()
    allow_public: bool = True
    #: Any private address at all: website checks with
    #: WEBSITE_PRIVATE_TARGETS=allow.
    allow_any_private: bool = False
    #: Names the allowed space in a refusal, e.g. `prod-vpc (10.20.0.0/16)`.
    label: str = "public addresses"


def website_scope() -> Scope:
    """Website checks: public addresses, plus private ones when allowed."""
    return Scope(allow_any_private=settings.WEBSITE_PRIVATE_TARGETS == "allow")


def vpc_scope(name: str, cidrs: Iterable[str], *, allow_public: bool = False) -> Scope:
    """An infrastructure probe: the CIDRs of its VPC, plus public addresses
    for a check that goes over the internet. Its target comes from AWS, never
    from a person, so a public address there is the resource's own."""
    networks = tuple(ipaddress.ip_network(str(cidr), strict=False) for cidr in cidrs)
    ranges = ", ".join(str(network) for network in networks)
    return Scope(private_cidrs=networks, allow_public=allow_public, label=f"{name} ({ranges})")


def _parse(address: str) -> IPAddress | None:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _deny_extra() -> list[IPNetwork]:
    return [ipaddress.ip_network(cidr.strip(), strict=False) for cidr in settings.EGRESS_DENY_CIDRS]


def is_private(address: str) -> bool:
    ip = _parse(address)
    return ip is not None and any(ip in network for network in PRIVATE)


def refusal(address: str, scope: Scope) -> str | None:
    """Why `address` may not be connected to under `scope`, or None when it may."""
    ip = _parse(address)
    if ip is None:
        return f"{address} is not an IP address"
    if ip in _own:
        return f"{ip} is this server's own address"
    for network, why in ALWAYS_BLOCKED:
        if ip in network:
            return f"{ip} is in {network} ({why})"
    for network in _deny_extra():
        if ip in network:
            return f"{ip} is in {network} (EGRESS_DENY_CIDRS)"
    if any(ip in network for network in PRIVATE):
        if any(ip in network for network in scope.private_cidrs) or scope.allow_any_private:
            return None
        if scope.private_cidrs:
            return f"{ip} is not inside {scope.label}"
        return (
            f"{ip} is a private address, and website checks only reach public ones "
            "(WEBSITE_PRIVATE_TARGETS=block); watch private resources from Infrastructure"
        )
    if scope.allow_public:
        return None
    return f"{ip} is a public address, outside {scope.label}"


def vet(addresses: Iterable[str], scope: Scope) -> list[str]:
    """The addresses that may be connected to, in the order given.

    Raises `BlockedAddressError`, naming the first refusal, when none may.
    """
    allowed: list[str] = []
    refused: list[tuple[str, str]] = []
    for address in addresses:
        reason = refusal(address, scope)
        if reason is None:
            allowed.append(address)
        else:
            refused.append((address, reason))
    if not allowed:
        if not refused:
            raise BlockedAddressError("nothing to connect to")
        raise BlockedAddressError(refused[0][1], refused[0][0])
    return allowed


def own_addresses() -> list[str]:
    return sorted(str(ip) for ip in _own)


def register_own_addresses(addresses: Iterable[str]) -> None:
    for address in addresses:
        ip = _parse(address)
        if ip is not None:
            _own.add(ip)


def _interface_addresses() -> list[str]:
    """The IPv4 address of each of this host's (or container's) interfaces,
    where the platform lets them be read (Linux); empty elsewhere."""
    try:
        import fcntl
        import struct
    except ImportError:
        return []
    addresses = []
    for _, name in socket.if_nameindex():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                # SIOCGIFADDR
                packed = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
            except OSError:
                continue
            addresses.append(socket.inet_ntoa(packed[20:24]))
    return addresses


async def load_own_addresses() -> None:
    """Learn this server's addresses: its host name's, its interfaces', and
    on EC2 every address of every network interface, from the instance
    metadata. Called once at startup, in the background."""
    try:
        _, _, hostname_addresses = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        hostname_addresses = []
    register_own_addresses(hostname_addresses)
    try:
        register_own_addresses(_interface_addresses())
    except OSError:
        pass
    metadata = await read_instance_metadata()
    if metadata is not None:
        register_own_addresses(metadata.addresses)
    logger.info("Egress policy: refusing this server's own addresses %s", own_addresses())
