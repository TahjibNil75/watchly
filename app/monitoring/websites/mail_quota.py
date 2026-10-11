"""How much of a mail provider's sending quota is used, over its own API.

A mail check's SMTP login (`mail_probe`) says the server is there and the
credentials work. It cannot say that the next message will be taken: once a
quota runs out, the server greets, logs in, and refuses every message. So for
the providers that publish their quota, a mail check also reads it every
MAIL_QUOTA_CHECK_INTERVAL_SECONDS:

- Amazon SES, with `sesv2:GetAccount`, which needs the `ses:GetAccount`
  permission. Its quota is a rolling 24 hours (`Max24HourSend`), and it also
  says whether sending is enabled, whether the account is in the sandbox, and
  its reputation status (healthy, probation, shutdown). The credentials are
  an AWS account registered for infrastructure, an access key of the check's
  own, or Watchly's own (the instance role on EC2), in that order.
- Resend, with `GET /usage`, which needs a full-access API key (a
  sending-only key is refused). Its quota is per day (free plan) and per
  month, each with when it resets.

Neither call sends anything or counts against the quota. A read that fails
comes back as a `MailQuota` with `error` set; it never raises, and never
makes the check down.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.monitoring.infra.aws import client as aws
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.websites.db_probe import split_endpoint
from app.monitoring.websites.models import MailProvider, Website

logger = logging.getLogger(__name__)

#: SES's SMTP endpoints name their region: `email-smtp.eu-west-1.amazonaws.com`.
_SES_HOST = re.compile(r"^email-smtp(?:-fips)?\.([a-z]{2}(?:-[a-z]+)+-\d)\.amazonaws\.com$")
#: The providers whose quota can be read.
QUOTA_PROVIDERS = frozenset({MailProvider.SES, MailProvider.RESEND})
#: What each quota window is called, for alerts and the page.
PERIOD_LABELS = {"24h": "last 24 hours", "day": "today", "month": "this month"}
#: Resend's account usage endpoint, under RESEND_API_URL.
RESEND_USAGE_PATH = "/usage"
#: How long the provider's API may take.
QUOTA_TIMEOUT_SECONDS = 15.0


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(slots=True)
class QuotaWindow:
    """One stretch the provider counts sends over."""

    #: `24h` (SES, rolling), `day` or `month` (Resend).
    period: str
    used: int
    #: None: the plan sets no limit for this window.
    limit: int | None
    #: When the count starts again; None for a rolling window.
    resets_at: datetime | None = None

    @property
    def label(self) -> str:
        return PERIOD_LABELS.get(self.period, self.period)

    @property
    def percent(self) -> float | None:
        if not self.limit:
            return None
        return self.used * 100 / self.limit

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(self.limit - self.used, 0)

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "percent": round(self.percent, 1) if self.percent is not None else None,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QuotaWindow":
        return cls(
            period=data["period"],
            used=int(data.get("used") or 0),
            limit=data.get("limit"),
            resets_at=_parse(data.get("resets_at")),
        )


@dataclass(slots=True)
class MailQuota:
    """What one read found. `error` is set only when it failed."""

    provider: MailProvider
    read_at: datetime
    windows: list[QuotaWindow] = field(default_factory=list)
    #: SES: whether the account may send at all. AWS pauses it for a bad
    #: bounce or complaint rate, or by hand.
    sending_enabled: bool | None = None
    #: SES: the account's reputation status, `healthy`, `probation` or
    #: `shutdown`.
    status: str | None = None
    #: SES: still in the sandbox, which only sends to verified addresses,
    #: 200 a day.
    sandbox: bool | None = None
    #: SES: messages a second, at most.
    max_send_rate: float | None = None
    #: SES: the region read.
    region: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict:
        """What a site keeps of the last good read, and a check of its own."""
        return {
            "provider": self.provider.value,
            "read_at": self.read_at.isoformat(),
            "windows": [window.as_dict() for window in self.windows],
            "sending_enabled": self.sending_enabled,
            "status": self.status,
            "sandbox": self.sandbox,
            "max_send_rate": self.max_send_rate,
            "region": self.region,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MailQuota":
        return cls(
            provider=MailProvider(data["provider"]),
            read_at=_parse(data.get("read_at")) or datetime.now(UTC),
            windows=[QuotaWindow.from_dict(w) for w in data.get("windows") or []],
            sending_enabled=data.get("sending_enabled"),
            status=data.get("status"),
            sandbox=data.get("sandbox"),
            max_send_rate=data.get("max_send_rate"),
            region=data.get("region"),
            error=data.get("error"),
        )


def ses_region(website: Website) -> str | None:
    """The region to read SES's quota in: the check's own, else the one its
    SMTP host names."""
    if website.mail_region:
        return website.mail_region
    return region_from_host(split_endpoint(website.url)[0])


def region_from_host(host: str) -> str | None:
    """`eu-west-1` for `email-smtp.eu-west-1.amazonaws.com`, else None."""
    match = _SES_HOST.match(host.lower())
    return match.group(1) if match else None


def mail_quota_due(website: Website, now: datetime | None = None) -> bool:
    """Whether this check should also read the quota: the first time, then
    every MAIL_QUOTA_CHECK_INTERVAL_SECONDS. The attempt time is stored even
    when the read fails, which keeps a refused key from being tried every tick."""
    if website.mail_provider not in QUOTA_PROVIDERS:
        return False
    if website.mail_quota_checked_at is None:
        return True
    elapsed = ((now or datetime.now(UTC)) - website.mail_quota_checked_at).total_seconds()
    return elapsed >= settings.MAIL_QUOTA_CHECK_INTERVAL_SECONDS


async def _registered_account(account_id: int) -> aws.Account | None:
    """An AWS account registered for infrastructure, read in a session of its
    own: a check runs outside the scheduler's. None once it was removed."""
    # Here, not at the top: `infra.aws.models` imports the websites models.
    from app.db.session import AsyncSessionLocal
    from app.monitoring.infra.aws.models import AwsAccount

    async with AsyncSessionLocal() as session:
        row = await session.get(AwsAccount, account_id)
        return aws.Account.of(row) if row is not None else None


