"""What can be customized about each notification kind, and the defaults.

The single source of truth for the API and the UI: which kinds exist, what a
template may reference, and the wording used when nobody has changed it. The
default subjects deliberately match what Watchly sent before templates existed.
"""

from dataclasses import dataclass

from app.monitoring.alerts.base import NotificationKind

_SITE = {
    "project": "Project name",
    "website": "Website name",
    "url": "Website URL, the host of a ping check, the domain of a DNS check, or the "
    "host:port of a database check",
    "status_code": "HTTP status of the latest check, “no response”, or “—” for a ping, "
    "DNS or database check",
    "response_time": "Response time of the latest check, e.g. “212 ms”; for a ping check "
    "the average round trip, for a DNS check the resolvers’ average answer time, for a "
    "database check the time to the server’s answer",
    "checked_at": "When the latest check ran (UTC)",
    "summary": "One-line result of the latest check, e.g. “HTTP 503 Service Unavailable”",
    "dashboard_url": "Link to this website in Watchly (empty if ALERT_DASHBOARD_URL is unset)",
}

_OUTAGE = {
    **_SITE,
    "status": "UP or DOWN",
    "error": "Error text from the latest check, or “—”",
    "downtime": "How long the site has been down, e.g. “1h 2m”",
    "down_since": "When the outage began (UTC)",
    "attempt": "Which alert this is within the outage",
    "max_attempts": "How many alerts an outage may send",
}

_SSL = {
    **_SITE,
    "expires_at": "When the certificate ends (UTC)",
    "expiry": "“expires in 5 days”, “expires in less than a day” or “expired 2 days ago”",
    "days_left": "Whole days remaining (0 when expired or under a day)",
    "issuer": "Who issued the certificate, or “—”",
}

_DOMAIN = {
    **_SITE,
    "domain": "The registered domain, e.g. “acme.example” for “https://www.acme.example/”",
    "expires_at": "When the registration ends (UTC)",
    "expiry": "“expires in 13 days”, “expires in less than a day” or “expired 2 days ago”",
    "days_left": "Whole days remaining (0 when expired or under a day)",
    "registrar": "Who the domain is registered with, or “—”",
}

_NAMESERVERS = {
    **_SITE,
    "domain": "The registered domain, e.g. “acme.example” for “https://www.acme.example/”",
    "nameservers": "The nameservers the domain is delegated to now",
    "previous_nameservers": "The ones it was delegated to before",
    "added": "Nameservers that are new, or “—”",
    "removed": "Nameservers that are gone, or “—”",
    "registrar": "Who the domain is registered with, or “—”",
}

_SLOW = {
    **_SITE,
    "threshold": "The slow-response threshold, e.g. “3000 ms”",
    "slow_checks": "Consecutive slow checks that triggered this alert",
    "slowest_step": "Which step of the latest check took longest, e.g. “Waiting for "
    "first byte (3920 ms, 93%)”, or “—” if it was not timed (always for a ping check)",
}

_LOSS = {
    **_SITE,
    "packet_loss": "Share of pings the latest check lost, e.g. “40%”",
    "threshold": "The packet-loss threshold, e.g. “20%”",
    "lossy_checks": "Consecutive lossy checks that triggered this alert",
}

_DNS_CHANGE = {
    **_SITE,
    "record_type": "The record watched: A, AAAA, CNAME, MX or TXT",
    "records": "The records every resolver returns now, e.g. “198.51.100.7”",
    "previous_records": "The records they returned before",
    "added": "Records that are new, or “—”",
    "removed": "Records that are gone, or “—”",
}

_REPORT = {
    "project": "Project name",
    "month": "The month covered, e.g. “August 2026”",
    "average_uptime": "Average uptime across the project’s websites, e.g. “99.95%”",
    "total_downtime": "Downtime summed across websites, or “none”",
    "incidents": "“no incidents”, “1 incident” or “3 incidents”",
    "sites": "“1 site” or “4 sites”",
    "dashboard_url": "Link to this project in Watchly (empty if ALERT_DASHBOARD_URL is unset)",
}


