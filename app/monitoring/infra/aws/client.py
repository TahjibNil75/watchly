"""The one place boto3 is imported.

boto3 is synchronous, so every call runs in a worker thread
(`anyio.to_thread`). Every call names a `Region`: one region of one AWS
account (an `aws_accounts` row). An account is reached with Watchly's own
credentials, boto3's usual chain (on EC2, the instance role), or with an
access key stored for it; either may then assume a role in the account. One
session is kept per account, and editing an account's credentials makes a new
one. `AWS_ENDPOINT_URL`, which boto3 reads itself, points every client at an
emulator for local testing.

Every failure comes back as `AwsError`, with AWS's error code when there is
one, so callers can show "AccessDenied on elasticloadbalancing:DescribeTargetHealth"
instead of a traceback.
"""

import functools
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
import boto3
import botocore.session
from botocore.config import Config
from botocore.credentials import DeferredRefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, NoRegionError

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.core.instance_metadata import InstanceMetadata, read_instance_metadata

logger = logging.getLogger(__name__)

#: Short timeouts: a check or a page waits on these. Standard retries
#: back off on throttling.
_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 3, "mode": "standard"},
    user_agent_extra="watchly-monitor",
)
#: Pages read of one listing at most, so one huge account cannot hold a
#: request for minutes.
MAX_PAGES = 50
#: How long what the instance metadata said is reused.
IDENTITY_TTL_SECONDS = 600
#: How long one assumed role's credentials last; boto3 asks again before
#: they run out.
ROLE_SESSION_SECONDS = 3600
#: Sessions kept, one per account and set of credentials.
MAX_SESSIONS = 64


class AwsError(Exception):
    """An AWS call that did not work, as `Code: message`."""

    def __init__(self, operation: str, code: str, message: str) -> None:
        self.operation = operation
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)

    @property
    def access_denied(self) -> bool:
        return self.code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}


@dataclass(frozen=True, slots=True)
class Account:
    """How one AWS account is reached. Frozen and hashable: it keys the
    session cache, so changed credentials get a session of their own."""

    #: The `aws_accounts` row; None for Watchly's own credentials alone.
    id: int | None
    name: str
    #: None: boto3's chain (the instance role on EC2, or the environment).
    access_key_id: str | None = None
    #: None with an access key when it could not be decrypted.
    secret_access_key: str | None = field(default=None, repr=False)
    #: Assumed on top of the credentials above.
    role_arn: str | None = None
    external_id: str | None = field(default=None, repr=False)
    #: Its default region, if it has one.
    region: str | None = None

    @classmethod
    def of(cls, row: Any) -> "Account":
        """From an `AwsAccount` row."""
        return cls(
            id=row.id,
            name=row.name,
            access_key_id=row.access_key_id if row.auth_type == "access_key" else None,
            secret_access_key=decrypt_secret(row.secret_access_key) if row.auth_type == "access_key" else None,
            role_arn=row.role_arn,
            external_id=row.external_id,
            region=row.default_region,
        )


#: Watchly's own credentials, as boto3 finds them.
OWN = Account(id=None, name="Watchly's own credentials")


@dataclass(frozen=True, slots=True)
class Region:
    """One region of one account: where a call goes."""

    account: Account
    name: str

    @classmethod
    def of(cls, vpc: Any) -> "Region":
        """Where an `AwsVpc` lives. Its account is loaded with it."""
        return cls(Account.of(vpc.account), vpc.region)

    def __str__(self) -> str:
        return self.name


def _assume_role(base: boto3.session.Session, account: Account) -> boto3.session.Session:
    """A session whose credentials come from assuming the account's role
    with `base`'s, renewed by botocore before they expire."""
    sts = base.client("sts", region_name=account.region or settings.AWS_REGION or "us-east-1", config=_CONFIG)

    def refresh() -> dict:
        params: dict[str, Any] = {
            "RoleArn": account.role_arn,
            "RoleSessionName": "watchly-monitor",
            "DurationSeconds": ROLE_SESSION_SECONDS,
        }
        if account.external_id:
            params["ExternalId"] = account.external_id
        credentials = sts.assume_role(**params)["Credentials"]
        return {
            "access_key": credentials["AccessKeyId"],
            "secret_key": credentials["SecretAccessKey"],
            "token": credentials["SessionToken"],
            "expiry_time": credentials["Expiration"].isoformat(),
        }

    core = botocore.session.get_session()
    # Deferred: nothing is asked of AWS until the first call, which runs in a
    # worker thread. botocore has no public setter for a session's credentials.
    core._credentials = DeferredRefreshableCredentials(refresh_using=refresh, method="sts-assume-role")
    return boto3.session.Session(botocore_session=core)


