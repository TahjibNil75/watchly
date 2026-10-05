# AWS infrastructure monitoring

The API, data model and the decisions behind them are in
[`doc/infraapi.md`](../../../../doc/infraapi.md). Everything here is off until
`INFRA_AWS_ENABLED=true`.

Watches AWS resources in a registered VPC:

| Resource | AWS | Checks |
| -------- | --- | ------ |
| Server | EC2 instance, in a public or a private subnet | `ping`, `tcp`, `http`: to its private IP, or with `use_public_ip` to its public IP |
| Load balancer | Application or Network Load Balancer, internal or internet-facing | `http` (e.g. its `/health`), `tcp` (one of its listeners' ports), `target_health` (its own health check's verdict per target) |
| Auto Scaling group | EC2 Auto Scaling group | `http`, `tcp`, `ping` on **every instance in service**, read from AWS at each run (e.g. the health API its target group calls); `group_health` (in service and healthy against desired capacity); `target_health` |
| Database | RDS DB instance, Aurora's included | none logs in, so no database credentials: `tcp` to its endpoint's port from inside the VPC; `db_status` (`DescribeDBInstances`); `db_metrics` (CloudWatch CPU, free storage and memory, connections, replica lag, against thresholds) |

A project monitors either websites or infrastructure (`Project.monitors`). An
infrastructure project owns one or more `AwsAccount`s, each holding how it is
reached: Watchly's own credentials (the instance role) or an access key,
stored encrypted, either optionally assuming a role in the account. A VPC
belongs to one account, and so to its project, and is registered when someone
first adds a resource from it. Every AWS call about a VPC, from discovery,
sync, probes or diagnose, goes through `client.Region.of(vpc)`, its account
and region. `client.py` keeps one boto3 session per account.
`credentials.py` holds the account input schema, which project creation
takes too.

This is a separate entity from [`websites`](../../websites/), which watches
public URLs, hosts and DNS records. It has its own tables (`aws_*`), its own
API (`/api/v1/monitoring/infra/aws`) and its own pages. A website check never
reaches a private address. An infrastructure probe reaches the addresses inside
its registered VPC, and public addresses only when it goes over the internet:
an internet-facing load balancer, or a server's or group's check with
`use_public_ip` (`models.is_public_path`). Those checks say nothing about reaching into the
VPC, so they never count towards marking it unreachable.

`app/monitoring/infra/` holds one sub-package per cloud provider. AWS is the
only one.

## Shared with the rest of monitoring

| From | What infra uses |
| ---- | --------------- |
| [`projects/`](../../projects/) | Projects, members, and permission scoping. A resource belongs to one project. |
| [`notifications/`](../../notifications/) | `Notifier`, the channels and per-kind settings. Infra adds its own kinds (`infra_down`, …) to `catalog.py`. |
| [`scheduler.py`](../../scheduler.py) | The tick and its advisory lock. `run_tick` also runs the infra checks, sync and rollup. |
| [`egress.py`](../../egress.py) | `vet()` and `vpc_scope()`, which every probe goes through before it connects. |
| [`websites/pinger.py`](../../websites/pinger.py), `checker.new_client()` | The ICMP and timed-HTTP code behind the `ping` and `http` checks. |

## Layout

```
infra/
├── __init__.py
└── aws/
    ├── __init__.py
    ├── README.md
    ├── models.py         AwsAccount, AwsVpc, AwsResource, AwsCheck, AwsCheckResult, AwsCheckHourly,
    │                     AwsEvent, AwsDeployment, AwsMaintenanceWindow
    ├── schemas.py
    ├── credentials.py    AccountAuth and the account input schema, shared with projects
    ├── routes.py         /monitoring/infra/aws/…, included from monitoring/routes.py
    ├── service.py        AWS accounts, VPCs, resources, checks: CRUD and permission scoping
    ├── monitor.py        due checks → results → resource outage, problems, VPC unreachable → alerts
    ├── events.py         Notification subclasses, sent through the existing Notifier
    ├── deployments.py    CodeDeploy deployments of accounts that watch them: announced, and the
    │                     resources they deploy to put in maintenance while they run
    ├── history.py        hourly rollup and purge of check results
    ├── client.py         the only module that imports boto3 (EC2, ELBv2, Auto Scaling, RDS,
    │                     CloudWatch, CodeDeploy, STS, IMDS); one session per account (`Account`, `Region`); every call
    │                     goes through anyio.to_thread
    ├── discovery.py      a VPC's instances, load balancers, Auto Scaling groups and RDS databases; suggested
    │                     checks; sync; a group's instances now (`read_group`)
    ├── diagnose.py       step-by-step dry run of a check; what an account's credentials may do
    ├── hints.py          where to look when a check cannot connect: the security group and rule to add,
    │                     or the ACL, route, host firewall or port to check (Admin and DevOps only)
    └── probes/           one module per check type; each returns a ProbeResult and never raises
        ├── ping.py
        ├── tcp.py
        ├── http.py
        ├── target_health.py
        ├── group.py          an Auto Scaling group's ping / tcp / http, on each of its instances
        ├── group_health.py   the group's own view: healthy and in service vs desired capacity
        ├── db_status.py      a database's status and replication, from RDS
        └── db_metrics.py     a database's CloudWatch metrics against thresholds
```