_INFRA = {
    "project": "Project name",
    "resource_name": "The resource's name, e.g. “orders-api-1”",
    "resource_kind": "server, load balancer or Auto Scaling group",
    "aws_id": "Instance id, load balancer ARN or Auto Scaling group name",
    "vpc_name": "The VPC it is in, as registered in Watchly",
    "aws_state": "What AWS last said about it, e.g. “running”, “stopped”, “active”, “2 of 3 in service”, or “—”",
    "address": "Its private IP or DNS name, or “—” (an Auto Scaling group has none)",
    "checked_at": "When the latest check ran (UTC)",
    "dashboard_url": "Link to this resource in Watchly (empty if ALERT_DASHBOARD_URL is unset)",
}

_INFRA_OUTAGE = {
    **_INFRA,
    "failing_checks": "Each failing check and why, e.g. “ping (ping): No reply to 3 pings; "
    "tcp 22 (tcp): TCP 22: No answer…”; “none” on recovery",
    "downtime": "How long it has been down, e.g. “1h 2m”",
    "down_since": "When the outage began (UTC)",
    "attempt": "Which alert this is within the outage",
    "max_attempts": "How many alerts an outage may send",
}

_INFRA_DEGRADED = {
    **_INFRA,
    "problem": "What is wrong, e.g. “Unhealthy targets” or “Slow response”",
    "problem_detail": "The figures, e.g. “orders-api-tg: 2 of 3 targets healthy; i-0a1b… failing with Target.Timeout”",
    "check": "The check that found it, e.g. “targets of orders-api-tg (target_health)”",
}

_INFRA_SCALING = {
    **_INFRA,
    "change": "“added” or “removed”",
    "instance_count": "How many instances joined or left",
    "instances": "Each instance with its zone and address; for a scale-out with a health "
    "endpoint set, whether it answered",
    "in_service": "Instances in service now",
    "desired": "The group's desired capacity",
    "health_endpoint": "The health endpoint probed on new instances, or “not set”",
}

_VPC = {
    "project": "Project name",
    "vpc_name": "The VPC, as registered in Watchly",
    "cidrs": "Its address ranges, e.g. “10.20.0.0/16”",
    "dashboard_url": "Link to this VPC in Watchly (empty if ALERT_DASHBOARD_URL is unset)",
}

_VPC_UNREACHABLE = {
    **_VPC,
    "failed_checks": "Checks in the VPC that failed to connect",
    "total_checks": "Checks in the VPC that were looked at",
}

_VPC_RECOVERED = {**_VPC, "downtime": "How long it was unreachable, e.g. “12m”"}

_DEPLOY = {
    "project": "Project name",
    "environment": "Where it deploys to, e.g. “production” or “development”: its resources' "
    "environment, else its AWS account's, else “—”",
    "application": "The CodeDeploy application, e.g. “orders-api”",
    "deployment_group": "The CodeDeploy deployment group, e.g. “orders-api-prod”",
    "deployment_id": "CodeDeploy's id, e.g. “d-7Q2X9ABCD”",
    "account": "The AWS account, as named in Watchly",
    "region": "The AWS region, e.g. “ap-southeast-1”",
    "revision": "What is deployed, e.g. “github acme/orders-api@4f1c2d9” or “s3://bucket/key”, "
    "or “—”",
    "initiated_by": "Who started it, as CodeDeploy says: “user”, “CloudFormation”, "
    "“codeDeployRollback”…, or “—”",
    "resources": "The monitored resources it pauses, e.g. “orders-api-asg (Auto Scaling "
    "group); orders-api-alb (load balancer)”, or “none”",
    "resource_count": "How many monitored resources it pauses",
    "dashboard_url": "Link to the project in Watchly (empty if ALERT_DASHBOARD_URL is unset)",
}

_DEPLOY_FINISHED = {
    **_DEPLOY,
    "status": "“succeeded”, “failed” or “was stopped”",
    "duration": "How long it took, e.g. “4m”",
    "error": "CodeDeploy's error message when it failed, or “—”",
}

