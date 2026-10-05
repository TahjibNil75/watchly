"""What the EC2 instance metadata service says about the server Watchly runs on.

Read over IMDSv2: a `PUT` for a session token, then `GET`s that carry it. It
works the same when the instance still allows IMDSv1. Off EC2 nothing answers
at 169.254.169.254, so every read gives up after a second and the result is
None; callers treat that as "not on EC2".

The egress policy (`app/monitoring/egress.py`) refuses this address to every
probe. Reading it here is not a probe: the path is fixed, never taken from a
check.
"""

import json
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

IMDS_URL = "http://169.254.169.254"
#: Seconds for each request. The service answers in milliseconds when it is
#: there at all.
TIMEOUT = 1.0
#: How long a token is asked to live; one read needs it for a moment only.
TOKEN_TTL_SECONDS = 60


@dataclass(slots=True)
class InstanceMetadata:
    """The parts of the metadata Watchly uses."""

    instance_id: str
    instance_type: str | None
    region: str | None
    availability_zone: str | None
    account_id: str | None
    private_ip: str | None
    public_ip: str | None
    vpc_id: str | None
    subnet_id: str | None
    #: The primary interface's security groups, as (id, name).
    security_groups: list[tuple[str, str]] = field(default_factory=list)
    iam_role: str | None = None
    #: Every address of every interface: what the egress policy must never
    #: let a probe connect back to.
    addresses: list[str] = field(default_factory=list)
    #: False when a plain GET without a token was answered too, i.e. the
    #: instance still allows IMDSv1.
    tokens_required: bool = True


async def _get(client: httpx.AsyncClient, path: str, token: str | None) -> str | None:
    headers = {"X-aws-ec2-metadata-token": token} if token else {}
    response = await client.get(f"{IMDS_URL}/latest/{path}", headers=headers)
    if response.status_code != 200:
        return None
    return response.text.strip()


async def read_instance_metadata() -> InstanceMetadata | None:
    """Everything above, or None off EC2 (or with the service turned off)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            token_response = await client.put(
                f"{IMDS_URL}/latest/api/token",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": str(TOKEN_TTL_SECONDS)},
            )
            if token_response.status_code != 200:
                return None
            token = token_response.text.strip()

            instance_id = await _get(client, "meta-data/instance-id", token)
            if not instance_id:
                return None
            # IMDSv1 still on: the same read answers without a token.
            unauthenticated = await client.get(f"{IMDS_URL}/latest/meta-data/instance-id")
            identity = {}
            document = await _get(client, "dynamic/instance-identity/document", token)
            if document:
                try:
                    identity = json.loads(document)
                except ValueError:
                    identity = {}

            metadata = InstanceMetadata(
                instance_id=instance_id,
                instance_type=identity.get("instanceType")
                or await _get(client, "meta-data/instance-type", token),
                region=identity.get("region"),
                availability_zone=identity.get("availabilityZone"),
                account_id=identity.get("accountId"),
                private_ip=await _get(client, "meta-data/local-ipv4", token),
                public_ip=await _get(client, "meta-data/public-ipv4", token),
                vpc_id=None,
                subnet_id=None,
                tokens_required=unauthenticated.status_code == 401,
            )

            role = await _get(client, "meta-data/iam/security-credentials/", token)
            metadata.iam_role = role.splitlines()[0] if role else None

            addresses: list[str] = []
            primary_mac = await _get(client, "meta-data/mac", token)
            macs = await _get(client, "meta-data/network/interfaces/macs/", token) or ""
            for mac in (line.strip().rstrip("/") for line in macs.splitlines()):
                if not mac:
                    continue
                base = f"meta-data/network/interfaces/macs/{mac}"
                for key in ("local-ipv4s", "public-ipv4s", "ipv6s"):
                    values = await _get(client, f"{base}/{key}", token)
                    addresses.extend(v.strip() for v in (values or "").splitlines() if v.strip())
                if mac == primary_mac:
                    metadata.vpc_id = await _get(client, f"{base}/vpc-id", token)
                    metadata.subnet_id = await _get(client, f"{base}/subnet-id", token)
                    ids = (await _get(client, f"{base}/security-group-ids", token) or "").split()
                    names = (await _get(client, f"{base}/security-groups", token) or "").split()
                    metadata.security_groups = [
                        (group_id, names[index] if index < len(names) else "")
                        for index, group_id in enumerate(ids)
                    ]
            for address in (metadata.private_ip, metadata.public_ip):
                if address and address not in addresses:
                    addresses.append(address)
            metadata.addresses = addresses
            return metadata
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("No instance metadata service: %s", exc)
        return None
