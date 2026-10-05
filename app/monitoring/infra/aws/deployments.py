"""CodeDeploy deployments: announced, and their resources silenced while they run.

An AWS account with `watch_deployments` is asked, every
DEPLOY_WATCH_INTERVAL_SECONDS, which CodeDeploy deployments are in progress in
each region it has VPCs in (and its default region):

    new deployment  -> deploy_started to its project; each monitored resource
                       it deploys to gets a maintenance window tied to it, so
                       its checks and down alerts pause
    still running   -> those windows are renewed DEPLOY_SILENCE_LEASE_MINUTES
                       ahead, never past DEPLOY_SILENCE_MAX_MINUTES from when
                       Watchly first saw it
    ended           -> deploy_finished (succeeded, failed or stopped); the
                       windows end DEPLOY_SETTLE_SECONDS later, and a resource
                       still down then alerts as usual

A short lease, renewed, rather than one long window: a deployment Watchly
loses sight of (the account's credentials break, Watchly stops) silences
nothing within minutes, not hours. A window someone ends by hand is not
renewed.

Which resources a deployment touches comes from its deployment group: the
Auto Scaling groups it deploys to and their servers, the servers its EC2 tag
filters match, and the load balancers and groups using the target groups it
shifts traffic on or that its groups are registered with. A database is never
silenced: a deployment does not take one down. When the group cannot be read,
every server, load balancer and Auto Scaling group of the account's region is
silenced instead, and the announcement says so.

Deployments CodeDeploy runs on its own for an instance an Auto Scaling group
launches or terminates (`creator` autoscaling) are scaling, not a release,
and are left alone.
"""

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.monitoring.alerts.base import NotificationKind, format_duration
from app.monitoring.infra.aws import client
from app.monitoring.infra.aws.client import AwsError
from app.monitoring.infra.aws.events import DeployedResource, DeployEvent
from app.monitoring.infra.aws.models import (
    AwsAccount,
    AwsDeployment,
    AwsEvent,
    AwsMaintenanceWindow,
    AwsResource,
    AwsVpc,
    ResourceKind,
)
from app.monitoring.notifications.dispatcher import Notifier
from app.monitoring.notifications.recipients import (
    project_recipients,
    slack_target,
    telegram_target,
    whatsapp_target,
)
from app.monitoring.websites.models import WebsiteEnvironment

logger = logging.getLogger(__name__)

#: CodeDeploy's states of a deployment that has not ended. `Ready` is a
#: blue/green one waiting to reroute traffic.
ACTIVE_STATUSES = ("Created", "Queued", "InProgress", "Baking", "Ready")
#: Who CodeDeploy names when it deploys on its own to an instance an Auto
#: Scaling group launches or terminates.
SCALING_CREATORS = frozenset({"autoscaling", "autoscalingTermination"})
#: Deployments `batch_get_deployments` takes at once.
BATCH = 25
#: What a deployment can silence.
SILENCED_KINDS = (ResourceKind.SERVER, ResourceKind.LOAD_BALANCER, ResourceKind.AUTO_SCALING_GROUP)
#: Status given to deployments still open when an account stops watching.
UNWATCHED = "Unwatched"

#: Scaling deployments already looked at, so they are not described again at
#: every look while they run. Only saves calls: losing it loses nothing.
_ignored: set[tuple[int, str, str]] = set()


def revision_text(revision: dict | None) -> str | None:
    """What a deployment deploys, in a line."""
    if not revision:
        return None
    kind = revision.get("revisionType")
    if kind == "GitHub":
        github = revision.get("gitHubLocation") or {}
        commit = (github.get("commitId") or "")[:7]
        text = f"github {github.get('repository') or '?'}" + (f"@{commit}" if commit else "")
    elif kind == "S3":
        s3 = revision.get("s3Location") or {}
        text = f"s3://{s3.get('bucket') or '?'}/{s3.get('key') or ''}"
        if s3.get("version"):
            text += f" (version {s3['version']})"
    elif kind in {"AppSpecContent", "String"}:
        text = "AppSpec content"
    else:
        text = kind
    return text[:1024] if text else None


