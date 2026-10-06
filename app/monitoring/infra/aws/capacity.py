"""What an AWS account holds rather than what runs in it: Elastic IPs left
attached to nothing, and how close it is to its Elastic IP quota.

An AWS account with `watch_capacity` is looked at every
CAPACITY_WATCH_INTERVAL_SECONDS, in each region it has VPCs in (and its
default region):

    eip_unattached  Elastic IPs associated with nothing on two looks in a row
                    (one moved between instances is not). AWS bills every
                    public IPv4 address by the hour. Alerted when one is
                    found that has not been alerted on yet; quiet while the
                    same ones stay.
    eip_quota       the region's Elastic IPs at CAPACITY_QUOTA_PERCENT or more
                    of its quota: the account's own (Service Quotas), else
                    AWS's default, else 5. Alerted as it opens, unless it
                    did within INFRA_PROBLEM_ALERT_COOLDOWN_SECONDS.

Its findings and open problems are kept on the account (`capacity`), so the
page can show them and a restart forgets nothing. A region AWS will not
answer for keeps what was last found there, and the error is recorded.

`DescribeAddresses` and the Service Quotas reads are free.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.monitoring.infra.aws import client
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.events import CapacityEvent
from app.monitoring.infra.aws.models import AwsAccount, AwsEvent, AwsVpc
from app.monitoring.infra.aws.schemas import CapacityRegion, UnattachedAddress
from app.monitoring.notifications.dispatcher import Notifier
from app.monitoring.notifications.recipients import (
    project_recipients,
    slack_target,
    telegram_target,
    whatsapp_target,
)

logger = logging.getLogger(__name__)

#: Service Quotas' code for "EC2-VPC Elastic IPs", per region.
EIP_QUOTA_CODE = "L-0263D0A3"
#: What AWS gives every account, used when Service Quotas cannot be asked.
DEFAULT_EIP_QUOTA = 5
#: What one public IPv4 address costs a month, about: US$0.005 an hour.
IPV4_MONTHLY_USD = 3.65
#: Addresses named in one alert's detail; the facts list up to ten.
NAMED = 5


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def forget_capacity(account: AwsAccount) -> None:
    """Drop what was found and the open problems. Not committed."""
    account.capacity = {}
    account.capacity_checked_at = None
    account.capacity_error = None


def capacity_regions(account: AwsAccount) -> list[CapacityRegion]:
    """What the last look found, per region, for the API."""
    if not account.watch_capacity:
        return []
    regions = (account.capacity or {}).get("regions") or {}
    return [
        CapacityRegion(
            region=name,
            elastic_ips=found.get("elastic_ips", 0),
            elastic_ip_quota=found.get("elastic_ip_quota"),
            quota_source=found.get("quota_source"),
            unattached=[UnattachedAddress(**row) for row in found.get("unattached", [])],
        )
        for name, found in sorted(regions.items())
    ]


def address_text(row: dict) -> str:
    """`203.0.113.10 (old-bastion, eipalloc-0a1b…)`."""
    extra = ", ".join(filter(None, [row.get("name"), row.get("allocation_id")]))
    return f"{row['public_ip']} ({extra})" if extra else row["public_ip"]


def unattached_detail(rows: list[dict]) -> str:
    count = len(rows)
    named = ", ".join(
        f"{r['public_ip']} ({r['name']})" if r.get("name") else r["public_ip"] for r in rows[:NAMED]
    )
    more = f" and {count - NAMED} more" if count > NAMED else ""
    noun = "Elastic IP" if count == 1 else "Elastic IPs"
    return f"{count} {noun} attached to nothing: {named}{more}; about US${count * IPV4_MONTHLY_USD:.2f} a month"


def quota_detail(used: int, quota: int, source: str) -> str:
    percent = used * 100 / quota
    assumed = ", AWS's default assumed: Service Quotas could not be read" if source == "assumed" else ""
    return (
        f"{used} of {quota} Elastic IPs used, {percent:.0f}% "
        f"(alert at {settings.CAPACITY_QUOTA_PERCENT}%{assumed})"
    )


class CapacityWatcher:
    """Looks at the Elastic IPs of the accounts that watch their capacity."""

    def __init__(self, session: AsyncSession, notifier: Notifier | None = None) -> None:
        self.session = session
        self.notifier = notifier or Notifier(session)

    async def run_due(self, now: datetime | None = None) -> int:
        """Look at every account whose look is due. Called by the scheduler.
        Returns how many accounts were looked at."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(seconds=settings.CAPACITY_WATCH_INTERVAL_SECONDS)
        accounts = list(
            await self.session.scalars(
                select(AwsAccount)
                .where(
                    AwsAccount.watch_capacity.is_(True),
                    (AwsAccount.capacity_checked_at.is_(None)) | (AwsAccount.capacity_checked_at <= cutoff),
                )
                .order_by(AwsAccount.id)
            )
        )
        for account in accounts:
            try:
                await self.watch_account(account)
            except Exception:
                logger.exception("Looking at the capacity of %s failed; continuing.", account.name)
                await self.session.rollback()
        return len(accounts)

    async def _regions(self, account: AwsAccount, credentials: client.Account) -> list[str]:
        """Where its VPCs are, and its default region; with neither,
        Watchly's own region."""
        names = set(
            await self.session.scalars(select(AwsVpc.region).where(AwsVpc.account_id == account.id).distinct())
        )
        if account.default_region:
            names.add(account.default_region)
        if not names:
            own = await client.default_region(credentials)
            if own:
                names.add(own)
        return sorted(names)

    async def watch_account(self, account: AwsAccount) -> list[CapacityEvent]:
        credentials = client.Account.of(account)
        state = dict(account.capacity or {})
        before = dict(state.get("regions") or {})
        problems = {key: dict(entry) for key, entry in (state.get("problems") or {}).items()}
        seen = dict(state.get("seen_unattached") or {})
        regions: dict[str, dict] = {}
        errors: list[str] = []
        events: list[CapacityEvent] = []
        now = datetime.now(UTC)
        names = await self._regions(account, credentials)
        for name in names:
            try:
                found = await self._look(client.Region(credentials, name))
            except AwsError as exc:
                errors.append(f"{name}: {exc}")
                if name in before:
                    regions[name] = before[name]
                continue
            regions[name] = found
            confirmed = [r for r in found["unattached"] if r["allocation_id"] in set(seen.get(name) or [])]
            seen[name] = [r["allocation_id"] for r in found["unattached"]]
            events.extend(self._judge(account, name, found, confirmed, problems, now))

        # A region no longer looked at takes its problems with it.
        problems = {key: entry for key, entry in problems.items() if key.partition(":")[0] in names}
        seen = {name: ids for name, ids in seen.items() if name in names}
        account.capacity = {"regions": regions, "problems": problems, "seen_unattached": seen}
        error = "; ".join(errors)[:2000] or None
        if error and error != account.capacity_error:
            logger.warning("Capacity of %s could not be looked at: %s", account.name, error)
        account.capacity_error = error
        account.capacity_checked_at = now
        for event in events:
            self.session.add(
                AwsEvent(
                    project_id=event.project_id,
                    vpc_id=None,
                    resource_id=None,
                    kind=event.kind.value,
                    occurred_at=now,
                    summary=f"{account.name} in {event.region}: {event.problem_detail}"[:2000],
                )
            )
        # Commit before sending, so a slow channel can never make the same
        # alert go out twice.
        await self.session.commit()
        for event in events:
            await self.notifier.dispatch(event)
        return events

    async def _look(self, region: client.Region) -> dict:
        """The region's Elastic IPs and quota. Raises AwsError when its
        addresses cannot be listed; a quota that cannot be read is assumed."""
        response = await client.call(
            "ec2", "describe_addresses", region, Filters=[{"Name": "domain", "Values": ["vpc"]}]
        )
        addresses = response.get("Addresses") or []
        unattached = [
            {
                "public_ip": a.get("PublicIp") or "?",
                "allocation_id": a.get("AllocationId"),
                "name": client.tags(a.get("Tags")).get("Name") or None,
            }
            for a in addresses
            if not (a.get("AssociationId") or a.get("InstanceId") or a.get("NetworkInterfaceId"))
        ]
        unattached.sort(key=lambda r: r["public_ip"])
        quota, source = await self._quota(region)
        return {
            "elastic_ips": len(addresses),
            "elastic_ip_quota": quota,
            "quota_source": source,
            "unattached": unattached,
        }

    @staticmethod
    async def _quota(region: client.Region) -> tuple[int, str]:
        """The account's own quota, else AWS's default, else DEFAULT_EIP_QUOTA."""
        for operation, source in (("get_service_quota", "applied"), ("get_aws_default_service_quota", "default")):
            try:
                response = await client.call(
                    "service-quotas", operation, region, ServiceCode="ec2", QuotaCode=EIP_QUOTA_CODE
                )
            except AwsError:
                continue
            value = (response.get("Quota") or {}).get("Value")
            if value:
                return int(value), source
        return DEFAULT_EIP_QUOTA, "assumed"

    def _judge(
        self,
        account: AwsAccount,
        region: str,
        found: dict,
        confirmed: list[dict],
        problems: dict[str, dict],
        now: datetime,
    ) -> list[CapacityEvent]:
        """Open or close the region's two problems; the alerts to send."""
        events: list[CapacityEvent] = []

        key = f"{region}:eip_unattached"
        entry = problems.get(key, {})
        if confirmed:
            ids = [r["allocation_id"] for r in confirmed]
            fresh = [i for i in ids if i not in set(entry.get("alerted") or [])]
            entry.setdefault("since", now.isoformat())
            entry["detail"] = unattached_detail(confirmed)
            if fresh:
                entry["last_alert_at"] = now.isoformat()
                events.append(self._event(account, region, "eip_unattached", entry["detail"], now, confirmed))
            # Only those still unattached: one attached and freed again is new.
            entry["alerted"] = ids
            problems[key] = entry
        else:
            problems.pop(key, None)

        key = f"{region}:eip_quota"
        entry = problems.get(key, {})
        quota = found["elastic_ip_quota"]
        used = found["elastic_ips"]
        if quota and used * 100 >= settings.CAPACITY_QUOTA_PERCENT * quota:
            entry["detail"] = quota_detail(used, quota, found["quota_source"])
            if not entry.get("since"):
                entry["since"] = now.isoformat()
                last = _parse(entry.get("last_alert_at"))
                if last is None or (now - last).total_seconds() >= settings.INFRA_PROBLEM_ALERT_COOLDOWN_SECONDS:
                    entry["last_alert_at"] = now.isoformat()
                    events.append(self._event(account, region, "eip_quota", entry["detail"], now))
            problems[key] = entry
        elif entry:
            # Kept closed, so the cooldown still holds if it comes back soon.
            problems[key] = {"last_alert_at": entry.get("last_alert_at")}
        return events

    @staticmethod
    def _event(
        account: AwsAccount,
        region: str,
        problem: str,
        detail: str,
        now: datetime,
        addresses: list[dict] | None = None,
    ) -> CapacityEvent:
        project = account.project
        return CapacityEvent(
            project_id=project.id,
            project_name=project.name,
            account_name=account.name,
            aws_account_id=account.aws_account_id,
            region=region,
            problem=problem,
            problem_detail=detail,
            occurred_at=now,
            addresses=tuple(address_text(r) for r in addresses or []),
            cooldown_seconds=settings.INFRA_PROBLEM_ALERT_COOLDOWN_SECONDS,
            recipients=project_recipients(project),
            slack=slack_target(project),
            telegram=telegram_target(project),
            whatsapp=whatsapp_target(project),
        )