_CAPACITY = {
    "project": "Project name",
    "account": "The AWS account, as named in Watchly",
    "aws_account_id": "Its 12-digit id, or “—”",
    "region": "The AWS region, e.g. “ap-southeast-1”",
    "problem": "“Elastic IPs attached to nothing” or “Elastic IP quota nearly used”",
    "problem_detail": "The figures, e.g. “2 Elastic IPs attached to nothing: 203.0.113.10 (old-bastion), "
    "203.0.113.11; about US$7 a month” or “4 of 5 Elastic IPs used, 80% (alert at 80%)”",
    "dashboard_url": "Link to the project in Watchly (empty if ALERT_DASHBOARD_URL is unset)",
}

_DOCKER_CONTAINER = {
    "project": "Project name",
    "host": "The Docker host, as named in Watchly",
    "container": "The container's name",
    "image": "Its image, e.g. “ghcr.io/acme/api:1.4.2”",
    "state": "Docker's state for it: running, exited, restarting...",
    "health": "Its healthcheck: healthy, unhealthy, starting, or none",
    "exit_code": "Its last exit code, or “—”",
    "restart_count": "How many times Docker's restart policy restarted it",
    "detail": "What happened, e.g. “exited with code 137 (killed)” or “memory at 94% of its "
    "512 MiB limit for 3 heartbeats”",
    "downtime": "How long it has been (or was) down, e.g. “4m”, or “—”",
    "occurred_at": "When Watchly noticed (UTC)",
    "dashboard_url": "Link to the container in Watchly (empty if ALERT_DASHBOARD_URL is unset)",
}

_DOCKER_HOST = {
    "project": "Project name",
    "host": "The Docker host, as named in Watchly",
    "hostname": "The machine's hostname, as Docker reports it, or “—”",
    "reason": "“the agent stopped reporting” or “Docker is not answering the agent: …”",
    "last_seen": "When the agent last reported (UTC), or “never”",
    "downtime": "How long it was offline, e.g. “21m”, or “—”",
    "occurred_at": "When Watchly noticed (UTC)",
    "dashboard_url": "Link to the host in Watchly (empty if ALERT_DASHBOARD_URL is unset)",
}

_DOCKER_AUDIENCE = (
    "The project's members and extra emails, ALERT_DEFAULT_EMAILS, and the project's Slack "
    "channel, Telegram chat and WhatsApp numbers. A muted container alerts nobody."
)

_INFRA_AUDIENCE = (
    "The project's members and extra emails, the resource's extra recipients, and the "
    "project's Slack channel, Telegram chat and WhatsApp numbers."
)


@dataclass(frozen=True, slots=True)
class KindInfo:
    kind: NotificationKind
    label: str
    description: str
    #: Who receives it, in words.
    audience: str
    default_subject: str
    default_body: str
    #: `{{name}}` -> what it becomes. Exactly the keys the event's `context()`
    #: returns; `samples.py` and a check in the API keep the two in step.
    placeholders: dict[str, str]