async def _aws_account(website: Website) -> aws.Account:
    """Whose credentials read SES's quota."""
    if website.mail_aws_account_id is not None:
        account = await _registered_account(website.mail_aws_account_id)
        if account is not None:
            return account
    if website.mail_aws_access_key_id:
        return aws.Account(
            id=None,
            name=f"the access key of {website.name}",
            access_key_id=website.mail_aws_access_key_id,
            secret_access_key=decrypt_secret(website.mail_aws_secret_access_key),
        )
    return aws.OWN


async def _read_ses(website: Website, quota: MailQuota) -> None:
    region = ses_region(website)
    if region is None:
        quota.error = (
            "No SES region: give the check one, or use the host "
            "email-smtp.<region>.amazonaws.com."
        )
        return
    quota.region = region
    account = await _aws_account(website)
    try:
        response = await aws.call("sesv2", "get_account", aws.Region(account, region))
    except AwsError as exc:
        if exc.access_denied:
            quota.error = (
                f"{account.name} may not read SES's quota in {region}: allow "
                f"ses:GetAccount. ({exc})"
            )
        else:
            quota.error = f"SES did not answer in {region}: {exc}"
        return
    send = response.get("SendQuota") or {}
    limit = send.get("Max24HourSend")
    quota.windows = [
        QuotaWindow(
            period="24h",
            used=int(send.get("SentLast24Hours") or 0),
            # -1 is unlimited.
            limit=int(limit) if limit is not None and limit >= 0 else None,
        )
    ]
    quota.max_send_rate = send.get("MaxSendRate")
    quota.sending_enabled = response.get("SendingEnabled")
    status = response.get("EnforcementStatus")
    quota.status = status.lower() if isinstance(status, str) else None
    production = response.get("ProductionAccessEnabled")
    quota.sandbox = None if production is None else not production


def _resend_key(website: Website) -> str | None:
    return decrypt_secret(website.mail_api_key) or decrypt_secret(website.mail_password)


async def _read_resend(website: Website, quota: MailQuota, client: httpx.AsyncClient | None) -> None:
    key = _resend_key(website)
    if not key:
        quota.error = "No Resend API key to read the quota with: give the check one."
        return
    url = settings.RESEND_API_URL.rstrip("/") + RESEND_USAGE_PATH
    owns = client is None
    client = client or httpx.AsyncClient()
    try:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {key}", "User-Agent": "Watchly-Monitor/1.0"},
            timeout=QUOTA_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        quota.error = f"Resend's API did not answer: {type(exc).__name__}: {exc}".rstrip(": ")
        return
    finally:
        if owns:
            await client.aclose()
    if response.status_code in (401, 403):
        quota.error = (
            f"Resend refused the API key (HTTP {response.status_code}): reading the "
            "quota needs a full-access key; a sending-only key cannot."
        )
        return
    if response.status_code == 429:
        quota.error = "Resend's rate limit was hit; the quota is read again later."
        return
    if response.status_code != 200:
        quota.error = f"Resend's API answered HTTP {response.status_code}."
        return
    try:
        emails = response.json()["emails"]
        windows = []
        for period in ("day", "month"):
            data = emails.get("daily" if period == "day" else "monthly")
            if not isinstance(data, dict):
                continue
            limit = data.get("limit")
            windows.append(
                QuotaWindow(
                    period=period,
                    used=int(data.get("used") or 0),
                    limit=int(limit) if isinstance(limit, int | float) and limit > 0 else None,
                    resets_at=_parse(data.get("resets_at")),
                )
            )
    except (ValueError, KeyError, TypeError, AttributeError):
        quota.error = "Resend's API answered with something other than its usage."
        return
    quota.windows = windows


async def read_quota(website: Website, client: httpx.AsyncClient | None = None) -> MailQuota:
    """The provider's quota as it stands. Never raises."""
    quota = MailQuota(provider=website.mail_provider or MailProvider.SMTP, read_at=datetime.now(UTC))
    try:
        if website.mail_provider is MailProvider.SES:
            await _read_ses(website, quota)
        elif website.mail_provider is MailProvider.RESEND:
            await _read_resend(website, quota, client)
        else:
            quota.error = "This provider's quota cannot be read."
    except Exception as exc:  # noqa: BLE001 - a failed read is data, not a crash
        logger.exception("Reading the mail quota of %s failed", website.url)
        quota.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if quota.error:
        logger.info("Mail quota of %s not read: %s", website.url, quota.error)
    return quota