@functools.lru_cache(maxsize=MAX_SESSIONS)
def _session(account: Account) -> boto3.session.Session:
    if account.access_key_id:
        if not account.secret_access_key:
            raise AwsError(
                "session",
                "NoCredentials",
                f"The secret access key of {account.name} cannot be read (SECRET_KEY changed?): enter it again.",
            )
        base = boto3.session.Session(
            aws_access_key_id=account.access_key_id, aws_secret_access_key=account.secret_access_key
        )
    else:
        base = boto3.session.Session()
    return _assume_role(base, account) if account.role_arn else base


@functools.lru_cache(maxsize=MAX_SESSIONS * 8)
def _client(account: Account, service: str, region: str) -> Any:
    # boto3 clients are thread-safe; sessions are not, hence one made here.
    return _session(account).client(service, region_name=region, config=_CONFIG)


_identity: tuple[float, InstanceMetadata | None] | None = None


async def identity(refresh: bool = False) -> InstanceMetadata | None:
    """Where Watchly runs, from the instance metadata; None off EC2."""
    global _identity
    if not refresh and _identity is not None and time.monotonic() - _identity[0] < IDENTITY_TTL_SECONDS:
        return _identity[1]
    metadata = await read_instance_metadata()
    _identity = (time.monotonic(), metadata)
    return metadata


async def default_region(account: Account = OWN) -> str | None:
    """The account's own default region, else AWS_REGION, else the instance's
    own region, else what boto3 finds (AWS_DEFAULT_REGION, the config file)."""
    if account.region:
        return account.region
    if settings.AWS_REGION:
        return settings.AWS_REGION
    metadata = await identity()
    if metadata is not None and metadata.region:
        return metadata.region
    return boto3.session.Session().region_name


def _translate(operation: str, exc: Exception) -> AwsError:
    if isinstance(exc, AwsError):
        return exc
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return AwsError(operation, error.get("Code") or "ClientError", error.get("Message") or "")
    if isinstance(exc, NoCredentialsError):
        return AwsError(
            operation,
            "NoCredentials",
            "No AWS credentials: attach an instance role to the EC2 instance Watchly runs on, "
            "or give the account an access key.",
        )
    if isinstance(exc, NoRegionError):
        return AwsError(operation, "NoRegion", "No AWS region: set AWS_REGION.")
    if isinstance(exc, BotoCoreError):
        return AwsError(operation, type(exc).__name__, str(exc))
    return AwsError(operation, type(exc).__name__, str(exc))


async def call(service: str, operation: str, region: Region, **kwargs: Any) -> dict:
    """One API call, e.g. `call("ec2", "describe_vpcs", region, VpcIds=[...])`."""
    try:
        method = getattr(_client(region.account, service, region.name), operation)
        response = await anyio.to_thread.run_sync(functools.partial(method, **kwargs))
    except Exception as exc:  # noqa: BLE001 - every failure becomes an AwsError
        raise _translate(f"{service}:{operation}", exc) from exc
    response.pop("ResponseMetadata", None)
    return response


async def paginate(service: str, operation: str, region: Region, key: str, **kwargs: Any) -> list:
    """Every item of a paginated listing, e.g. all `Reservations`."""

    def collect(client: Any) -> list:
        items: list = []
        paginator = client.get_paginator(operation)
        for index, page in enumerate(paginator.paginate(**kwargs)):
            items.extend(page.get(key, []))
            if index + 1 >= MAX_PAGES:
                logger.warning("%s:%s: stopped after %d pages", service, operation, MAX_PAGES)
                break
        return items

    try:
        # Made here, on the event loop: sessions are not thread-safe.
        client = _client(region.account, service, region.name)
        return await anyio.to_thread.run_sync(collect, client)
    except Exception as exc:  # noqa: BLE001
        raise _translate(f"{service}:{operation}", exc) from exc


def tags(items: list[dict] | None) -> dict[str, str]:
    """AWS's `[{"Key": ..., "Value": ...}]` as a dict."""
    return {item["Key"]: item.get("Value", "") for item in items or [] if "Key" in item}