CATALOG: dict[NotificationKind, KindInfo] = {
    info.kind: info
    for info in (
        KindInfo(
            kind=NotificationKind.DOWN,
            label="Site down",
            description="The first failed check of an outage.",
            audience="The website’s recipients (project members and extra emails, site "
            "recipients), and the Slack channel, Telegram chat and WhatsApp numbers of "
            "the site or its project.",
            default_subject="[DOWN] {{project}} / {{website}} is not responding",
            default_body="{{website}} did not respond as expected: {{summary}}",
            placeholders=_OUTAGE,
        ),
        KindInfo(
            kind=NotificationKind.STILL_DOWN,
            label="Still down",
            description="Follow-up reminders while a site stays down, up to the "
            "website’s alert limit.",
            audience="Same as “Site down”.",
            default_subject="[STILL DOWN] {{project}} / {{website}} has been down for {{downtime}}",
            default_body="{{website}} is still not responding after {{downtime}}: {{summary}}",
            placeholders=_OUTAGE,
        ),
        KindInfo(
            kind=NotificationKind.RECOVERED,
            label="Back up",
            description="The site answered again after an outage.",
            audience="Same as “Site down”.",
            default_subject="[RECOVERED] {{project}} / {{website}} is back up after {{downtime}}",
            default_body="{{website}} is responding again after {{downtime}} of downtime.",
            placeholders=_OUTAGE,
        ),
        KindInfo(
            kind=NotificationKind.SSL_EXPIRING,
            label="SSL certificate expiring",
            description="An HTTPS site’s certificate is close to its end date, at 14 and 7 "
            "days (configurable) and once more if it expires.",
            audience="Same as “Site down”.",
            default_subject="[SSL] {{project}} / {{website}}: certificate {{expiry}}",
            default_body=(
                "The SSL certificate for {{website}} {{expiry}} ({{expires_at}}). "
                "Renew it promptly to avoid browser warnings and failed connections."
            ),
            placeholders=_SSL,
        ),
        KindInfo(
            kind=NotificationKind.DOMAIN_EXPIRING,
            label="Domain expiring",
            description="A site’s domain registration is close to its end date, at 14 and 7 "
            "days (configurable) and once more if it lapses. Sent once per project "
            "for each domain, however many of its sites use it.",
            audience="Same as “Site down”, for the site that found it.",
            default_subject="[DOMAIN] {{project}} / {{domain}}: registration {{expiry}}",
            default_body=(
                "The domain {{domain}}, used by {{website}}, {{expiry}} ({{expires_at}}). "
                "Renew it before its websites and email stop working (registrar: "
                "{{registrar}})."
            ),
            placeholders=_DOMAIN,
        ),
        KindInfo(
            kind=NotificationKind.NAMESERVERS_CHANGED,
            label="Nameservers changed",
            description="The registry now delegates a site’s domain to other nameservers: "
            "a move between DNS providers, or a sign the domain was hijacked. Sent once "
            "per project for each change, however many of its sites use the domain.",
            audience="Same as “Site down”, for the site that found it.",
            default_subject="[NAMESERVERS] {{project}} / {{domain}}: nameservers changed",
            default_body=(
                "The nameservers for {{domain}}, used by {{website}}, are now "
                "{{nameservers}} (were {{previous_nameservers}}). If you did not make this "
                "change, lock the domain at your registrar ({{registrar}}) at once."
            ),
            placeholders=_NAMESERVERS,
        ),
        KindInfo(
            kind=NotificationKind.SLOW_RESPONSE,
            label="Slow response",
            description="The site is up but has answered slower than its threshold for "
            "several checks in a row.",
            audience="Same as “Site down”.",
            default_subject="[SLOW] {{project}} / {{website}} is responding slowly ({{response_time}})",
            default_body=(
                "{{website}} has taken longer than {{threshold}} to respond for "
                "{{slow_checks}} checks in a row; the latest took {{response_time}}. "
                "Slowest step: {{slowest_step}}. It is still up."
            ),
            placeholders=_SLOW,
        ),
        KindInfo(
            kind=NotificationKind.PACKET_LOSS,
            label="Packet loss",
            description="A pinged host still answers, but has lost at least its threshold "
            "of pings for several checks in a row.",
            audience="Same as “Site down”.",
            default_subject="[PACKET LOSS] {{project}} / {{website}} is losing {{packet_loss}} of pings",
            default_body=(
                "{{website}} has lost at least {{threshold}} of its pings for "
                "{{lossy_checks}} checks in a row; the latest lost {{packet_loss}}, and "
                "the replies that came back took {{response_time}} on average. It is "
                "still up."
            ),
            placeholders=_LOSS,
        ),
        KindInfo(
            kind=NotificationKind.DNS_CHANGED,
            label="DNS records changed",
            description="A DNS check with no expected values: every resolver now returns "
            "different records than before. (With expected values, a different answer "
            "is an outage instead.)",
            audience="Same as “Site down”.",
            default_subject="[DNS CHANGED] {{project}} / {{website}}: {{record_type}} records "
            "for {{url}} changed",
            default_body=(
                "The {{record_type}} records for {{url}} are now {{records}} (were "
                "{{previous_records}}). If you did not make this change, check your DNS "
                "provider and registrar accounts."
            ),
            placeholders=_DNS_CHANGE,
        ),
        KindInfo(
            kind=NotificationKind.MONTHLY_REPORT,
            label="Monthly uptime report",
            description="How every site in the project did last month: uptime, downtime, "
            "incidents and response time.",
            audience="The project’s members and extra emails, ALERT_DEFAULT_EMAILS, and "
            "the project’s Slack channel, Telegram chat and WhatsApp numbers.",
            default_subject="{{project}}: {{month}} uptime report ({{average_uptime}})",
            default_body=(
                "Here is how {{sites}} in {{project}} performed in {{month}}: "
                "{{average_uptime}} average uptime with {{incidents}}."
            ),
            placeholders=_REPORT,
        ),
        KindInfo(
            kind=NotificationKind.INFRA_DOWN,
            label="Resource down",
            description="The first of an AWS server's, load balancer's or Auto Scaling "
            "group's checks failed: one alert per resource, listing every failing check.",
            audience=_INFRA_AUDIENCE,
            default_subject="[DOWN] {{project}} / {{resource_name}} ({{resource_kind}}) is down",
            default_body="{{resource_name}} in {{vpc_name}} is down: {{failing_checks}}",
            placeholders=_INFRA_OUTAGE,
        ),
        KindInfo(
            kind=NotificationKind.INFRA_STILL_DOWN,
            label="Resource still down",
            description="Follow-up reminders while a resource stays down, up to its alert limit.",
            audience="Same as “Resource down”.",
            default_subject="[STILL DOWN] {{project}} / {{resource_name}} has been down for "
            "{{downtime}}",
            default_body="{{resource_name}} is still down after {{downtime}}: {{failing_checks}}",
            placeholders=_INFRA_OUTAGE,
        ),
        KindInfo(
            kind=NotificationKind.INFRA_RECOVERED,
            label="Resource back up",
            description="Every check of a resource passes again after an outage.",
            audience="Same as “Resource down”.",
            default_subject="[RECOVERED] {{project}} / {{resource_name}} is back up after "
            "{{downtime}}",
            default_body="{{resource_name}} is healthy again after {{downtime}} of downtime.",
            placeholders=_INFRA_OUTAGE,
        ),
        KindInfo(
            kind=NotificationKind.INFRA_DEGRADED,
            label="Resource degraded",
            description="A resource is up, but a problem has lasted several checks: "
            "unhealthy load balancer targets, an Auto Scaling group's instances failing "
            "their health check or short of its desired capacity, slow responses or "
            "packet loss.",
            audience="Same as “Resource down”.",
            default_subject="[DEGRADED] {{project}} / {{resource_name}}: {{problem}}",
            default_body="{{resource_name}} is up but degraded: {{problem_detail}}. Found by "
            "{{check}}.",
            placeholders=_INFRA_DEGRADED,
        ),
        KindInfo(
            kind=NotificationKind.ASG_SCALED_OUT,
            label="Servers added (scale out)",
            description="Instances joined an Auto Scaling group. Opt-in: only for groups "
            "whose “notify on scale-out” is on. If the group has a health endpoint, the "
            "message says whether each new server answered it.",
            audience=_INFRA_AUDIENCE,
            default_subject="[SCALED OUT] {{project}} / {{resource_name}} added {{instance_count}} "
            "instance(s)",
            default_body="{{resource_name}} in {{vpc_name}} added {{instance_count}} instance(s): "
            "{{instances}}. {{in_service}} in service, {{desired}} desired.",
            placeholders=_INFRA_SCALING,
        ),
        KindInfo(
            kind=NotificationKind.ASG_SCALED_IN,
            label="Servers removed (scale in)",
            description="Instances left an Auto Scaling group. Opt-in: only for groups whose "
            "“notify on scale-in” is on.",
            audience="Same as “Servers added (scale out)”.",
            default_subject="[SCALED IN] {{project}} / {{resource_name}} removed {{instance_count}} "
            "instance(s)",
            default_body="{{resource_name}} in {{vpc_name}} removed {{instance_count}} instance(s): "
            "{{instances}}. {{in_service}} in service, {{desired}} desired.",
            placeholders=_INFRA_SCALING,
        ),
        KindInfo(
            kind=NotificationKind.VPC_UNREACHABLE,
            label="VPC unreachable",
            description="Most checks in a VPC failed to connect at once: one alert for the "
            "VPC, and each resource's own down alert is held.",
            audience="The members and extra emails, Slack channel, Telegram chat and WhatsApp "
            "numbers of each project with resources in the VPC.",
            default_subject="[VPC UNREACHABLE] {{project}} / {{vpc_name}} cannot be reached",
            default_body=(
                "Watchly cannot reach into {{vpc_name}} ({{cidrs}}): {{failed_checks}} of "
                "{{total_checks}} checks failed to connect. Look for a security group, NACL, "
                "route or peering change."
            ),
            placeholders=_VPC_UNREACHABLE,
        ),
        KindInfo(
            kind=NotificationKind.VPC_RECOVERED,
            label="VPC reachable again",
            description="An unreachable VPC answers again.",
            audience="Same as “VPC unreachable”.",
            default_subject="[VPC RECOVERED] {{project}} / {{vpc_name}} answers again",
            default_body="{{vpc_name}} can be reached again after {{downtime}}.",
            placeholders=_VPC_RECOVERED,
        ),
        KindInfo(
            kind=NotificationKind.DEPLOY_STARTED,
            label="Deployment started",
            description="A CodeDeploy deployment started in an AWS account that watches them. "
            "Down alerts for the servers, load balancers and Auto Scaling groups it deploys "
            "to pause until it ends.",
            audience="The project's members and extra emails, ALERT_DEFAULT_EMAILS, and the "
            "project's Slack channel, Telegram chat and WhatsApp numbers.",
            default_subject="[DEPLOYING] {{project}} / {{application}} to {{environment}}",
            default_body=(
                "Deploying {{application}} ({{deployment_group}}) to {{environment}}: "
                "{{revision}}. Down alerts paused for {{resource_count}} resource(s) until it ends."
            ),
            placeholders=_DEPLOY,
        ),
        KindInfo(
            kind=NotificationKind.DEPLOY_FINISHED,
            label="Deployment finished",
            description="That deployment succeeded, failed or was stopped. Its resources' "
            "checks resume shortly after; any still down then alerts as usual.",
            audience="Same as “Deployment started”.",
            default_subject="[DEPLOY] {{project}} / {{application}} to {{environment}} {{status}}",
            default_body=(
                "Deploying {{application}} ({{deployment_group}}) to {{environment}} {{status}} "
                "after {{duration}}."
            ),
            placeholders=_DEPLOY_FINISHED,
        ),
        KindInfo(
            kind=NotificationKind.ACCOUNT_CAPACITY,
            label="AWS account capacity",
            description="An AWS account that watches its capacity holds Elastic IPs attached to "
            "nothing, which AWS bills by the hour, or has nearly used its Elastic IP quota in a "
            "region.",
            audience="Same as “Deployment started”.",
            default_subject="[CAPACITY] {{project}} / {{account}} in {{region}}: {{problem}}",
            default_body="{{account}} in {{region}}: {{problem_detail}}.",
            placeholders=_CAPACITY,
        ),
        KindInfo(
            kind=NotificationKind.DOCKER_CONTAINER_DOWN,
            label="Container down",
            description="A container that was running stopped and stayed stopped past the grace "
            "period (DOCKER_DOWN_GRACE_SECONDS), so a redeploy stays quiet.",
            audience=_DOCKER_AUDIENCE,
            default_subject="[DOWN] {{project}} / {{host}}: {{container}} is down",
            default_body="{{container}} on {{host}} is down: {{detail}}.",
            placeholders=_DOCKER_CONTAINER,
        ),
        KindInfo(
            kind=NotificationKind.DOCKER_CONTAINER_RECOVERED,
            label="Container back up",
            description="A container that was down runs again.",
            audience="Same as “Container down”.",
            default_subject="[RECOVERED] {{project}} / {{host}}: {{container}} is running again",
            default_body="{{container}} on {{host}} is running again after {{downtime}}.",
            placeholders=_DOCKER_CONTAINER,
        ),
        KindInfo(
            kind=NotificationKind.DOCKER_CONTAINER_UNHEALTHY,
            label="Container unhealthy",
            description="A running container's own healthcheck reports it unhealthy.",
            audience="Same as “Container down”.",
            default_subject="[UNHEALTHY] {{project}} / {{host}}: {{container}}",
            default_body="{{container}} on {{host}} runs, but {{detail}}.",
            placeholders=_DOCKER_CONTAINER,
        ),
        KindInfo(
            kind=NotificationKind.DOCKER_CONTAINER_OOM,
            label="Container out of memory",
            description="The kernel killed a process in a container for lack of memory.",
            audience="Same as “Container down”.",
            default_subject="[OOM] {{project}} / {{host}}: {{container}} ran out of memory",
            default_body="{{container}} on {{host}} was {{detail}}.",
            placeholders=_DOCKER_CONTAINER,
        ),
        KindInfo(
            kind=NotificationKind.DOCKER_RESTART_LOOP,
            label="Container restart loop",
            description="A container keeps crashing and being restarted by its restart policy "
            "(DOCKER_RESTART_LOOP_COUNT restarts within DOCKER_RESTART_LOOP_WINDOW_SECONDS).",
            audience="Same as “Container down”.",
            default_subject="[RESTARTING] {{project}} / {{host}}: {{container}} keeps restarting",
            default_body="{{container}} on {{host}} {{detail}}; last exit code {{exit_code}}.",
            placeholders=_DOCKER_CONTAINER,
        ),
        KindInfo(
            kind=NotificationKind.DOCKER_RESOURCE_HIGH,
            label="Container running hot",
            description="A container stays over its CPU or memory threshold "
            "(DOCKER_CPU_ALERT_PERCENT, DOCKER_MEMORY_ALERT_PERCENT) for several heartbeats.",
            audience="Same as “Container down”.",
            default_subject="[HOT] {{project}} / {{host}}: {{container}}",
            default_body="{{container}} on {{host}}: {{detail}}.",
            placeholders=_DOCKER_CONTAINER,
        ),
        KindInfo(
            kind=NotificationKind.DOCKER_HOST_OFFLINE,
            label="Docker host offline",
            description="A host's agent stopped reporting (DOCKER_OFFLINE_AFTER_SECONDS), or "
            "reports that it cannot reach Docker. Its containers' alerts pause meanwhile.",
            audience=_DOCKER_AUDIENCE,
            default_subject="[OFFLINE] {{project}} / {{host}} stopped reporting",
            default_body="{{host}} is offline: {{reason}}. Last heard from {{last_seen}}.",
            placeholders=_DOCKER_HOST,
        ),
        KindInfo(
            kind=NotificationKind.DOCKER_HOST_RECOVERED,
            label="Docker host back",
            description="That host's agent reports again, with Docker reachable.",
            audience="Same as “Docker host offline”.",
            default_subject="[RECOVERED] {{project}} / {{host}} reports again",
            default_body="{{host}} reports again after {{downtime}} offline.",
            placeholders=_DOCKER_HOST,
        ),
    )
}

#: Kinds in the order the UI lists them.
KINDS: tuple[NotificationKind, ...] = tuple(CATALOG)

#: The one kind that may not be silenced on every channel: without it, a
#: project would go down without anyone being told.
PROTECTED_KIND = NotificationKind.DOWN