def _filter_matches(item: dict, tags: dict[str, str]) -> bool:
    """One EC2 tag filter of a deployment group, against an instance's tags."""
    key, value, kind = item.get("Key"), item.get("Value"), item.get("Type")
    if kind == "KEY_ONLY":
        return key in tags
    if kind == "VALUE_ONLY":
        return value in tags.values()
    return key in tags and tags[key] == value


def _tag_selectors(group: dict, targets: dict) -> list[list[list[dict]]]:
    """The tag filters an instance may match to be deployed to: any one
    selector, whose lists must all match, each with any one of its filters.
    A flat filter list is one selector of one list; a tag set is one selector
    of its lists."""
    selectors = []
    for source, flat in ((group, "ec2TagFilters"), (targets, "tagFilters")):
        if source.get(flat):
            selectors.append([source[flat]])
        tag_set = [lists for lists in (source.get("ec2TagSet") or {}).get("ec2TagSetList") or [] if lists]
        if tag_set:
            selectors.append(tag_set)
    return selectors


def _target_group_names(detail: dict | None) -> set[str]:
    return {g["name"] for g in (detail or {}).get("target_groups") or [] if g.get("name")}


def match(resources: list[AwsResource], group: dict, deployment: dict) -> list[AwsResource]:
    """The resources a deployment deploys to, or takes traffic from."""
    targets = deployment.get("targetInstances") or {}
    groups = {g["name"] for g in group.get("autoScalingGroups") or [] if g.get("name")}
    groups |= {name for name in targets.get("autoScalingGroups") or [] if name}
    balancing = group.get("loadBalancerInfo") or {}
    target_groups = {t["name"] for t in balancing.get("targetGroupInfoList") or [] if t.get("name")}
    for pair in balancing.get("targetGroupPairInfoList") or []:
        target_groups |= {t["name"] for t in pair.get("targetGroups") or [] if t.get("name")}
    selectors = _tag_selectors(group, targets)

    def deployed_to(resource: AwsResource) -> bool:
        detail = resource.aws_detail or {}
        if resource.kind is ResourceKind.AUTO_SCALING_GROUP:
            return resource.aws_id in groups or bool(_target_group_names(detail) & target_groups)
        if resource.kind is ResourceKind.SERVER:
            tags = detail.get("tags") or {}
            return detail.get("auto_scaling_group") in groups or any(
                all(any(_filter_matches(f, tags) for f in lists) for lists in selector)
                for selector in selectors
            )
        return bool(_target_group_names(detail) & target_groups)

    hit = {r.id for r in resources if deployed_to(r)}
    # A group deployed to takes its servers down with it, and the load
    # balancers it is registered with see its targets drain.
    matched_groups = [r for r in resources if r.id in hit and r.kind is ResourceKind.AUTO_SCALING_GROUP]
    names = {r.aws_id for r in matched_groups}
    their_target_groups = set().union(*(_target_group_names(r.aws_detail) for r in matched_groups))
    for resource in resources:
        if resource.kind is ResourceKind.SERVER and (resource.aws_detail or {}).get("auto_scaling_group") in names:
            hit.add(resource.id)
        if resource.kind is ResourceKind.LOAD_BALANCER and _target_group_names(resource.aws_detail) & their_target_groups:
            hit.add(resource.id)
    return [r for r in resources if r.id in hit]


def deployment_environment(resources: list[AwsResource], account: AwsAccount) -> WebsiteEnvironment | None:
    """Its resources' environment when they agree on one, else its account's."""
    found = {r.environment for r in resources if r.environment is not None}
    if len(found) == 1:
        return found.pop()
    return account.environment


class DeploymentWatcher:
    """Follows the CodeDeploy deployments of the accounts that watch them."""

    def __init__(self, session: AsyncSession, notifier: Notifier | None = None) -> None:
        self.session = session
        self.notifier = notifier or Notifier(session)

    @staticmethod
    def _lease_end(seen_at: datetime, now: datetime) -> datetime:
        return min(
            now + timedelta(minutes=settings.DEPLOY_SILENCE_LEASE_MINUTES),
            seen_at + timedelta(minutes=settings.DEPLOY_SILENCE_MAX_MINUTES),
        )

    # -- entry point ---------------------------------------------------------

    async def run_due(self, now: datetime | None = None) -> int:
        """Ask CodeDeploy about every account whose look is due. Called by
        the scheduler. Returns how many accounts were asked."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(seconds=settings.DEPLOY_WATCH_INTERVAL_SECONDS)
        accounts = list(
            await self.session.scalars(
                select(AwsAccount)
                .where(
                    AwsAccount.watch_deployments.is_(True),
                    (AwsAccount.deployments_checked_at.is_(None)) | (AwsAccount.deployments_checked_at <= cutoff),
                )
                .order_by(AwsAccount.id)
            )
        )
        for account in accounts:
            try:
                await self.watch_account(account)
            except Exception:
                logger.exception("Watching deployments of %s failed; continuing.", account.name)
                await self.session.rollback()
        return len(accounts)

    async def watch_account(self, account: AwsAccount) -> None:
        credentials = client.Account.of(account)
        errors = []
        for name in await self._regions(account, credentials):
            try:
                await self._watch_region(account, client.Region(credentials, name))
            except AwsError as exc:
                errors.append(f"{name}: {exc}")
        error = "; ".join(errors)[:2000] or None
        if error and error != account.deployments_error:
            logger.warning("CodeDeploy could not be asked for %s: %s", account.name, error)
        elif not error and account.deployments_error:
            logger.info("CodeDeploy answers again for %s.", account.name)
        account.deployments_error = error
        account.deployments_checked_at = datetime.now(UTC)
        await self.session.commit()

    async def _regions(self, account: AwsAccount, credentials: client.Account) -> list[str]:
        """Where its VPCs are, its default region, and wherever a deployment
        it follows is; with none of those, Watchly's own region."""
        names = set(
            await self.session.scalars(select(AwsVpc.region).where(AwsVpc.account_id == account.id).distinct())
        )
        names |= set(
            await self.session.scalars(
                select(AwsDeployment.region)
                .where(AwsDeployment.account_id == account.id, AwsDeployment.finished_at.is_(None))
                .distinct()
            )
        )
        if account.default_region:
            names.add(account.default_region)
        if not names:
            own = await client.default_region(credentials)
            if own:
                names.add(own)
        return sorted(names)

    # -- one region ----------------------------------------------------------

    async def _describe(self, region: client.Region, ids: list[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for start in range(0, len(ids), BATCH):
            response = await client.call(
                "codedeploy", "batch_get_deployments", region, deploymentIds=ids[start : start + BATCH]
            )
            found.update({d["deploymentId"]: d for d in response.get("deploymentsInfo", []) if "deploymentId" in d})
        return found

    async def _watch_region(self, account: AwsAccount, region: client.Region) -> None:
        """Every AWS call that may fail the region is made before anything
        is written."""
        active = set(
            await client.paginate(
                "codedeploy", "list_deployments", region, "deployments",
                includeOnlyStatuses=list(ACTIVE_STATUSES),
            )
        )
        rows = await self.session.scalars(
            select(AwsDeployment).where(
                AwsDeployment.account_id == account.id,
                AwsDeployment.region == region.name,
                (AwsDeployment.finished_at.is_(None)) | (AwsDeployment.deployment_id.in_(active)),
            )
        )
        known = {row.deployment_id: row for row in rows}
        new = sorted(
            i for i in active if i not in known and (account.id, region.name, i) not in _ignored
        )
        ended = [row for row in known.values() if row.is_active and row.deployment_id not in active]
        running = [row for row in known.values() if row.is_active and row.deployment_id in active]
        infos = await self._describe(region, new + [row.deployment_id for row in ended])

        for deployment_id in new:
            info = infos.get(deployment_id)
            if info is None:
                continue
            if info.get("creator") in SCALING_CREATORS:
                if len(_ignored) > 10_000:
                    _ignored.clear()
                _ignored.add((account.id, region.name, deployment_id))
                continue
            await self.start(account, region, info)
        for row in ended:
            await self.finish(row, infos.get(row.deployment_id))
        if running:
            await self._renew(running)

    async def _candidates(self, account: AwsAccount, region: str) -> list[AwsResource]:
        rows = await self.session.scalars(
            select(AwsResource)
            .join(AwsVpc, AwsVpc.id == AwsResource.vpc_id)
            .where(
                AwsVpc.account_id == account.id,
                AwsVpc.region == region,
                AwsResource.kind.in_(SILENCED_KINDS),
            )
            .order_by(AwsResource.id)
        )
        return list(rows)

    # -- start, renew, finish ------------------------------------------------

    async def start(self, account: AwsAccount, region: client.Region, info: dict) -> AwsDeployment:
        """Record a deployment, silence its resources, and announce it."""
        now = datetime.now(UTC)
        application = info.get("applicationName") or "?"
        group_name = info.get("deploymentGroupName") or "?"
        group, fallback = {}, None
        try:
            response = await client.call(
                "codedeploy", "get_deployment_group", region,
                applicationName=application, deploymentGroupName=group_name,
            )
            group = response.get("deploymentGroupInfo") or {}
        except AwsError as exc:
            fallback = f"Watchly could not read deployment group {group_name} ({exc.code})."
        candidates = await self._candidates(account, region.name)
        targets = candidates if fallback else match(candidates, group, info)

        row = AwsDeployment(
            account_id=account.id,
            region=region.name,
            deployment_id=info["deploymentId"],
            application_name=application[:255],
            group_name=group_name[:255],
            status=info.get("status") or "InProgress",
            environment=deployment_environment(targets, account),
            creator=(info.get("creator") or None),
            description=(info.get("description") or "").strip() or None,
            revision=revision_text(info.get("revision")),
            resources=[{"id": r.id, "name": r.name, "kind": r.kind.value} for r in targets],
            fallback=fallback,
            started_at=info.get("createTime") or now,
            created_at=now,
        )
        row.account = account
        self.session.add(row)
        await self.session.flush()
        until = self._lease_end(now, now)
        for resource in targets:
            self.session.add(
                AwsMaintenanceWindow(
                    resource_id=resource.id,
                    starts_at=now,
                    ends_at=until,
                    reason=f"Deploying {application} ({row.deployment_id})"[:255],
                    deployment_id=row.id,
                )
            )
        event = self._event(NotificationKind.DEPLOY_STARTED, row)
        self.session.add(self._feed(event, now, f"{event.target}: alerts paused for {len(targets)} resource(s)"))
        # Commit before sending, so a slow channel can never make the same
        # announcement go out twice.
        await self.session.commit()
        logger.info(
            "Deployment %s of %s/%s started in %s; %d resource(s) silenced.",
            row.deployment_id, application, group_name, account.name, len(targets),
        )
        await self.notifier.dispatch(event)
        if event.slack is not None and event.slack_ts:
            row.slack_thread_ts = event.slack_ts
            row.slack_thread_channel = event.slack.channel_id
        if event.telegram is not None and event.telegram_message_id:
            row.telegram_thread_message_id = event.telegram_message_id
            row.telegram_thread_chat = event.telegram.chat_id
        await self.session.commit()
        return row

    async def _renew(self, rows: list[AwsDeployment]) -> None:
        """Push the windows of running deployments a lease ahead. One that
        has ended (by hand, or as its lease ran out) stays ended."""
        now = datetime.now(UTC)
        for row in rows:
            until = self._lease_end(row.created_at, now)
            if until <= now:
                continue
            await self.session.execute(
                update(AwsMaintenanceWindow)
                .where(
                    AwsMaintenanceWindow.deployment_id == row.id,
                    AwsMaintenanceWindow.ends_at > now,
                    AwsMaintenanceWindow.ends_at < until,
                )
                .values(ends_at=until)
            )
        await self.session.commit()

    async def finish(self, row: AwsDeployment, info: dict | None) -> None:
        """It ended: let its resources be checked again after the settle
        time, and say how it went."""
        now = datetime.now(UTC)
        info = info or {}
        status = info.get("status") or "Unknown"
        if status in ACTIVE_STATUSES:
            # Listed as running a moment ago, and described as running now.
            return
        row.status = status
        row.finished_at = info.get("completeTime") or now
        row.error = ((info.get("errorInformation") or {}).get("message") or None) if status != "Succeeded" else None
        await end_windows(self.session, row, now + timedelta(seconds=settings.DEPLOY_SETTLE_SECONDS))
        event = self._event(NotificationKind.DEPLOY_FINISHED, row)
        self.session.add(
            self._feed(event, now, f"{event.target} {event.outcome} after {format_duration(event.duration_seconds)}")
        )
        await self.session.commit()
        logger.info("Deployment %s of %s ended: %s.", row.deployment_id, row.application_name, status)
        await self.notifier.dispatch(event)

    # -- the messages --------------------------------------------------------

    def _event(self, kind: NotificationKind, row: AwsDeployment) -> DeployEvent:
        account = row.account
        project = account.project
        slack, telegram = slack_target(project), telegram_target(project)
        if kind is NotificationKind.DEPLOY_FINISHED:
            # The end replies to the start, and shows in the channel too.
            if slack is not None and row.slack_thread_ts and row.slack_thread_channel == slack.channel_id:
                slack = replace(slack, thread_ts=row.slack_thread_ts, broadcast=True)
            if telegram is not None and row.telegram_thread_message_id and row.telegram_thread_chat == telegram.chat_id:
                telegram = replace(telegram, reply_to=row.telegram_thread_message_id)
        return DeployEvent(
            kind=kind,
            project_id=project.id,
            project_name=project.name,
            account_name=account.name,
            aws_account_id=account.aws_account_id,
            region=row.region,
            deployment_id=row.deployment_id,
            application=row.application_name,
            group=row.group_name,
            environment=row.environment.value if row.environment else None,
            started_at=row.started_at,
            creator=row.creator,
            description=row.description,
            revision=row.revision,
            resources=tuple(
                DeployedResource(item["id"], item["name"], ResourceKind(item["kind"])) for item in row.resources
            ),
            fallback=row.fallback,
            status=row.status if kind is NotificationKind.DEPLOY_FINISHED else None,
            finished_at=row.finished_at,
            error=row.error,
            max_minutes=settings.DEPLOY_SILENCE_MAX_MINUTES,
            settle_seconds=settings.DEPLOY_SETTLE_SECONDS,
            recipients=project_recipients(project),
            slack=slack,
            telegram=telegram,
            whatsapp=whatsapp_target(project),
        )

    @staticmethod
    def _feed(event: DeployEvent, at: datetime, summary: str) -> AwsEvent:
        return AwsEvent(
            project_id=event.project_id,
            vpc_id=None,
            resource_id=None,
            kind=event.kind.value,
            occurred_at=at,
            summary=summary[:2000],
        )


async def end_windows(session: AsyncSession, row: AwsDeployment, at: datetime) -> None:
    """Make the deployment's windows still in effect end at `at`, sooner or
    later than they would. One already ended, by hand or by its lease, stays
    ended."""
    now = datetime.now(UTC)
    windows = await session.scalars(
        select(AwsMaintenanceWindow).where(
            AwsMaintenanceWindow.deployment_id == row.id, AwsMaintenanceWindow.ends_at > now
        )
    )
    for window in windows:
        if at <= window.starts_at:
            await session.delete(window)
        else:
            window.ends_at = at


async def stop_watching(session: AsyncSession, account: AwsAccount) -> None:
    """The account no longer watches deployments: what it was following ends
    now, quietly, and its resources are checked again. Not committed."""
    now = datetime.now(UTC)
    rows = await session.scalars(
        select(AwsDeployment).where(AwsDeployment.account_id == account.id, AwsDeployment.finished_at.is_(None))
    )
    for row in rows:
        await end_windows(session, row, now)
        row.status = UNWATCHED
        row.finished_at = now
    account.deployments_error = None
    account.deployments_checked_at = None
