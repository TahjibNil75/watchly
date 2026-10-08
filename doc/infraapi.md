# Infrastructure monitoring (AWS): API design

> **Status: phase 1 built, behind `INFRA_AWS_ENABLED`.** It describes how
> Watchly watches AWS resources (EC2 servers in public and private subnets, EC2
> Auto Scaling groups and every instance they run, and Application and Network
> Load Balancers, internal or internet-facing, with their target health) from
> an EC2 instance inside the same VPC. The endpoint
> list is in [§6](#6-api-at-a-glance), the open decisions in [§13](#13-decisions-for-review).
>
> **Revised 2026-10-04.** Infrastructure is now an entity of its own, separate
> from websites, with its own package (`app/monitoring/infra/aws/`), tables and
> API. It covers AWS only. [What changed from the first draft](#what-changed-from-the-first-draft)
> lists what moved.
>
> **Revised 2026-10-05.** Scope narrowed to servers and load balancers: RDS
> databases, their `postgres`/`mysql` checks and database credentials are gone.
> EC2 servers in public subnets are in, with an optional check of their public
> IP, and so are NLBs and internet-facing load balancers. Later that day: Auto
> Scaling groups, whose `ping`, `tcp` and `http` checks (typically their
> health API) run on every instance in service, plus a `group_health` check.
>
> **Revised 2026-10-05, later.** Several AWS accounts. An account has its
> own credentials: Watchly's own (the instance role) or an IAM user's access
> key, stored encrypted, and either may assume a role in the account. Every VPC
> belongs to one account, and every call about it uses that account's
> credentials. See [Several AWS accounts](#several-aws-accounts) and
> [`/accounts`](#7-vpcs).
>
> **Revised 2026-10-05, last.** RDS databases are back, **without database
> credentials**. A `database` resource is an RDS DB instance (Aurora's
> included), checked three ways, none of which logs in: `tcp` to its endpoint
> and port from inside the VPC, `db_status` (what `DescribeDBInstances` says:
> available, stopped, storage full, replication broken) and `db_metrics` (its
> CloudWatch CPU, free storage, freeable memory, connections and replica lag
> against thresholds). The IAM policy gains `rds:DescribeDBInstances` and
> `cloudwatch:GetMetricData`.
>
> **Then: infrastructure projects.** A project now monitors either websites or
> infrastructure, chosen when it is created. An infrastructure project is
> created with at least one AWS account, and owns its accounts. A VPC belongs
> to the project of its account, and is registered when someone first adds a
> resource from it; admins no longer register VPCs and grant them to projects
> (`PUT /vpcs/{id}/projects` and `aws_vpc_projects` are gone).
>
> **Revised 2026-10-07: capacity alerts.** What runs out before a resource goes
> down. A server's `ec2_metrics` (CPU, a burstable instance's CPU credits, its
> EBS volumes' burst balance and provisioned IOPS, its disks through the
> CloudWatch agent); `db_metrics` gains a storage forecast ("full in about 9
> days"), connections against `max_connections`, provisioned IOPS, gp2 burst
> balance and CPU credits, and counts storage autoscaling in. An AWS account
> may watch its capacity (`watch_capacity`): Elastic IPs attached to nothing,
> and its Elastic IP quota, as `account_capacity` alerts ([Capacity](#capacity)).
> The IAM policy gains `ec2:DescribeVolumes`, `ec2:DescribeInstanceTypes`,
> `rds:DescribeDBParameters` and `cloudwatch:ListMetrics`, and a Capacity
> statement.
>
> **Revised 2026-10-08: ALB edge security.** An Application Load Balancer's
> page grades the AWS WAF Web ACL attached to it, if any: the AWS managed rule
> groups that matter (core, known bad inputs, IP reputation, SQL injection), a
> rate-based rule, rule groups left in Count mode, WAF logging and fail-open.
> It also grades the ALB's own HTTP-to-HTTPS redirect, TLS policy, invalid-header
> dropping and desync mitigation. Read at each sync into `aws_detail` (`waf`,
> `attributes`), graded when read as `edge_security` on `GET /resources/{id}`.
> Nothing alerts. The IAM policy gains
> `elasticloadbalancing:DescribeLoadBalancerAttributes`,
> `wafv2:GetWebACLForResource` and `wafv2:GetLoggingConfiguration`.
>
> Companion docs: [`apis.md`](apis.md) for today's endpoints, [`hld.md`](hld.md)
> for how the pieces fit, and the package's
> [`README`](../app/monitoring/infra/aws/README.md) for its file layout.

**Contents**

1. [What changes](#1-what-changes)
2. [Security first: phase 0](#2-security-first-phase-0)
3. [Concepts](#3-concepts)
4. [AWS setup](#4-aws-setup)
5. [Permissions](#5-permissions)
6. [API at a glance](#6-api-at-a-glance)
7. [VPCs](#7-vpcs)
8. [Resources and checks](#8-resources-and-checks)
9. [Diagnose, overview and self](#9-diagnose-overview-and-self)
10. [Notifications](#10-notifications)
11. [Data model, configuration and code layout](#11-data-model-configuration-and-code-layout)
12. [Dashboard screens and the calls behind them](#12-dashboard-screens-and-the-calls-behind-them)
13. [Decisions for review](#13-decisions-for-review)
14. [Phases](#14-phases)

---

## 1. What changes

Today Watchly checks public URLs, hosts and DNS records. Running it on an EC2
instance inside a VPC lets it reach private addresses, as far as each target's
security group allows. This proposal adds a second kind of monitored thing,
next to websites:

| | Websites (today) | Infrastructure (new) |
| - | ---------------- | -------------------- |
| Watches | public URLs, hosts, DNS records | AWS resources: EC2 servers (public and private), Auto Scaling groups, ALBs and NLBs (internal and internet-facing) |
| Code | `app/monitoring/websites/` | `app/monitoring/infra/aws/` |
| API | `/api/v1/monitoring/websites` | `/api/v1/monitoring/infra/aws` |
| Tables | `websites`, `website_checks`, … | `aws_vpcs`, `aws_resources`, `aws_checks`, … |
| May reach | public addresses only (after [phase 0](#2-security-first-phase-0)) | addresses inside a registered VPC; public addresses only for a check over the internet |
| Dashboard | Websites | Infrastructure |

The two share what would otherwise be built twice: projects, members, roles
and permissions; the notification channels, per-kind notification settings and
`Notifier`; the scheduler tick and its advisory lock; and the egress policy
(`app/monitoring/egress.py`).

What infrastructure adds:

- **resources read from AWS**: an EC2 instance, an Auto Scaling group, an ALB
  or an NLB is added by its AWS id or picked from discovery, and its address
  and state stay in sync with AWS;
- **Auto Scaling groups as a whole**: a group's checks run on every instance it
  has in service at that moment, so instances replaced by scaling never raise
  false alarms, and the group's own health and capacity are watched too;
- **several checks per resource**, such as ping, a port and `/healthz` on one
  server, with the resource as the unit of alerting;
- **check types that understand infrastructure**: a TCP port, a load
  balancer's listener and its target health;
- **public and private paths**: a server is checked at its private IP from
  inside the VPC, or at its public IP over the internet; an internet-facing
  load balancer is checked the way its users reach it;
- **VPCs**: which VPCs Watchly may probe, and which projects may use each;
- **diagnosis**: "timed out" inside a VPC almost always means a security group,
  and Watchly can say which one;
- **a dashboard organised by VPC and resource.**

```
                         Internet
                            │  443 only, from your office / VPN / CloudFront
                            ▼
 ┌──────────────────────────── VPC prod-vpc 10.20.0.0/16 ─────────────────────────────┐
 │  public subnet 10.20.0.0/24                                                        │
 │  ┌──────────────────────────────┐    instance role watchly-monitor (read-only)     │
 │  │ EC2 watchly  10.20.0.25      │──────────────▶ EC2 / ELB APIs                    │
 │  │ sg-watchly                   │                                                  │
 │  │ web (nginx) · api + loop     │                                                  │
 │  └──────────────┬───────────────┘                                                  │
 │                 │ probes; each target's security group allows sg-watchly           │
 │  public subnets │  internet-facing ALB shop-web, public EC2 bastion 203.0.113.25   │
 │  private subnets▼                                                                  │
 │   internal ALB orders-api ── /health ──▶ orders-api-tg ──▶ 3 × EC2 :8080           │
 │   internal NLB ledger-tcp :7000 ──▶ ledger-tg ──▶ 2 × EC2 :7000                    │
 │   private EC2s: ssh :22, app ports, ICMP                                           │
 └────────────────────────────────────────────────────────────────────────────────────┘
      peered staging-vpc 10.30.0.0/16 ── registered as a VPC of its own; reachable
                                         when routes and security groups allow
```

Alerting channels, notification settings, maintenance windows, history, stats
and the event feed work as they do for websites. Infrastructure has its own
tables for them, and the same behaviour.

### What changed from the first draft

| First draft | Now |
| ----------- | --- |
| Infra checks were new `check_type`s on `/monitoring/websites`, in the `websites` table | A separate entity: `/monitoring/infra/aws`, `aws_*` tables, `app/monitoring/infra/aws/` |
| One monitor per check | A resource with several checks; outages and alerts per resource |
| Any network (`provider: aws` or `other`), probe agents for the rest | AWS only. VPCs are registered by id and their CIDRs read from AWS. No agents |
| Targets typed by hand; discovery optional | Resources come from AWS, by id or discovery, and their addresses are kept in sync |
| Redis / ElastiCache checks | Dropped: not in scope |
| Stored, encrypted passwords as a credential source | Dropped, then databases and credentials altogether (2026-10-05) |
| RDS databases, `postgres` / `mysql` checks | Dropped (2026-10-05); databases came back the same day without logging in: `tcp`, `db_status`, `db_metrics` |
| Internal ALBs only | ALBs and NLBs, internal or internet-facing (2026-10-05) |
| Any check could reach a private address inside a granted network | Website checks are public-only; only infrastructure probes reach private addresses |

---

## 2. Security first: phase 0

**This part should ship before Watchly is deployed into a VPC, even if nothing
else in this document does.**

Today:

- anyone who may create a project (Admin, DevOps, Project Manager) can add a
  check for any URL or host. Nothing refuses `http://10.20.21.7:5432` or
  `http://169.254.169.254/latest/meta-data/`;
- the HTTP checker follows redirects (`follow_redirects=True` in
  `checker.new_client()`), so a public page that redirects to an internal
  address is followed too;
- a check reports status, headers, body length and timings, and `must_contain`
  answers "does the body contain X?", which is enough to read a response one
  guess at a time.

On a laptop none of that reaches anything interesting. On an EC2 instance with
an IAM role and security-group access to private servers, it turns Watchly
into a port scanner for anyone who can create a project. If the instance still
allows IMDSv1, it also exposes the role's credentials.

### The egress policy

Every probe resolves its target, then each resolved address is judged before
anything connects to it. With websites and infrastructure split, the rule is
simple: website checks are for public addresses, and only infrastructure
probes reach private ones, and only inside their resource's VPC.

| Address | Website checks (`http`, `ping`, `dns`) | Infrastructure probes |
| ------- | -------------------------------------- | --------------------- |
| loopback `127.0.0.0/8`, `::1`; `0.0.0.0/8`; link-local `169.254.0.0/16`, `fe80::/10` (instance metadata `169.254.169.254`, ECS `169.254.170.2`, Amazon DNS `169.254.169.253`); metadata over IPv6 `fd00:ec2::254`; multicast; **this server's own addresses** (read from the instance metadata at startup); anything in `EGRESS_DENY_CIDRS` | **blocked** | **blocked** |
| private: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `fc00::/7` | **blocked**, unless `WEBSITE_PRIVATE_TARGETS=allow` | allowed only inside the CIDRs of the resource's VPC, which belongs to the resource's project |
| everything else (public) | allowed, as today | **blocked**, unless the check goes over the internet: an internet-facing load balancer, or a server check with `use_public_ip`. The address then comes from AWS (the DNS name or public IP AWS gave the resource), never from a person. |

**Where it goes.** `app/monitoring/egress.py` holds `vet()`, used by both
sides: website checks pass "public only", infrastructure probes pass their
VPC's CIDRs (`vpc_scope()`, with `allow_public` for a check over the internet). For websites, `_TimedBackend.connect_tcp` in
[`checker.py`](../app/monitoring/websites/checker.py) already resolves the host
itself before connecting. The policy sits between `getaddrinfo` and
`_connect_first`: it filters the resolved addresses, and only the ones that
pass are connected to. Because the connection goes to the address that was
checked, DNS rebinding cannot swap it afterwards. Every redirect hop opens its
connection through the same backend, so redirects are covered too.
`pinger.py` and every infrastructure probe call `vet()` before they connect. A refused check fails with `error_type: blocked_address` and names the
rule.

DNS resolvers are exempt: they come from admin configuration
(`DNS_RESOLVERS`), never from a check.

Watchly's own addresses matter because `docker-compose.yml` publishes the API
(`8000`) and its own PostgreSQL (`5432`, default password `postgres`) on the
host. Inside the VPC, `10.20.0.25:5432` is a private address in the VPC's
range, so without this rule someone could add Watchly's own instance as a
server and probe its database.

### On the instance

| What | Why |
| ---- | --- |
| **IMDSv2 required** (`HttpTokens=required`), hop limit 2 | IMDSv2 needs a `PUT` for its token, and the checker only sends `GET`, `HEAD`, `POST` and `OPTIONS`. A gap in the policy still could not read the role. Hop limit 2 lets the API container use the role itself. |
| Inbound **443 only, from known addresses** (office, VPN, CloudFront or an ALB with WAF). Never expose 8000 or 5432. | The dashboard now holds the keys to the VPC. |
| A **read-only** IAM role | See [§4](#4-aws-setup). |

`WEBSITE_PRIVATE_TARGETS=block` can break existing website checks that point at
private addresses (a LAN install, for example). `GET /monitoring/websites/blocked`
lists them before the upgrade. `allow` keeps today's behaviour and must never
be set on an EC2 deployment. See decision 3 in [§13](#13-decisions-for-review).

---

## 3. Concepts

### VPC

A VPC Watchly may probe. It is in one AWS account of one infrastructure
project, and only that project's resources live in it. It is registered by its
id, when someone first adds a resource from it; Watchly reads the rest from
AWS. Two projects that watch the same VPC each register it.

| Field | Meaning |
| ----- | ------- |
| `account` | The AWS account it is read with, and so its `project`. |
| `name` | e.g. `prod-vpc`; unique within its account. Defaults to the VPC's `Name` tag. |
| `region`, `aws_vpc_id` | The region defaults to Watchly's own. |
| `cidrs` | Read from AWS (`DescribeVpcs`) and refreshed by every [sync](#discovery-and-sync); not editable. Infrastructure probes may reach these ranges, and public addresses only for a check over the internet. |
| `is_watchly_vpc` | Whether Watchly itself runs in this VPC. Any other VPC is reached through peering or a transit gateway. |

Every VPC stands on its own, as every project does: two VPCs may share or
overlap ranges (e.g. `10.50.0.0/18` in each environment's account). A probe
only reaches the ranges of its own resource's VPC; where an address leads is up
to the routes of the host Watchly runs on. Names resolve through Watchly's own VPC resolver (Amazon DNS), so an
internal load balancer's name resolves to private addresses. For a peered VPC, turn on DNS
resolution across the peering connection.

### Resource

An AWS resource that belongs to one project and one VPC. Watchly reads it from
AWS; nobody types its address.

| `kind` | AWS resource | `aws_id` | `address` (read from AWS) |
| ------ | ------------ | -------- | ------------------------- |
| `server` | EC2 instance, in a public or a private subnet | instance id, `i-0b2e5d6c7b8a9f0e1` | its private IP; its public IP, if it has one, is in `aws_detail.public_ip` |
| `load_balancer` | Application or Network Load Balancer, internal or internet-facing (not a Gateway Load Balancer) | its ARN (or name, when adding) | its DNS name |
| `auto_scaling_group` | EC2 Auto Scaling group launching into the VPC's subnets | its name, `orders-api-asg` | none: its instances, with their private and public IPs, are in `aws_detail.instances`, and read again at every check |
| `database` | RDS DB instance whose subnet group is in the VPC, Aurora's included (each Aurora instance is its own resource) | its identifier, `orders-db` | its endpoint, `orders-db.c9akciq32.ap-southeast-1.rds.amazonaws.com`; its port is `aws_detail.port` |

A server is **public** when it has a public IP, and a load balancer when its
scheme is `internet-facing`. The address, `Name` tag, AWS state (`running`,
`stopped`, `active`, …) and details (instance type, public IP and DNS name,
load balancer type, scheme and listeners, AZ, subnet, security groups, tags)
are refreshed by the sync, so a public IP that changes on stop and start is
picked up. A resource whose `aws_id` no
longer exists becomes `missing`: its checks stop, and it stays under "needs
attention" until someone removes it.

Instances in an Auto Scaling group are replaced, and their ids change with
them. Watch the **group** rather than its instances one by one: discovery
lists it next to its instances, and labels each instance with its group's
name. A group's `ping`, `tcp` and `http` checks run on every instance it has
in service, read from AWS (`DescribeAutoScalingGroups`, then
`DescribeInstances`) at each run, so an instance removed by a scale-in is never
probed and one launched since the last sync is. An instance still launching
(`Pending`), or in service for less than the group's health-check grace
period, is left out until it is ready, as the group leaves it out of its own
health checks. Its `aws_state` reads `3 of 3 in service`.

### Check

What Watchly does to a resource. A resource has 1–10 checks, each with its own
interval, timeout, retries and settings.

| `check_type` | on | what it does | up when | degraded when |
| ------------ | -- | ------------ | ------- | ------------- |
| `ping` | server | ICMP echo to the private IP (or the public one) | any echo is answered | packet loss at or over its threshold |
| `tcp` | server, load balancer | connects to a port (one of its listeners' for a load balancer), and does a TLS handshake or reads a banner, if set | the port accepts the connection | — |
| `http` | server, load balancer | `GET` a path on the server's private (or public) IP or the load balancer's DNS name | the expected status (and `must_contain`, if set) | slower than its threshold |
| `target_health` | load balancer, Auto Scaling group | reads `DescribeTargetHealth` for one of its target groups | at least `min_healthy_targets` targets are `healthy` | any registered target is not healthy |
| `ping`, `tcp`, `http` on a group | Auto Scaling group | the same probe, on every instance in service (at most 50 per run, 10 at once) | at least `min_healthy_instances` instances pass (by default, at least one) | any instance fails (`instances_failing`), or an instance's own problem (slow, lossy) |
| `group_health` | Auto Scaling group | reads `DescribeAutoScalingGroups`: each instance's lifecycle state and the group's health status for it (EC2 status checks, or the load balancer's with the `ELB` health check type) | at least `min_healthy_instances` are `InService` and `Healthy` (capped at the desired capacity) | an instance in service is unhealthy (`instances_unhealthy`); fewer healthy plus launching instances than desired (`capacity_short`, with the reason the latest scaling activity failed) |
| `db_status` | database | reads `DescribeDBInstances` | `available`, or busy with something it serves through (`backing-up`, `modifying`, `maintenance`, `upgrading`, …) | it is busy (`db_busy`); a read replica's replication is in error or stopped (`replication_broken`) |
| `db_metrics` | database | reads its latest CloudWatch datapoints over 15 minutes with one `GetMetricData`: `CPUUtilization`, `FreeStorageSpace` (not on Aurora), `FreeableMemory`, `DatabaseConnections`, `ReplicaLag` (`AuroraReplicaLag` on Aurora) for a replica, `ReadIOPS` and `WriteIOPS` for io1, io2 and gp3 storage, `BurstBalance` for gp2, and `CPUCreditBalance` and `CPUSurplusCreditBalance` for a `db.t*` class; and, hourly, 7 days of `FreeStorageSpace` for the forecast | CloudWatch answers; no datapoint (a stopped or brand-new database) still passes | CPU over `cpu_percent_max` (`high_cpu`, default 90); free storage under `free_storage_percent_min` of what it may use, the allocated storage or with storage autoscaling its maximum (`low_storage`, default 10); running out within `storage_full_days_min` days at the rate it shrank since storage was last added (`storage_filling`, default 14); freeable memory under `freeable_memory_mb_min` (`low_memory`, off); connections over `connections_max` (`many_connections`, off) or over `connections_percent_max` of `max_connections` (`connections_near_limit`, default 80); IOPS over `iops_percent_max` of provisioned (`iops_saturated`, default 90); gp2 burst balance under `burst_balance_percent_min` (`low_burst_balance`, default 20); CPU credits under `cpu_credits_percent_min` of the most it can bank, or spending surplus credits (`low_cpu_credits`, default 10); replica lag over `replica_lag_seconds_max` (`replica_lag`, default 60) |
| `ec2_metrics` | server | reads its latest CloudWatch datapoints over 30 minutes, in 5-minute buckets, with one `GetMetricData`: `CPUUtilization`; `CPUCreditBalance` and `CPUSurplusCreditBalance` on a burstable (T) instance; `EBSIOBalance%` and `EBSByteBalance%` on sizes up to 2xlarge; `BurstBalance` of each gp2, st1 and sc1 volume; `VolumeReadOps` and `VolumeWriteOps` of each io1, io2 and gp3 volume; and the CloudWatch agent's `disk_used_percent` for each filesystem it reports, found with `ListMetrics` | CloudWatch answers; no datapoint (a stopped or brand-new instance) still passes | CPU over `cpu_percent_max` (`high_cpu`, default 90); CPU credits under `cpu_credits_percent_min` of the most it can bank, or spending surplus credits in unlimited mode (`low_cpu_credits`, default 10); a volume's, or the instance's own, EBS burst balance under `burst_balance_percent_min` (`low_burst_balance`, default 20); a volume's IOPS over `iops_percent_max` of provisioned (`iops_saturated`, default 90); a filesystem over `disk_used_percent_max` used (`low_disk_space`, default 90) |
| `ec2_status` *(phase 2)* | server | reads `DescribeInstanceStatus` | `running`, and both status checks `ok` | a scheduled event (reboot, retirement) is pending |

**Private or public path.** A server's `ping`, `tcp` and `http` go to its
private IP, from inside the VPC, unless `use_public_ip` is set: then they go
to its public IP, over the internet, which tests what the internet sees (its
security group's rules for the world, the internet gateway, an Elastic IP).
Only a server that has a public IP takes `use_public_ip`. An internet-facing
load balancer's name resolves to public addresses, so its checks always go
over the internet; an internal one's stay inside the VPC.

**Databases without credentials.** Watchly never logs in to a database, so it
needs no user, password or IAM database authentication. A database's `tcp`
check opens and closes a connection to its endpoint and port from inside the
VPC (its security group must allow `sg-watchly` on that port), which tells
"the database is up and reachable" from "a security group or route is in the
way". `db_status` and `db_metrics` ask RDS and CloudWatch, so they work even
when the network path does not. What none of them can tell is whether a query
succeeds: a full connection pool, locks or a broken schema show up only as
`connections_near_limit`, `many_connections` or `high_cpu`, or not at all. A
database is always checked inside the VPC; `use_public_ip` is refused for it.

**Limits Watchly measures against.** `connections_near_limit` needs the
database's `max_connections`. Sync reads it from its DB parameter group
(`rds:DescribeDBParameters`, reused for an hour): a number is used as it is; a
formula such as `LEAST({DBInstanceClassMemory/9531392},5000)` is worked out
for its class, whose memory comes from `ec2:DescribeInstanceTypes`, and is an
estimate, a little high, since RDS keeps some of the class's memory for itself.
A formula Watchly cannot work out (Aurora MySQL's uses `log`) leaves it
unknown; the check's own `max_connections` setting overrides either way. A T
instance's or `db.t*` class's CPU credits are measured against the most it can
bank, 24 hours of what it earns, from AWS's table. IOPS are measured only where
they are a hard cap (io1, io2, gp3); gp2, st1 and sc1 volumes burst above their
baseline, so their burst balance is what says they are running out.

**The storage forecast.** `storage_filling` fits a straight line, by least
squares, to 7 days of hourly free storage, starting again after the last rise
of more than 2% of the allocated storage (at least 1 GB) within an hour:
storage added, or a large cleanup. Under 12 hours of history, or free storage
not shrinking, makes no forecast. With storage autoscaling on, what is left is
free storage plus the room to grow to its maximum, so a database autoscaling
will grow in time is not reported as filling up, and neither as `low_storage`.

**A server's disks.** EC2 cannot see inside an instance, so `low_disk_space`
needs the CloudWatch agent publishing `disk_used_percent` to `CWAgent` with
the `InstanceId` dimension, as its default configuration does. Watchly lists
those metrics with `cloudwatch:ListMetrics` (reused for an hour), skipping
`tmpfs`, `devtmpfs`, `overlay` and `squashfs`. Without the agent, the snapshot
says so and the rest of the check works as usual.

**An Auto Scaling group's health API.** A group behind a load balancer has a
target group whose health check calls each instance's health endpoint, such as
`GET /healthz` on 8080. Discovery reads it and suggests an `http` check with
that path, port and expected status, run on every instance, next to
`group_health` and the target group's `target_health`. A group with no target
group gets a `ping` of each instance; add an `http` check of its health API by
hand.

**The load balancer's health check.** A load balancer checks its targets
itself, with the protocol, path, port and matcher set on each target group.
`target_health` reads that verdict for every target, with AWS's reason code
(`Target.ResponseCodeMismatch`, `Target.Timeout`, …), and shows the target
group's health-check settings next to it. An `http` check of an ALB's
`/health`, or a `tcp` check of an NLB's listener, tests the path a request
takes: listener, then a healthy target. Together they separate "the load
balancer does not answer" from "some targets are failing". Discovery suggests
both.

### Health and state

Each check has a **health**:

| `health` | when |
| -------- | ---- |
| `unknown` | not run yet |
| `healthy` | passed, no open problem |
| `degraded` | passed, but one of its problems has been open for `INFRA_PROBLEM_CHECKS` checks in a row |
| `down` | failed, after its retries |

A **problem** is a slow `http` response, `ping` packet loss, a target group
with an unhealthy target, or for an Auto Scaling group: an instance failing
its check, an instance the group marks unhealthy, or capacity short of the
desired count.

A resource has exactly one **state**, so counts always add up: `paused`
(disabled), `maintenance` (a window is in effect), `missing` (AWS no longer has
it), or else the worst health of its enabled checks: `down`, then `degraded`,
then `healthy`, and `unknown` until a check has run.

### Alerts per resource, not per check

A server that loses power fails its ping, its port and its `/healthz` on the
same tick. That is one outage, not three, so the resource is the unit of
alerting:

| Event | When | Notification |
| ----- | ---- | ------------ |
| down | the first of its checks goes down | `infra_down`, listing every failing check, and AWS's state for the resource (`stopped`, `active`, …) |
| still down | while `down_alerts_sent < max_down_alerts`, as for websites | `infra_still_down` |
| recovered | every check is up again | `infra_recovered`, with the total downtime |
| degraded | a problem opens | `infra_degraded`, once per problem, then quiet for `INFRA_PROBLEM_ALERT_COOLDOWN_SECONDS` |

A check that goes down while its resource is already down joins the open
outage, and the next follow-up lists it. The policy, and the Slack and
Telegram threading, are the websites' (see
[`service.py`](../app/monitoring/service.py)), ported to `monitor.py`, so an
infrastructure outage behaves the way a site outage does.

### When a whole VPC fails: one alert, not forty

If Watchly loses its route into a VPC (a security group edit, a removed
peering, a NACL), every check in that VPC times out on the same tick. After
each tick, a VPC is marked **unreachable** when at least
`VPC_UNREACHABLE_MIN_CHECKS` of its checks ran and at least
`VPC_UNREACHABLE_PERCENT` of them failed to *connect* (timeout, no route).
"Connection refused" means the host answered, so it does not count. While the
VPC is unreachable:

- checks still record their results, so history stays honest;
- its resources' own down alerts are held, and each project with resources in
  the VPC gets **one** `vpc_unreachable` alert instead;
- when the VPC answers again, `vpc_recovered` goes out, and any resource still
  down then sends its own `infra_down`.

`target_health`, `group_health`, `db_status`, `db_metrics`, `ec2_metrics` and `ec2_status` read the AWS API rather than
connecting into the VPC, and a check over the internet (an internet-facing load balancer, a
server's public IP) does not go through the VPC's routes, so none of them
count towards the share.

### Discovery and sync

Using the instance role, Watchly lists what is in a VPC: EC2 instances, public
and private, ALBs and NLBs, internal and internet-facing, with their
target groups, Auto Scaling groups, and RDS DB instances. For each one it suggests checks and flags what is already
monitored:

| Discovered | Suggested checks |
| ---------- | ---------------- |
| EC2 instance | `ping`, and a `tcp` check for each port its security groups open to `sg-watchly`, all to its private IP. For a public instance, the add form can send them to its public IP instead. `ec2_metrics` with the default thresholds. |
| ALB | `http` on its listener, with the path its target group's health check uses; one `target_health` per target group |
| NLB | `tcp` on its first TCP or TLS listener; one `target_health` per target group |
| Auto Scaling group | `group_health`; an `http` (or `tcp`) check of each instance, from each of its target groups' health checks (path, port, expected status); a `ping` of each instance when it has no target group; one `target_health` per target group |
| RDS DB instance | `db_status`; `tcp` on its endpoint's port; `db_metrics` with the default thresholds |

Importing creates the resources and their checks in one go. A resource can also
be added by its AWS id.

The same calls run every `AWS_SYNC_INTERVAL_SECONDS` to keep each resource's
address and state current. They are batched per VPC: one `DescribeInstances`,
one `DescribeVolumes` (each server's volumes' type, size and IOPS), one
`DescribeLoadBalancers`, one `DescribeAutoScalingGroups` and one
`DescribeDBInstances` per VPC per sync, whatever the number of resources, and
a database's parameter group at most once an hour. A refused `DescribeVolumes`
is listed with discovery's skipped calls; the volumes then keep only their id,
and `ec2_metrics` reads every one's burst balance and none's IOPS.

### Diagnose

A dry run of a check that reports each step: resolve, policy, connect, TLS,
response. It saves nothing. The add form uses it for **Test connection**,
and a failing resource's page uses it for **Why is this down?** A connect
timeout gets a hint naming the security group that is missing the rule
(phase 2):

```
resolve   ✓        10.20.21.55 is an IP address
policy    ✓        10.20.21.55 is inside prod-vpc (10.20.0.0/16)
connect   ✗ 5.0 s  no answer on 8080
                   sg-0db7c1e9f2a3b4c5d (report-runner) has no inbound rule for TCP 8080 from
                   sg-0a7e3c2194b1d5f60 (watchly). Add: TCP 8080, source sg-0a7e3c2194b1d5f60.
tls · response     skipped
```

A **timeout** means something dropped the packets (security group, NACL,
route). **Connection refused** means the host answered and nothing listens on
that port. The difference tells you where to look, so diagnose always reports
which one it was.

---

## 4. AWS setup

One-time, by whoever owns the AWS account.

**Placement.** An EC2 instance in a public subnet of the VPC, with an Elastic
IP, running the existing `docker-compose.yml`. Watchly's own database can stay
in Compose or move to RDS.

**Security groups.**

| Group | Rule |
| ----- | ---- |
| `sg-watchly` inbound | 443 (and 80 for the redirect) from your office / VPN ranges, or from the ALB / CloudFront in front. Nothing else. |
| `sg-watchly` outbound | the VPC's CIDR (and peered ones) for probes; 443 to the internet for alert channels and AWS APIs; 587 if SMTP; the ports of internet-facing load balancers and of servers checked at their public IP. |
| each target's group inbound | from `sg-watchly`: the listener ports for internal load balancers, the service port (or 22) for servers, the database port (5432, 3306, …) for databases, ICMP echo for ping checks. A server checked at its public IP needs the rule for the world (or Watchly's Elastic IP) instead. |

**Peered VPCs.** Routes both ways, security groups that name the peer's range
(security-group references only work within a region), and DNS resolution
across the peering, so internal load balancers' names resolve to private
addresses.

**IAM role `watchly-monitor`**, attached through an instance profile:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Discover",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeRouteTables", "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus", "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSecurityGroups",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeListeners",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth",
        "elasticloadbalancing:DescribeTags",
        "autoscaling:DescribeAutoScalingGroups",
        "autoscaling:DescribeScalingActivities",
        "rds:DescribeDBInstances",
        "cloudwatch:GetMetricData",
        "ec2:DescribeVolumes",
        "ec2:DescribeInstanceTypes",
        "rds:DescribeDBParameters",
        "cloudwatch:ListMetrics",
        "elasticloadbalancing:DescribeLoadBalancerAttributes",
        "wafv2:GetWebACLForResource",
        "wafv2:GetLoggingConfiguration"
      ],
      "Resource": "*"
    }
  ]
}
```

`ec2:DescribeVolumes` through `cloudwatch:ListMetrics` are what `ec2_metrics`
and `db_metrics` measure against: a volume's type and IOPS, an RDS class's
memory and its parameter group's `max_connections`, and the CloudWatch agent's
disks. Without them those thresholds are skipped, and the rest of each check
works.

The last three grade an ALB's edge security (`edge_security` on the resource):
the AWS WAF Web ACL in front of it, its rules and logging, and the ALB's TLS,
redirect and header settings. Without them those items read "unknown".

To have an account's Elastic IPs and Elastic IP quota watched
(`watch_capacity`, see [Capacity](#capacity)), add:

```json
{
  "Sid": "Capacity",
  "Effect": "Allow",
  "Action": [
    "ec2:DescribeAddresses",
    "servicequotas:GetServiceQuota",
    "servicequotas:GetAWSDefaultServiceQuota"
  ],
  "Resource": "*"
}
```

To have an account's CodeDeploy deployments announced and silence their
resources (`watch_deployments`), add one more statement. It is read-only too,
and free:

```json
{
  "Sid": "Deployments",
  "Effect": "Allow",
  "Action": [
    "codedeploy:ListDeployments",
    "codedeploy:BatchGetDeployments",
    "codedeploy:GetDeploymentGroup"
  ],
  "Resource": "*"
}
```

Nothing here can change anything. All the `Describe*` calls, and the Service
Quotas reads, are free. `cloudwatch:GetMetricData` is billed per metric read,
US$0.01 per 1,000: a `db_metrics` check reads 5 to 10 a run, and one more an
hour for the forecast, so every 5 minutes is roughly US$0.45 to US$0.90 a month
per database. An `ec2_metrics` check reads 1 (CPU), 2 more on a burstable
instance, 2 for the instance's EBS burst, 1 per gp2/st1/sc1 volume or 2 per
io1/io2/gp3 volume, and 1 per disk: a t3 with one gp3 volume and two disks is
9, roughly US$0.80 a month every 5 minutes. A longer interval costs less.
`cloudwatch:ListMetrics` is reused for an hour per server.
`ec2:DescribeRouteTables` only tells the VPC map which subnets are public (a
route to an internet gateway); without it the map guesses from each subnet's
"auto-assign public IP" setting.

### Several AWS accounts

Watchly reads each AWS account with that account's own credentials. AWS
accounts belong to infrastructure projects: a project is created with at least
one (**Projects → New project → Infrastructure**, `POST /monitoring/projects`),
and more are added on its page (`POST /accounts`). Each has one of:

| Credentials | When |
| ----------- | ---- |
| **Watchly's own** (`auth_type: default`) | The account Watchly runs in: the instance role above. |
| **Watchly's own, assuming a role** (`default` + `role_arn`) | Another account, the recommended way: create `watchly-monitor` in it with the policy above and a trust policy that lets Watchly's instance role assume it (with an `ExternalId`, if you like). No long-lived key is stored. |
| **An access key** (`access_key`) | An IAM user in that account with the policy above. The secret is stored encrypted with `SECRET_KEY` (`app/core/crypto.py`) and never returned; rotating `SECRET_KEY` means entering it again. |
| **An access key, assuming a role** (`access_key` + `role_arn`) | An IAM user in one account that assumes the role in another. |

The trust policy of `watchly-monitor` in the other account, for the instance
role:

```json
{
  "Effect": "Allow",
  "Principal": {"AWS": "arn:aws:iam::<watchly-account-id>:role/watchly-monitor"},
  "Action": "sts:AssumeRole",
  "Condition": {"StringEquals": {"sts:ExternalId": "<external id>"}}
}
```

and the instance role itself needs `sts:AssumeRole` on that role's ARN.
Credentials are tried (`sts:GetCallerIdentity`, which needs no permission)
before they are saved, and the 12-digit account id it answers is recorded: one
AWS account is added to a project once (two projects may each add it). Credentials changed later must reach the same AWS
account while it holds VPCs. `POST /accounts/{id}/test` tries every permission
above with the account's credentials.

Probes still leave from Watchly's own instance, so a VPC in another account is
reached as a peered one: peering or a transit gateway, routes, and security
groups that allow Watchly's address range. VPCs whose ranges overlap are
registered all the same; Watchly's host routes an overlapping address to one of
them, so a private-address probe into the others needs its own path (or a
check that only asks AWS, or `use_public_ip`).

---

## 5. Permissions

Same roles as today ([`hld.md` §6](hld.md#6-permissions)). An
infrastructure project's AWS accounts, VPCs and resources follow the project:
whoever may manage the project (Admin, DevOps, the project manager who owns
it) manages them, and a resource lives in a VPC of its own project's accounts.

| | Admin | DevOps | Project manager | Developer, Viewer |
| - | :-: | :-: | :-: | :-: |
| Create an infrastructure project with its AWS accounts | ✅ | ✅ | ✅ | ❌ |
| Add, edit, test, delete a project's AWS accounts | ✅ | ✅ | their own projects' | ❌ |
| Register (on first use), test, rename, delete VPCs | ✅ | ✅ | their own projects' | ❌ |
| List / read VPCs | all | all | those of their projects | those of projects they can see |
| VPC map | all | all | their projects' VPCs; only resources they can see | same |
| Discover | ✅ | ✅ | in their projects' VPCs | ❌ |
| Add, edit, delete resources and checks; import | project rights, in a VPC of the project | same | same | ❌ |
| Diagnose | ✅ with security-group hints | ✅ with hints | own projects, no hints | ❌ |
| `/self` | ✅ | ✅ | ❌ | ❌ |
| Overview, resource detail, history, events | scoped exactly like websites today | | | |

As elsewhere, a VPC or resource the caller cannot see answers
`404`, not `403`. In a discovery result, `monitored_by` only names resources
the caller can see.

---

## 6. API at a glance

**Base URL:** `/api/v1/monitoring/infra/aws`. **Auth:** bearer token, as
everywhere else. Every endpoint answers `404` while `INFRA_AWS_ENABLED=false`.

| # | method | endpoint | what it does |
| - | ------ | -------- | ------------ |
| | | **AWS accounts** | |
| A1 | `GET` | `/accounts` | The AWS accounts of the projects the caller manages (`project_id` for one), how each is reached and how many VPCs it holds. |
| A2 | `POST` | `/accounts` | Add an account to an infrastructure project: Watchly's own credentials or an access key, optionally assuming a role. Tried before saving. |
| A3 | `GET` | `/accounts/{account_id}` | One account. The secret is never returned, only `secret_access_key_hint`. |
| A4 | `PATCH` | `/accounts/{account_id}` | Rename, change its default region, or its credentials (tried again). |
| A5 | `DELETE` | `/accounts/{account_id}` | Remove an account that holds no VPC; not the project's last. |
| A6 | `POST` | `/accounts/{account_id}/test` | Who its credentials are and which permissions they have. |
| | | **VPCs** | |
| V1 | `GET` | `/vpcs` | Registered VPCs the caller may see, with health and per-state counts. |
| V2 | `GET` | `/vpcs/available` | VPCs an account has in a region, and which are registered. |
| V3 | `POST` | `/vpcs` | Register a VPC of a project's account by its id, for that project; CIDRs come from AWS. |
| V4 | `GET` | `/vpcs/{vpc_id}` | One VPC, with its projects, counts and last test. |
| V5 | `PATCH` | `/vpcs/{vpc_id}` | Rename, or change the description. |
| V6 | `DELETE` | `/vpcs/{vpc_id}` | Delete a VPC; with `with_resources=true`, its monitored resources too. |
| V8 | `POST` | `/vpcs/{vpc_id}/test` | Test the VPC now: placement, AWS access, reach to a sample of its resources. |
| V9 | `POST` | `/vpcs/{vpc_id}/discover` | The VPC's EC2 instances, Auto Scaling groups, ALBs and NLBs, with suggested checks and what is already monitored. |
| V10 | `POST` | `/vpcs/{vpc_id}/import` | Create resources and their checks from discovered items, all at once. |
| V11 | `GET` | `/vpcs/{vpc_id}/topology` | Nodes and edges for the map: subnets, load balancer → targets, probe paths. |
| | | **Resources** | |
| R1 | `GET` | `/resources` | List resources, filterable by VPC, project, kind, state and environment. |
| R2 | `GET` | `/resources/summary` | Counts per state, with the same filters. |
| R3 | `GET` | `/resources/events` | The infrastructure event feed, for the toasts and history. |
| R4 | `POST` | `/resources` | Add a resource by its AWS id, with its checks. |
| R5 | `GET` | `/resources/{resource_id}` | One resource, with its checks, problems and AWS details. |
| R6 | `PATCH` | `/resources/{resource_id}` | Rename, change environment, pause or resume. |
| R7 | `DELETE` | `/resources/{resource_id}` | Stop monitoring it; its history goes too. |
| R8 | `POST` | `/resources/{resource_id}/check` | Run all its checks now and record the results. |
| R9 | `GET` | `/resources/{resource_id}/results` | Recent check results, with each type's detail group. |
| R10 | `GET` | `/resources/{resource_id}/stats` | Uptime, response time and each check's own metric over a range; CSV too. |
| R11 | `GET` | `/resources/{resource_id}/targets` | Load balancers and Auto Scaling groups: each target's state now and over the last 24 hours. |
| R12 | `POST` / `DELETE` | `/resources/{resource_id}/recipients`, `…/recipients/{user_id}` | Extra people alerted about this resource only, as for websites. |
| R13 | `POST` / `DELETE` | `/resources/{resource_id}/maintenance`, `…/maintenance/end`, `…/maintenance/{window_id}` | Maintenance windows, as for websites. |
| | | **Checks** | |
| K1 | `POST` | `/resources/{resource_id}/checks` | Add a check to a resource. |
| K2 | `PATCH` | `/resources/{resource_id}/checks/{check_id}` | Change its settings or interval; `check_type` stays fixed. |
| K3 | `DELETE` | `/resources/{resource_id}/checks/{check_id}` | Remove a check; not the resource's last one. |
| | | **Diagnose, overview, self** | |
| D1 | `POST` | `/diagnose` | Dry-run a check step by step (resolve → policy → connect → TLS → response). Saves nothing. |
| D2 | `GET` | `/overview` | Dashboard data: counts by state, per VPC and per kind, and what needs attention. |
| D3 | `GET` | `/self` | Where Watchly runs, IMDS settings, the egress guard, and which permissions Watchly's own role has. |
| | | **Websites (phase 0)** | |
| W1 | `GET` | `/api/v1/monitoring/websites/blocked` | Existing website checks the egress policy refuses (Admin/DevOps). |

---

## 7. VPCs

Every VPC comes back as:

```json
{
  "id": 1,
  "name": "prod-vpc",
  "description": "Production, Singapore",
  "region": "ap-southeast-1",
  "aws_vpc_id": "vpc-0a1b2c3d4e5f60718",
  "cidrs": ["10.20.0.0/16"],
  "is_watchly_vpc": true,
  "projects": [{"id": 3, "name": "Orders platform"}, {"id": 5, "name": "Data & analytics"}],
  "health": "reachable",
  "unreachable_since": null,
  "synced_at": "2026-10-04T09:10:00Z",
  "last_tested_at": "2026-10-04T09:12:00Z",
  "counts": {"total": 9, "healthy": 5, "degraded": 2, "down": 1, "unknown": 0, "maintenance": 1, "paused": 0, "missing": 0},
  "created_by_id": 1,
  "created_at": "2026-10-01T08:00:00Z"
}
```

`id` is Watchly's; `aws_vpc_id` is AWS's. `health` is `reachable`,
`unreachable` (see [§3](#when-a-whole-vpc-fails-one-alert-not-forty)) or
`untested` (no checks yet). `counts` are resource states, which add up to
`total`.

### `POST /accounts`
Body: `project_id` (an infrastructure project the caller manages), `name`
(unique in the project), `auth_type` (`access_key`, the default, or `default` for
Watchly's own credentials), `access_key_id` and `secret_access_key` (with
`access_key`), and optionally `role_arn`, `external_id`, `default_region`,
`environment` (`development`, `testing`, `uat`, `staging` or `production`: what
its resources get when they are added without one), `watch_deployments`
(default `false`: see [Deployments](#deployments)), `watch_capacity` (default
`false`: see [Capacity](#capacity)) and `description`.

```json
{
  "name": "Client A production",
  "auth_type": "default",
  "role_arn": "arn:aws:iam::210987654321:role/watchly-monitor",
  "external_id": "watchly-7f3a",
  "default_region": "ap-southeast-1"
}
```
`201`, the account with `aws_account_id` read from STS, and its `project` ·
`403` · `404` no such project · `409` name taken in the project, or that AWS
account is already in it · `422` incomplete credentials, AWS refused them, or
the project monitors websites

### `PATCH /accounts/{account_id}`
Only what is sent changes. A new `access_key_id` comes with its
`secret_access_key`; the secret alone may be sent to replace it. Switching to
`default` drops the stored key. New credentials are tried before they are
saved and must reach the same AWS account while it holds VPCs (`409`).
Turning `watch_deployments` off ends what it was following at once, quietly:
those deployments read `Unwatched`, and their resources are checked again.
Turning `watch_capacity` off or on forgets what the last look found and its
open problems, so it starts afresh.

An account reads back, besides what was sent: `watch_capacity`,
`capacity_checked_at`, `capacity_error` (per region) and `capacity`, what the
last look found in each region:

```json
"capacity": [
  {
    "region": "ap-southeast-1", "elastic_ips": 4, "elastic_ip_quota": 5, "quota_source": "applied",
    "unattached": [{"public_ip": "203.0.113.10", "allocation_id": "eipalloc-0a1b2c3d", "name": "old-bastion"}]
  }
]
```

### `DELETE /accounts/{account_id}`
`204` · `409` it still holds VPCs, or it is the project's last account.

### `POST /accounts/{account_id}/test`
Query: `region` (default the account's own). Rate-limited like diagnose. An
account that watches deployments is also tried for the three `codedeploy:`
reads, the last two on its latest deployment, and one that watches its
capacity for `ec2:DescribeAddresses` and `servicequotas:GetServiceQuota`.
`rds:DescribeDBParameters` is tried on the first database's parameter group,
when there is a database.

```json
{
  "ok": false,
  "tested_at": "2026-10-05T09:12:00Z",
  "region": "ap-southeast-1",
  "caller": {"account": "210987654321", "arn": "arn:aws:sts::210987654321:assumed-role/watchly-monitor/watchly-monitor"},
  "permissions": [
    {"action": "ec2:DescribeVpcs", "ok": true},
    {"action": "autoscaling:DescribeScalingActivities", "ok": false, "detail": "AccessDenied"}
  ]
}
```

### `GET /vpcs`
Query: `limit`, `offset`, `health`. Each VPC carries its `account`
(`id`, `name`, `aws_account_id`) and `project`.
`200`

### `GET /vpcs/available`
Query: `account_id` (required), `region` (default the account's default
region, else Watchly's own). What the account's credentials can see, for the
Add resources form:

```json
{
  "account": {"id": 1, "name": "Production", "aws_account_id": "123456789012"},
  "region": "ap-southeast-1",
  "vpcs": [
    {"aws_vpc_id": "vpc-0a1b2c3d4e5f60718", "name": "prod-vpc", "cidrs": ["10.20.0.0/16"], "is_watchly_vpc": true, "registered_as": 1},
    {"aws_vpc_id": "vpc-0f9e8d7c6b5a40312", "name": "staging-vpc", "cidrs": ["10.30.0.0/16"], "is_watchly_vpc": false, "registered_as": null}
  ]
}
```
`200` · `403` · `404` · `502` AWS did not answer

### `POST /vpcs`
Body: `account_id`, `aws_vpc_id`, and optionally `name` (default: its `Name`
tag, with the VPC id added if the account already has a VPC of that name),
`description`, `region` (default the account's, else Watchly's own). The VPC
belongs to the account's project, and is read, synced and probed through AWS
with that account's credentials from then on. The Add resources form calls
this when someone first picks a VPC.

```json
{"account_id": 1, "aws_vpc_id": "vpc-0a1b2c3d4e5f60718", "region": "ap-southeast-1"}
```
Watchly reads the VPC's CIDR blocks from AWS; they must be private ranges.
`201` · `403` no rights over the account's project · `404` no such account ·
`409` already registered from this account, or name taken · `422` no such VPC in that region, or the account's
credentials cannot see it · `502`

### `GET /vpcs/{vpc_id}`
`200` · `404`

### `PATCH /vpcs/{vpc_id}`
`name`, `description`. The rest comes from AWS.
`200` · `403` · `404` · `409` name taken

### `DELETE /vpcs/{vpc_id}`
Without `with_resources`, only when it holds no resource: `409` names them.
With `?with_resources=true`, every resource in it stops being monitored
first, its checks, history and events deleted with it. Nothing changes in AWS.
`204` · `403` · `404` · `409` (still holds resources, without `with_resources`)

### `POST /vpcs/{vpc_id}/test`
Runs now and stores the result as the VPC's last test. No body.

```json
{
  "ok": false,
  "tested_at": "2026-10-04T09:12:00Z",
  "steps": [
    {"step": "placement", "ok": true, "detail": "Watchly runs in vpc-0a1b2c3d4e5f60718, subnet-0e1f2a3b4c5d6e7f8, 10.20.0.25"},
    {"step": "aws", "ok": true, "time_ms": 140, "detail": "DescribeVpcs answered; ranges 10.20.0.0/16"},
    {"step": "reach", "ok": false, "detail": "4 of 5 resources answered; report-runner 10.20.21.55:22 timed out"}
  ]
}
```
`reach` opens a TCP connection to up to 5 of the VPC's resources, one per
subnet where it can, always at their private addresses. Internet-facing load
balancers are left out: reaching them says nothing about reaching into the VPC.
`200` · `403` · `404` · `429` rate-limited (`RATE_LIMIT_INFRA_DIAGNOSE`)

### `POST /vpcs/{vpc_id}/discover`
Lists the VPC's resources through the instance role. It takes a few seconds,
so results are cached for `AWS_DISCOVERY_CACHE_SECONDS`; send
`{"refresh": true}` to skip the cache.

```json
{
  "discovered_at": "2026-10-04T09:15:00Z",
  "items": [
    {
      "key": "ec2:i-0f1e2d3c4b5a69788",
      "kind": "server",
      "aws_id": "i-0f1e2d3c4b5a69788",
      "name": "bastion",
      "detail": "t3.micro · running · public 203.0.113.25",
      "address": "10.20.0.40",
      "public": true,
      "public_ip": "203.0.113.25",
      "subnet": "public-1a",
      "tags": {"env": "production", "team": "platform"},
      "monitored_by": [{"resource_id": 20, "project": {"id": 3, "name": "Orders platform"}}],
      "suggested": [{"check_type": "ping"}, {"check_type": "tcp", "settings": {"port": 22}}]
    },
    {
      "key": "elb:orders-api",
      "kind": "load_balancer",
      "aws_id": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:loadbalancer/app/orders-api/50dc6c495c0c9188",
      "name": "orders-api",
      "detail": "ALB · internal · HTTP:80 · 1 target group",
      "address": "internal-orders-api-1234567890.ap-southeast-1.elb.amazonaws.com",
      "public": false,
      "target_groups": [{"arn": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:targetgroup/orders-api-tg/73e2d6bc24d8a067", "name": "orders-api-tg", "targets": 3, "health_check": "HTTP /health on 8080, 200"}],
      "monitored_by": [],
      "suggested": [
        {"check_type": "http", "settings": {"port": 80, "path": "/health"}},
        {"check_type": "target_health", "settings": {"target_group_arn": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:targetgroup/orders-api-tg/73e2d6bc24d8a067"}}
      ]
    },
    {
      "key": "elb:ledger-tcp",
      "kind": "load_balancer",
      "aws_id": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:loadbalancer/net/ledger-tcp/6c2f1a0b9d8e7f65",
      "name": "ledger-tcp",
      "detail": "NLB · internal · TCP:7000 · 1 target group",
      "address": "ledger-tcp-0a1b2c3d4e5f6789.elb.ap-southeast-1.amazonaws.com",
      "public": false,
      "monitored_by": [],
      "suggested": [
        {"check_type": "tcp", "settings": {"port": 7000, "tls": false}},
        {"check_type": "target_health", "settings": {"target_group_arn": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:targetgroup/ledger-tg/1f2e3d4c5b6a7980"}}
      ]
    },
    {
      "key": "asg:orders-api-asg",
      "kind": "auto_scaling_group",
      "aws_id": "orders-api-asg",
      "name": "orders-api-asg",
      "detail": "3 of 3 healthy · min 2, max 6 · ELB health check · 1 target group",
      "address": null,
      "public": false,
      "aws_state": "3 of 3 in service",
      "instances": [
        {"id": "i-0a14c2e3f4a5b6c7d", "az": "ap-southeast-1a", "lifecycle_state": "InService", "health_status": "Healthy",
         "private_ip": "10.20.12.30", "public_ip": null, "launch_time": "2026-10-03T22:10:00+00:00"}
      ],
      "monitored_by": [],
      "suggested": [
        {"check_type": "group_health", "name": "group health", "settings": {}},
        {"check_type": "http", "name": "GET /healthz on each instance", "settings": {"scheme": "http", "port": 8080, "path": "/healthz", "expected_status": 200}},
        {"check_type": "target_health", "name": "targets of orders-api-tg", "settings": {"target_group_arn": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:targetgroup/orders-api-tg/73e2d6bc24d8a067"}}
      ]
    },
    {
      "key": "ec2:i-0d4c3b2a1f0e9d8c7",
      "kind": "server",
      "aws_id": "i-0d4c3b2a1f0e9d8c7",
      "name": "batch-tmp",
      "detail": "t3.medium · running · private",
      "address": "10.20.31.61",
      "public": false,
      "auto_scaling_group": null,
      "monitored_by": [],
      "suggested": [{"check_type": "ping"}, {"check_type": "tcp", "settings": {"port": 22}}]
    }
  ],
  "skipped": [{"service": "elbv2", "call": "DescribeTargetHealth", "reason": "AccessDenied"}]
}
```
A call the role may not make is listed in `skipped` and does not fail the
discovery. Suggested checks of a server go to its private IP; to check a
public one at its public IP, add `"use_public_ip": true` to their `settings`
on import (the add form's "Check its public IP" does this).
`200` · `403` · `404` · `502` AWS did not answer

### `POST /vpcs/{vpc_id}/import`
Body: `project_id`, optional `environment` (default: the VPC's account's), and
`items`, each a discovered
`key`, optionally a `name`, and its `checks`. An item without `checks` gets the
suggested ones.

```json
{
  "project_id": 3,
  "environment": "production",
  "items": [
    {"key": "ec2:i-0f1e2d3c4b5a69788", "checks": [{"check_type": "tcp", "settings": {"port": 22, "use_public_ip": true}}]},
    {"key": "elb:orders-api"},
    {"key": "ec2:i-0d4c3b2a1f0e9d8c7", "name": "batch-tmp"}
  ]
}
```
All or nothing: every item is validated like `POST /resources` first, and the
resources are created in one transaction.
`201` `{"created": [<resource>, …]}` · `403` · `404` · `409` already
monitored, with the items listed · `422` with the failing items listed

### `GET /vpcs/{vpc_id}/topology`
What the VPC map draws, for anyone who can see the VPC. Read from AWS (the
discovery listing, cached for `AWS_DISCOVERY_CACHE_SECONDS`; subnets, route
tables and target health, cached for a minute; `?refresh=true` asks again) and
laid over the resources the caller can see. Unmonitored resources are included
with the discovery `key` that adds them.

- `subnets`: each `public` when its route table sends `0.0.0.0/0` to an
  internet gateway; `null` when that is unknown.
- `nodes`: `internet`, `watchly`, `aws_api`, and `lb:<arn>`, `tg:<arn>`,
  `asg:<name>`, `i:<instance id>`, `ip:<address>`. A monitored one has
  `resource_id` and `state`; `aws_health` is what AWS itself says (target
  health, Auto Scaling health). An instance has the `subnet` it sits in; a
  target group its `health_check` (`HTTP /health on 80, 200`).
- `edges`: `internet` (to what is public), `forwards` (load balancer → target
  group), `targets` (target group → target, with its target state), `registers`
  (Auto Scaling group → the target groups it registers its instances in),
  `launches` (Auto Scaling group → instance), `probes` (Watchly → a monitored resource,
  `via` `vpc` or `internet`, coloured by its checks) and `api` (Watchly → AWS
  API → what `target_health` and `group_health` read). `state` is `ok`,
  `degraded`, `failing`, `unknown` or `off` (paused, in maintenance, missing,
  or a stopped target).
- `watchly`: `in_vpc`, `outside` (another VPC: reached through peering or a
  transit gateway) or `not_on_ec2`.

```json
{
  "vpc_id": 3,
  "read_at": "2026-10-05T07:40:12Z",
  "watchly": "in_vpc",
  "subnets": [
    {"id": "subnet-0e1f2a3b4c5d6e7f8", "label": "public-1a", "cidr": "10.20.0.0/24", "az": "ap-southeast-1a", "public": true},
    {"id": "subnet-0a9b8c7d6e5f4a3b2", "label": "private-app-1b", "cidr": "10.20.12.0/24", "az": "ap-southeast-1b", "public": false}
  ],
  "nodes": [
    {"id": "watchly", "kind": "watchly", "label": "Watchly", "address": "10.20.0.25", "subnet": "subnet-0e1f2a3b4c5d6e7f8"},
    {"id": "lb:arn:aws:elasticloadbalancing:…:loadbalancer/app/orders-api/50dc6c495c0c9188", "kind": "load_balancer", "label": "orders-api", "resource_id": 12, "state": "degraded"},
    {"id": "tg:arn:aws:elasticloadbalancing:…:targetgroup/orders-api-tg/73e2d6bc24d8a067", "kind": "target_group", "label": "orders-api-tg", "aws_health": "degraded", "detail": "2 of 3 healthy", "health_check": "HTTP /health on 8080, 200"},
    {"id": "i:i-0b2e5d6c7b8a9f0e1", "kind": "instance", "label": "orders-api-2", "subnet": "subnet-0a9b8c7d6e5f4a3b2", "aws_health": "unhealthy"},
    {"id": "i:i-0d4c3b2a1f0e9d8c7", "kind": "instance", "label": "batch-tmp", "key": "ec2:i-0d4c3b2a1f0e9d8c7"}
  ],
  "edges": [
    {"source": "lb:arn:…/orders-api/50dc6c495c0c9188", "target": "tg:arn:…/orders-api-tg/73e2d6bc24d8a067", "kind": "forwards", "state": "degraded", "label": "HTTP:8080"},
    {"source": "tg:arn:…/orders-api-tg/73e2d6bc24d8a067", "target": "i:i-0b2e5d6c7b8a9f0e1", "kind": "targets", "state": "failing", "detail": "unhealthy: Health checks failed with these codes: [502]"},
    {"source": "watchly", "target": "lb:arn:…/orders-api/50dc6c495c0c9188", "kind": "probes", "via": "vpc", "state": "ok", "label": "GET /health"}
  ],
  "skipped": [],
  "aws_error": null
}
```
`200` · `404`. When AWS lists nothing, the map holds the monitored resources
only, from what Watchly last read, and `aws_error` says why.

---

## 8. Resources and checks

Every resource comes back as (an internal ALB here):

```json
{
  "id": 12,
  "project": {"id": 3, "name": "Orders platform"},
  "vpc": {"id": 1, "name": "prod-vpc"},
  "kind": "load_balancer",
  "name": "orders-api",
  "environment": "production",
  "aws_id": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:loadbalancer/app/orders-api/50dc6c495c0c9188",
  "aws_arn": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:loadbalancer/app/orders-api/50dc6c495c0c9188",
  "address": "internal-orders-api-1234567890.ap-southeast-1.elb.amazonaws.com",
  "aws_state": "active",
  "aws_detail": {
    "type": "application", "scheme": "internal",
    "listeners": [{"protocol": "HTTP", "port": 80}],
    "target_groups": [{"arn": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:targetgroup/orders-api-tg/73e2d6bc24d8a067",
                       "name": "orders-api-tg", "protocol": "HTTP", "port": 8080, "health_check": "HTTP /health on 8080, 200"}],
    "security_groups": ["sg-0db7c1e9f2a3b4c5d"], "azs": ["ap-southeast-1a", "ap-southeast-1b"]
  },
  "synced_at": "2026-10-04T09:10:00Z",
  "is_enabled": true,
  "state": "degraded",
  "down_since": null,
  "degraded_since": "2026-10-04T09:04:00Z",
  "problems": [
    {"kind": "targets_unhealthy", "check_id": 31, "since": "2026-10-04T09:04:00Z",
     "detail": "orders-api-tg: 2 of 3 targets healthy; i-0b2e5d6c7b8a9f0e1 failing with Target.ResponseCodeMismatch"}
  ],
  "max_down_alerts": 4,
  "checks": [
    {
      "id": 31,
      "check_type": "target_health",
      "name": "targets of orders-api-tg",
      "is_enabled": true,
      "check_interval_seconds": 60,
      "timeout_seconds": 10,
      "retries_on_failure": 1,
      "settings": {"target_group_arn": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:targetgroup/orders-api-tg/73e2d6bc24d8a067",
                   "min_healthy_targets": 1},
      "health": "degraded",
      "last_checked_at": "2026-10-04T09:14:31Z",
      "last_result": {"ok": true, "response_time_ms": 84, "summary": "orders-api-tg: 2 of 3 targets healthy"},
      "snapshot": {"target_group": "orders-api-tg", "healthy": 2, "total": 3, "unhealthy": ["i-0b2e5d6c7b8a9f0e1"]}
    }
  ],
  "maintenance": null,
  "extra_recipients": [],
  "created_by_id": 1,
  "created_at": "2026-10-01T08:30:00Z"
}
```
A server's `aws_detail` has `instance_type`, `az`, `subnet`, `private_ip`,
`public_ip` and `public_dns` (null for a private one), `security_groups`,
`auto_scaling_group` and `tags`. An Auto Scaling group's has `min_size`,
`max_size`, `desired_capacity`, `health_check_type`,
`health_check_grace_period`, `launch_template`, `subnets`, `target_groups`
(with each health check's protocol, port, path and matcher), `instances` (as
in discovery, as of the last sync) and `tags`. Its checks that run on each
instance keep, in `snapshot`, `healthy`, `total`, `warming`, `desired` and
`instances`: each one's `id`, `address`, `ok`, `summary` and
`response_time_ms` from the latest run.
`snapshot` is the latest of each check's figures, for the detail page's cards;
the full record of every run is in `/results`.

### `GET /resources`
Query: `limit`, `offset`, `vpc_id`, `project_id`, `kind`, `state`,
`environment`, `q` (name, AWS id or address contains), `sort` (`state` puts
down first, then degraded, then the rest; or `name`).
`200`

### `GET /resources/summary`
Counts per state, taking the same filters as the list.
`200` `{"total": 23, "healthy": 17, "degraded": 2, "down": 1, "unknown": 1, "maintenance": 1, "paused": 1, "missing": 0}`

### `GET /resources/events`
The infrastructure event feed, like `/monitoring/websites/events`: the
notification kinds of [§10](#10-notifications), each with its resource (or
VPC; neither for a deployment) and time. Query: `since`, `limit`.
`200`

### `POST /resources`
Body: `project_id`, `vpc_id`, `kind`, `aws_id`, and optionally `name` (default:
its `Name` tag or identifier), `environment` (default: the account's), `max_down_alerts`, and `checks`
(default: the suggested ones, as in discovery).

```json
{
  "project_id": 3,
  "vpc_id": 1,
  "kind": "load_balancer",
  "aws_id": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:loadbalancer/app/orders-api/50dc6c495c0c9188",
  "environment": "production",
  "checks": [
    {"check_type": "http", "settings": {"path": "/health"}},
    {"check_type": "target_health", "settings": {
      "target_group_arn": "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:targetgroup/orders-api-tg/73e2d6bc24d8a067",
      "min_healthy_targets": 2}}
  ]
}
```
Watchly looks the resource up in AWS, checks it is in that VPC, and reads its
address and details.
`201` · `403` no rights over the project · `404` VPC · `409` already monitored
(naming the project, if the caller can see it) · `422` not found in that VPC,
the wrong `kind`, a VPC of another project, or an invalid check (see
K1) · `502` AWS did not answer

### `GET /resources/{resource_id}`
`200` · `404`

### `PATCH /resources/{resource_id}`
`name`, `environment`, `max_down_alerts`, `is_enabled` (pause or resume).
`kind`, `vpc_id`, `aws_id` and the project are fixed.

For an Auto Scaling group, also the opt-in scaling notifications (all off or
null by default; `422` on any other kind):

| field | meaning |
| ----- | ------- |
| `notify_scale_out` | send `asg_scaled_out` when instances join the group |
| `notify_scale_in` | send `asg_scaled_in` when instances leave it |
| `scale_health_check` | `{scheme, port, path, expected_status, use_public_ip, verify_tls}`, or `null` to remove it. Probed (with 2 retries) on each instance that joined; the scale-out message says whether it answered. Ignored by scale-in. |

Membership is compared at each VPC sync (`AWS_SYNC_INTERVAL_SECONDS`), over
the instances `InService`: one that joins and leaves between two syncs is not
reported, and a group's first sync only records its instances. A paused group,
or one in maintenance, says nothing. The same three fields can be sent on
`POST /resources`.
`200` · `403` · `404` · `422`

### `DELETE /resources/{resource_id}`
Deletes its checks, results, events and maintenance windows too.
`204` · `403` · `404`

### `POST /resources/{resource_id}/check`
Runs every enabled check now, records the results through the usual state
machine, and returns the resource. Like websites' `/check`.
`200` · `403` · `404` · `409` paused, missing, or in maintenance · `503` a
probe cannot run here (no ICMP permission; see `/self`)

### `GET /resources/{resource_id}/results`
Query: `check_id`, `limit`, `offset`, `since`. Newest first. Each result has
`id`, `check_id`, `checked_at`, `ok`, `response_time_ms`, `dns_ms`,
`connect_ms`, `tls_ms`, `error_type` and `error`, so the time-breakdown chart
works as for websites, plus one group for its type:

```json
// ping
"ping": {"address": "10.20.21.55", "sent": 3, "received": 0, "loss_percent": 100, "rtt_avg_ms": null}

// tcp
"tcp": {"address": "10.20.21.55", "port": 22, "banner": null}

// http
"http": {"address": "10.20.12.7", "url": "http://internal-orders-api-1234567890.ap-southeast-1.elb.amazonaws.com/health",
         "status_code": 200, "body_bytes": 15}

// ping, tcp, http of an Auto Scaling group: one row per instance checked
"instances": [
  {"id": "i-0a14c2e3f4a5b6c7d", "az": "ap-southeast-1a", "address": "10.20.12.30", "ok": true,
   "summary": "HTTP 200 in 6 ms", "error_type": null, "response_time_ms": 6},
  {"id": "i-0b2e5d6c7b8a9f0e1", "az": "ap-southeast-1b", "address": "10.20.13.31", "ok": false,
   "summary": "HTTP 503 Service Unavailable", "error_type": "unexpected_status", "response_time_ms": 4}
]

// target_health
"targets": [
  {"id": "i-0a14c2e3f4a5b6c7d", "port": 8080, "az": "ap-southeast-1a", "state": "healthy"},
  {"id": "i-0b2e5d6c7b8a9f0e1", "port": 8080, "az": "ap-southeast-1b", "state": "unhealthy",
   "reason": "Target.ResponseCodeMismatch", "description": "Health checks failed with these codes: [502]"},
  {"id": "i-0c3f9e8d7c6b5a4f2", "port": 8080, "az": "ap-southeast-1c", "state": "healthy"}
]
```
For `target_health`, `response_time_ms` is the AWS API call's time.
`200` · `404`

### `GET /resources/{resource_id}/stats`
Query: `range` (`24h`, `7d`, `30d`, `90d`), `format` (`json`, `csv`). Read from
hourly rollups, like websites' stats, with one series per check:

```json
{
  "range": "24h",
  "checks": [
    {
      "check_id": 31, "check_type": "target_health", "metric": "healthy_targets",
      "buckets": [{"start": "2026-10-04T08:00:00Z", "uptime_percent": 100.0, "response_avg_ms": 84, "response_max_ms": 140, "metric_min": 2, "metric_max": 3}]
    }
  ]
}
```
`metric` is the type's own figure: `packet_loss_percent` (ping),
`healthy_targets` (target health, with `metric_min` as well),
`healthy_instances` (an Auto Scaling group's `group_health`, and its checks
that run on each instance, with `metric_min` as well), `cpu_percent` (a
database's `db_metrics`, a server's `ec2_metrics`), or null.
`200` · `404`

### `GET /resources/{resource_id}/targets`
Load balancers and Auto Scaling groups. For each `target_health` check, each registered target's
state now and its changes over the last 24 hours, read from the results:

```json
{
  "target_groups": [
    {
      "check_id": 31, "name": "orders-api-tg", "health_check": "HTTP /health on 8080, 200",
      "targets": [
        {
          "id": "i-0b2e5d6c7b8a9f0e1", "name": "orders-api-2", "address": "10.20.12.31", "port": 8080, "az": "ap-southeast-1b",
          "state": "unhealthy", "since": "2026-10-04T09:02:00Z",
          "reason": "Target.ResponseCodeMismatch", "description": "Health checks failed with these codes: [502]",
          "timeline": [{"from": "2026-10-03T09:15:00Z", "to": "2026-10-04T09:02:00Z", "state": "healthy"},
                       {"from": "2026-10-04T09:02:00Z", "to": null, "state": "unhealthy"}]
        }
      ]
    }
  ]
}
```
`200` · `404` · `409` neither a load balancer nor an Auto Scaling group

### Deployments

An account with `watch_deployments` is asked every
`DEPLOY_WATCH_INTERVAL_SECONDS` (60) which CodeDeploy deployments are running,
in each region it has VPCs in and its default region
(`app/monitoring/infra/aws/deployments.py`):

| CodeDeploy | Watchly |
| ---------- | ------- |
| a deployment starts | `deploy_started` to the project ("Deploying orders-api to production"); each monitored resource it deploys to gets a maintenance window tied to it, so it is not checked and alerts nobody |
| it keeps running | the windows are renewed `DEPLOY_SILENCE_LEASE_MINUTES` (10) ahead, never past `DEPLOY_SILENCE_MAX_MINUTES` (180) from when Watchly first saw it |
| it succeeds, fails or is stopped | `deploy_finished`, replying in the start's Slack thread and Telegram chain; the windows end `DEPLOY_SETTLE_SECONDS` (60) later, and a resource still down then alerts as usual |

The resources come from its deployment group: its Auto Scaling groups and their
servers, the servers its EC2 tag filters match (by the tags the last sync read),
and the load balancers and groups using its target groups, or the target groups
its groups are registered with. Databases are never silenced. If the group
cannot be read, every server, load balancer and Auto Scaling group of the
account's region is silenced, and the announcement says so. The environment in
the message is its resources' when they agree on one, else the account's.

The lease is short so that a deployment Watchly loses sight of (credentials
broken, Watchly down) silences nothing after a few minutes. A window ended by
hand (**End now**) is not renewed. Deployments CodeDeploy runs on its own for
an instance an Auto Scaling group launches (`creator` `autoscaling`) are left
alone; automatic rollbacks are deployments of their own, and are announced.
A deployment that starts and ends between two looks is not seen.

### Capacity

An account with `watch_capacity` is looked at every
`CAPACITY_WATCH_INTERVAL_SECONDS` (3600), in each region it has VPCs in and its
default region (`app/monitoring/infra/aws/capacity.py`), with one
`DescribeAddresses` and the region's Elastic IP quota from Service Quotas
(`L-0263D0A3`, "EC2-VPC Elastic IPs": the account's own, else AWS's default,
else 5, said to be assumed):

| Found | Watchly |
| ----- | ------- |
| Elastic IPs associated with nothing, on two looks in a row (one moved between instances is not) | `account_capacity` (`eip_unattached`) to the project, listing them with what they cost: AWS bills every public IPv4 address, about US$3.65 a month. Again only when another one is left unattached. |
| the region's Elastic IPs at `CAPACITY_QUOTA_PERCENT` (80) of its quota or more | `account_capacity` (`eip_quota`) as it opens; if it clears and comes back, again at most every `INFRA_PROBLEM_ALERT_COOLDOWN_SECONDS` |

What each look found and the open problems are kept on the account
(`aws_accounts.capacity`), so the AWS accounts page shows them and a restart
forgets nothing. A region AWS will not answer for keeps what was last found
there, and `capacity_error` says why.

### `GET /deployments`
Query: `project_id`, `active` (`true` running, `false` ended), `limit` (20),
`offset`. The deployments of the projects the caller can see, newest first.

```json
{
  "items": [{
    "id": 7,
    "project": {"id": 3, "name": "Orders platform"},
    "account": {"id": 2, "name": "Production", "aws_account_id": "123456789012"},
    "region": "ap-southeast-1",
    "deployment_id": "d-7Q2X9ABCD",
    "application_name": "orders-api",
    "group_name": "orders-api-prod",
    "status": "InProgress",
    "environment": "production",
    "creator": "user",
    "description": "Release 2.4.1",
    "revision": "github acme/orders-api@4f1c2d9",
    "resources": [{"id": 30, "name": "orders-api-asg", "kind": "auto_scaling_group"}],
    "fallback": null,
    "started_at": "2026-10-05T09:12:00Z",
    "finished_at": null,
    "error": null,
    "is_active": true
  }],
  "total": 1, "limit": 20, "offset": 0
}
```
`status` is CodeDeploy's (`Created`, `Queued`, `InProgress`, `Baking`, `Ready`,
`Succeeded`, `Failed`, `Stopped`), or `Unwatched`. Kept for
`CHECK_RETENTION_DAYS` after they end.

### Recipients and maintenance

`POST /resources/{resource_id}/recipients`, `DELETE …/recipients/{user_id}`,
`POST …/maintenance`, `POST …/maintenance/end` and
`DELETE …/maintenance/{window_id}` take the same bodies and answer the same way
as their `/monitoring/websites/{website_id}/…` counterparts. While a window is
in effect, the resource is not checked; a check run by hand is recorded but
changes no state and alerts nobody.

### `POST /resources/{resource_id}/checks`
Body: `check_type`, and optionally `name`, `settings`,
`check_interval_seconds`, `timeout_seconds`, `retries_on_failure`,
`is_enabled`. Defaults as for websites: interval 300 s, timeout 10 s.

`settings` per type, with defaults:

```json
// ping (servers)
{"count": 3, "packet_loss_threshold_percent": 20, "use_public_ip": false}

// tcp (port required; for a load balancer, one of its listeners')
{"port": 22, "tls": false, "expect_banner": null, "use_public_ip": false}

// http (Watchly fills in the host: the server's private or public IP, or the load balancer's DNS name)
{"scheme": "http", "port": 80, "path": "/health", "host_header": null,
 "expected_status": 200, "must_contain": null, "slow_threshold_ms": 3000,
 "verify_tls": false, "use_public_ip": false}

// target_health (load balancers and groups; the target group must be one of this resource's)
{"target_group_arn": "arn:aws:elasticloadbalancing:…", "min_healthy_targets": 1}

// group_health (Auto Scaling groups)
{"min_healthy_instances": 1}

// db_status (databases): nothing to set
{}

// db_metrics (databases; null turns a threshold off; max_connections null reads
// it from the parameter group)
{"cpu_percent_max": 90, "free_storage_percent_min": 10, "storage_full_days_min": 14,
 "freeable_memory_mb_min": null, "connections_max": null, "connections_percent_max": 80,
 "max_connections": null, "iops_percent_max": 90, "burst_balance_percent_min": 20,
 "cpu_credits_percent_min": 10, "replica_lag_seconds_max": 60}

// ec2_metrics (servers; null turns a threshold off)
{"cpu_percent_max": 90, "cpu_credits_percent_min": 10, "burst_balance_percent_min": 20,
 "iops_percent_max": 90, "disk_used_percent_max": 90}
```
On an Auto Scaling group, `ping`, `tcp` and `http` also take
`min_healthy_instances` (null by default: down only when no instance passes),
and run on every instance in service; `use_public_ip` sends them to each
instance's public IP.
`use_public_ip` sends a server's check to its public IP, over the internet,
instead of its private IP; only a server with a public IP takes it.
`host_header` is for ALB rules that route by host. Setting a threshold to null
turns that problem off for the check.

`201` · `403` · `404` · `409` the same check already exists on this resource ·
`422` a type the resource's kind does not take, a `tcp` port that is not one of
a load balancer's listeners, `use_public_ip` on a load balancer or on a server
without a public IP, `min_healthy_instances` on anything but an Auto Scaling
group's check, a target group that is not the resource's, a `settings` field
the type does not take, or more than `INFRA_MAX_CHECKS_PER_RESOURCE` checks

### `PATCH /resources/{resource_id}/checks/{check_id}`
The same fields; `check_type` is fixed.
`200` · `403` · `404` · `409` · `422`

### `DELETE /resources/{resource_id}/checks/{check_id}`
`204` · `403` · `404` · `409` the resource's last check; delete the resource
instead

---

## 9. Diagnose, overview and self

### `POST /diagnose`
A dry run of a check, step by step. The body is either an existing check,
`{"resource_id": 15, "check_id": 22}`, or an unsaved one for the add form:
`vpc_id`, `kind`, `aws_id`, `project_id`, and the check's fields as for K1.

```json
{
  "ok": false,
  "failed_step": "connect",
  "steps": [
    {"step": "resolve", "ok": true, "detail": "10.20.21.55 is an IP address"},
    {"step": "policy", "ok": true, "detail": "10.20.21.55 is inside prod-vpc (10.20.0.0/16)"},
    {"step": "connect", "ok": false, "time_ms": 5000, "error_type": "connect_timeout",
     "detail": "10.20.21.55:8080: No answer: the packets were dropped (security group, NACL or route)."},
    {"step": "tls", "skipped": true},
    {"step": "banner", "skipped": true}
  ],
  "hints": [
    {
      "kind": "security_group",
      "resource": "sg-0db7c1e9f2a3b4c5d",
      "detail": "report-runner's security group has no inbound rule for TCP 8080 from sg-0a7e3c2194b1d5f60 (watchly).",
      "fix": "Add an inbound rule: TCP 8080, source sg-0a7e3c2194b1d5f60."
    }
  ]
}
```
Steps by type: `ping`: resolve, policy, echo. `tcp`: resolve, policy, connect,
tls, banner. `http`: resolve, policy, connect, tls, response. `target_health`:
aws_api, targets. `group_health`: aws_api, instances. `db_status`: aws_api,
status. `db_metrics` and `ec2_metrics`: aws_api, metrics. An Auto Scaling group's
`ping`, `tcp` and `http`: aws_api, then one step per instance (named by its
id, with its address and result; skipped while it launches or warms up). For a check over the internet, the policy step says the
address is public rather than inside the VPC.

`hints` need an Admin or DevOps caller (they show the network's layout); for
anyone else the list is empty. They say where to look when a `ping`, `tcp` or
`http` check cannot connect, one entry per place, `kind` being:

| `kind` | When | `resource` | What it says |
| - | - | - | - |
| `security_group` | a timeout, and no inbound rule of the target's groups lets Watchly in | the group to change | the rule to add: protocol and port, and the source (Watchly's security group inside one VPC, else its address as `/32`) |
| `security_group` | the target's groups allow it, but Watchly's own groups have no outbound rule | Watchly's group | the outbound rule to add |
| `security_group` | the groups could not be read (no `ec2:DescribeNetworkInterfaces` or `DescribeSecurityGroups`), a prefix list may cover Watchly, or Watchly does not run on EC2 | the group to open | which rule to look for in it |
| `network_acl` | a timeout, and the groups already allow it | the subnet | the network ACLs (inbound the port, outbound replies on 1024-65535) and the host's own firewall |
| `route` | the same, and Watchly runs outside the target's VPC | the VPC | the route tables on both sides of the peering or transit gateway |
| `port` | connection refused: the host answered, so it is not a security group | `host:port` | nothing listens there: start the service, bind it to the private IP, or use a port the resource is known to use (a load balancer's listeners, a database's port) |
| `port` | the TLS handshake failed | `host:port` | the port may not speak TLS: turn TLS off for the check, or use the TLS port |

For an Auto Scaling group, up to three failing instances are explained, and
hints naming the same group are merged. The target's groups come from its
network interface (`ec2:DescribeNetworkInterfaces`, `DescribeSecurityGroups`),
else from the last sync. Network ACLs and route tables are pointed at, not
evaluated: VPC Reachability Analyzer would, but AWS bills it per analysis.
`200` either way, the outcome is in `ok` · `403` no rights over the project, or
a VPC of another project · `404` · `422` invalid fields · `429` rate-limited

### `GET /overview`
Everything the Infrastructure page's top half needs, in one call, scoped to
what the caller can see. Query: `vpc_id`, `project_id`, `environment`.

```json
{
  "counts": {"total": 23, "healthy": 17, "degraded": 2, "down": 1, "unknown": 1, "maintenance": 1, "paused": 1, "missing": 0},
  "vpcs": [
    {"id": 1, "name": "prod-vpc", "health": "reachable", "cidrs": ["10.20.0.0/16"],
     "counts": {"total": 14, "healthy": 9, "degraded": 2, "down": 1, "unknown": 0, "maintenance": 1, "paused": 1, "missing": 0}},
    {"id": 2, "name": "staging-vpc", "health": "reachable", "cidrs": ["10.30.0.0/16"],
     "counts": {"total": 9, "healthy": 8, "degraded": 0, "down": 0, "unknown": 1, "maintenance": 0, "paused": 0, "missing": 0}}
  ],
  "by_kind": [
    {"kind": "server", "total": 17, "down": 1, "degraded": 1},
    {"kind": "load_balancer", "total": 6, "down": 0, "degraded": 1}
  ],
  "attention": [
    {"resource": {"id": 15, "name": "report-runner", "kind": "server", "vpc": {"id": 1, "name": "prod-vpc"}},
     "state": "down", "since": "2026-10-04T08:58:00Z", "summary": "ping: no reply; tcp 22: timed out after 5 s"},
    {"resource": {"id": 20, "name": "bastion", "kind": "server", "vpc": {"id": 1, "name": "prod-vpc"}},
     "state": "degraded", "since": "2026-10-04T08:41:00Z", "summary": "33% of pings lost (limit 20%)"},
    {"resource": {"id": 12, "name": "orders-api", "kind": "load_balancer", "vpc": {"id": 1, "name": "prod-vpc"}},
     "state": "degraded", "since": "2026-10-04T09:02:00Z", "summary": "orders-api-tg: 2 of 3 targets healthy; i-0b2e5d6c7b8a9f0e1 failing with 502"}
  ]
}
```
`attention` lists down resources first, then degraded and missing ones, oldest
first, at most 20. The table under it is the ordinary `GET /resources`.
`200`

### `GET /self`
Where Watchly runs and what it is allowed to do. The API process reads it from
the instance metadata service itself; the egress policy does not apply, since
this is not a probe.

```json
{
  "instance": {
    "id": "i-0f3c9a12d4e5b6a78", "type": "t3.small", "region": "ap-southeast-1", "az": "ap-southeast-1a",
    "vpc_id": "vpc-0a1b2c3d4e5f60718", "subnet_id": "subnet-0e1f2a3b4c5d6e7f8",
    "private_ip": "10.20.0.25", "public_ip": "203.0.113.25",
    "security_groups": [{"id": "sg-0a7e3c2194b1d5f60", "name": "watchly"}],
    "iam_role": "watchly-monitor", "account_id": "123456789012"
  },
  "imds": {"tokens_required": true, "hop_limit": 2},
  "guard": {"metadata_blocked": true, "website_private_targets": "block", "deny_extra": []},
  "permissions": [
    {"action": "ec2:DescribeInstances", "ok": true},
    {"action": "elasticloadbalancing:DescribeTargetHealth", "ok": false, "detail": "AccessDenied"}
  ],
  "probes": {"ping": true, "tcp": true, "http": true, "target_health": true}
}
```
`instance` is null when Watchly does not run on EC2. Infrastructure monitoring
then cannot work, and the Infrastructure page says so. Each permission is
tested by making the read call once with the smallest page size.
`200` · `403`

### `GET /api/v1/monitoring/websites/blocked` *(phase 0)*
Existing website checks the egress policy refuses right now, each with
`website_id`, `name`, `address` and `reason`, plus the policy in effect
(`always_blocked`, `deny_extra`, `website_private_targets`). Use it before
turning the policy on.
`200` · `403` not Admin/DevOps

---

## 10. Notifications

New kinds, configured per channel and per project like the existing ones
(`GET/PUT /monitoring/notifications/{kind}`). Kinds are plain strings in
`notification_settings`, so adding them needs no migration. Their default
templates go in `catalog.py`.

| kind | sent when | placeholders added |
| ---- | --------- | ------------------ |
| `infra_down` | a resource goes down ([§3](#alerts-per-resource-not-per-check)) | `{{resource_name}}`, `{{resource_kind}}`, `{{aws_id}}`, `{{vpc_name}}`, `{{aws_state}}`, `{{failing_checks}}` |
| `infra_still_down` | a follow-up while it stays down | the same, and `{{downtime}}` |
| `infra_recovered` | every check is up again | the same, and `{{downtime}}` |
| `infra_degraded` | a problem opens; again after `INFRA_PROBLEM_ALERT_COOLDOWN_SECONDS` | `{{problem}}`, `{{problem_detail}}` |
| `asg_scaled_out` | opt-in: instances joined an Auto Scaling group | `{{instances}}`, `{{instance_count}}`, `{{in_service}}`, `{{desired}}`, `{{health_endpoint}}` |
| `asg_scaled_in` | opt-in: instances left an Auto Scaling group | the same |
| `vpc_unreachable` | most checks in a VPC fail to connect in one tick | `{{vpc_name}}`, `{{failed_checks}}`, `{{total_checks}}` |
| `vpc_recovered` | that VPC answers again | `{{vpc_name}}`, `{{downtime}}` |
| `deploy_started` | opt-in per account: a CodeDeploy deployment starts ([Deployments](#deployments)) | `{{environment}}`, `{{application}}`, `{{deployment_group}}`, `{{deployment_id}}`, `{{account}}`, `{{region}}`, `{{revision}}`, `{{initiated_by}}`, `{{resources}}`, `{{resource_count}}` |
| `deploy_finished` | that deployment succeeds, fails or is stopped | the same, and `{{status}}`, `{{duration}}`, `{{error}}` |
| `account_capacity` | opt-in per account: Elastic IPs attached to nothing, or the Elastic IP quota nearly used ([Capacity](#capacity)) | `{{account}}`, `{{aws_account_id}}`, `{{region}}`, `{{problem}}`, `{{problem_detail}}` |

The project placeholders and the dashboard link work as today. Recipients are
the project's, plus the resource's extra recipients, exactly as for a website.
In Slack and Telegram an outage is one thread, as it is for websites. Every
kind also lands in `GET /resources/events` and the toasts.

The monthly report stays websites-only in phase 1; an infrastructure section is
phase 2.

---

## 11. Data model, configuration and code layout

### Tables

```
aws_accounts             id, project_id FK CASCADE, name, UK (project_id, name), description, auth_type (default, access_key), access_key_id,
                         secret_access_key (encrypted), role_arn, external_id, default_region, environment,
                         aws_account_id, verified_at, last_error, watch_deployments,
                         deployments_checked_at, deployments_error, watch_capacity,
                         capacity_checked_at, capacity_error,
                         capacity jsonb   -- per region what was found, and open problems
                         created_by_id, timestamps
aws_vpcs                 id, account_id FK CASCADE, name, UK (account_id, name), description,
                         region, aws_vpc_id, UK (account_id, region, aws_vpc_id),
                         cidrs cidr[], is_watchly_vpc, unreachable_since, synced_at,
                         last_test_at, last_test jsonb, created_by_id, timestamps
aws_resources            id, project_id FK, vpc_id FK CASCADE, kind, aws_id, aws_arn, name,
                         environment, address, aws_state, aws_detail jsonb, synced_at, is_enabled,
                         state, down_since, degraded_since, max_down_alerts, down_alerts_sent,
                         slack_thread_ts, slack_thread_channel,
                         telegram_thread_message_id, telegram_thread_chat,
                         problems jsonb   -- open problems with their streak and last alert
                         created_by_id, timestamps; UK (vpc_id, aws_id)
aws_resource_recipients  resource_id PK, user_id PK
aws_checks               id, resource_id FK CASCADE, check_type, name, settings jsonb,
                         check_interval_seconds, timeout_seconds, retries_on_failure, is_enabled,
                         health, consecutive_failures, last_checked_at,
                         last_result jsonb, snapshot jsonb, timestamps
aws_check_results        id, check_id FK CASCADE, checked_at, ok, response_time_ms, dns_ms,
                         connect_ms, tls_ms, error_type, error,
                         detail jsonb     -- the ping / tcp / http / targets group
aws_check_hourly         check_id PK, hour PK, checks, failures, response_avg_ms,
                         response_max_ms, metric_min, metric_max
aws_events               id, vpc_id FK, resource_id FK null, kind, created_at, detail jsonb
aws_maintenance_windows  id, resource_id FK CASCADE, starts_at, ends_at, reason, created_by_id
enums                    aws_resource_kind (server, load_balancer, auto_scaling_group, database),
                         aws_check_type (ping, tcp, http, target_health, group_health,
                                         db_status, db_metrics, ec2_metrics)
```
Column names follow the `websites` table wherever the meaning is the same.
Migrations `0038_aws_vpcs`, `0039_aws_resources_checks`,
`0040_aws_scaling_notifications`, `0041_aws_accounts` (VPCs registered before
it move to an account named "Watchly's own credentials") and
`0042_project_monitors` (`projects.monitors`; each account goes to the project
that used it, copied for each further project, with the VPCs it used), then
`0043`–`0046` (account environments, databases, deployments) and
`0047_capacity_alerts` (`ec2_metrics`; the new `db_metrics` thresholds on every
existing check at their defaults; an account's capacity). The
cascades only act when a whole project is deleted: the API refuses to remove
an account or VPC that is still in use.
Phase 0 needs none.

### Configuration

| setting | default | what |
| ------- | ------- | ---- |
| `INFRA_AWS_ENABLED` | `false` | The `/monitoring/infra/aws` API, the infrastructure checks and sync, and the Infrastructure page. |
| `WEBSITE_PRIVATE_TARGETS` | `block` | Phase 0. `allow` lets website checks reach private addresses, as today. For LAN installs only, never on EC2. Metadata, loopback and Watchly's own addresses are blocked either way. |
| `EGRESS_DENY_CIDRS` | empty | Extra ranges nothing may reach, e.g. the subnet of Watchly's own database. |
| `AWS_REGION` | from the instance metadata | Default region for an account without one of its own. |
| `AWS_SYNC_INTERVAL_SECONDS` | `300` | How often resources' addresses and AWS states are refreshed. |
| `AWS_DISCOVERY_CACHE_SECONDS` | `300` | |
| `INFRA_MAX_CHECKS_PER_RESOURCE` | `10` | |
| `INFRA_PROBLEM_CHECKS` | `2` | Checks in a row before a problem counts (degraded, and alerted). |
| `INFRA_PROBLEM_ALERT_COOLDOWN_SECONDS` | `21600` | As `SLOW_ALERT_COOLDOWN_SECONDS`. |
| `VPC_UNREACHABLE_PERCENT` | `60` | Share of a VPC's checks failing to connect in one tick. |
| `VPC_UNREACHABLE_MIN_CHECKS` | `3` | Fewer checks than this never mark a VPC unreachable. |
| `CAPACITY_WATCH_INTERVAL_SECONDS` | `3600` | How often an account that watches its capacity has its Elastic IPs and quota looked at (min 300). |
| `CAPACITY_QUOTA_PERCENT` | `80` | The share of a region's Elastic IP quota in use that is `eip_quota`. |
| `RATE_LIMIT_INFRA_DIAGNOSE` | `30/minute` | Per user, for diagnose and VPC tests. |

`CHECK_RETENTION_DAYS` and `DEFAULT_MAX_DOWN_ALERTS` apply to infrastructure as
they do to websites. There are no AWS keys in the environment: Watchly's own
credentials are the instance role, and any other account's are stored with it
(see [Several AWS accounts](#several-aws-accounts)).

### New dependencies

`boto3`, called through `anyio.to_thread`.

### Where the code goes

The new package is `app/monitoring/infra/aws/`; its file layout is in its
[`README`](../app/monitoring/infra/aws/README.md). `app/monitoring/infra/`
holds one sub-package per cloud provider, and AWS is the only one. Outside the
package:

| Path | Change |
| ---- | ------ |
| `app/monitoring/egress.py` | New: `vet()`, the policy, and `vpc_scope()` for infrastructure probes. Phase 0. |
| `app/monitoring/websites/checker.py`, `pinger.py` | Call `vet()` before connecting. Phase 0. |
| `app/monitoring/websites/routes.py` | `GET /blocked`. Phase 0. |
| `app/monitoring/routes.py` | Includes the infrastructure router. |
| `app/monitoring/scheduler.py` | `run_tick` also runs due infrastructure checks, the sync, and the infrastructure rollup and purges, each under `_run_guarded`. |
| `app/monitoring/notifications/catalog.py` | The six new kinds, with their default templates and placeholders. |
| `app/core/config.py` | The settings above. |
| `alembic/env.py` | Imports `app.monitoring.infra.aws.models`. |
| `frontend/src/pages/Infrastructure.jsx`, `InfraVpc.jsx`, `InfraResourceDetail.jsx`, `NewInfraResource.jsx`, `InfraSettings.jsx` | New pages. `Layout.jsx` gains the sidebar entry; `EventToasts.jsx` also polls the infrastructure feed. |

---

## 12. Dashboard screens and the calls behind them

The sidebar gains **Infrastructure**, next to Websites, shown when
`INFRA_AWS_ENABLED` is on. Its settings tab is visible to Admin and DevOps only.

| Screen | What it shows | Calls |
| ------ | ------------- | ----- |
| **1. Infrastructure overview** (`/infra`) | A map of Watchly → AWS accounts → VPCs (default; each VPC coloured by reach and its resources' states), state cards (healthy, degraded, down, pending, maintenance, paused, missing), one card per VPC, counts by kind, a "needs attention" list, and the resource table with each kind's key figure | D2, R1, R2, R3 for toasts |
| **2. VPC** (`/infra/vpcs/:id`) | The network map (default) or a list by subnet, its project, the last test. The map: subnets as lanes (public or private), the internet, Watchly and the AWS API outside the VPC, load balancer → target group → targets, Auto Scaling group → instances, Watchly's probe paths coloured by their checks, unmonitored resources dashed with "Monitor this". Phase 2 adds a blocked path's security-group hint | V4, R1, V8, V11 |
| **3. Resource detail** (`/infra/resources/:id`) | Status strip, its checks with their health, AWS details. A **server**: ping, ports, `/healthz`, its private and public IPs, and which checks go to which. An **Auto Scaling group**: capacity (min, desired, max), health check type and grace period, an instances table with each instance's lifecycle, group health and latest result per check, and its target groups' targets. A **load balancer** (ALB or NLB, internal or internet-facing): the `/health` or listener result, the target grid with reason codes, the healthy-targets chart, per-target 24-hour timelines | R5, R9, R10, R11, D1 ("Why is this down?") |
| **4. Add infrastructure** (`/infra/new`) | Pick a VPC, choose discovered resources (or enter an AWS id), each marked public or private, review the suggested checks, for a public server choose its public or private IP, **Test connection** with the step-by-step result, save | V1, V9, D1, R4 or V10 |
| **5. AWS & access** (`/infra/settings`) | Where Watchly runs, IMDS and guard status, the IAM permission checklist, VPCs with their ranges and projects | D3, V1, V2 |

---

## 13. Decisions for review

**Decided (2026-10-04)**

- **Infrastructure is a separate entity from websites**, with its own package,
  tables, API and pages. The cost: results, rollups, events and maintenance
  windows get tables of their own, and the outage policy is ported from
  `MonitoringService` rather than shared. Projects, permissions, notification
  channels and settings, the scheduler and the egress policy are shared.
  Extracting a shared outage core can follow once both sides exist.
- **AWS only**, in the account Watchly runs in. In scope: EC2 servers, RDS
  databases, internal ALBs and their target health.

**Decided (2026-10-05)**

- **Servers and load balancers only.** RDS databases, the `postgres` and
  `mysql` checks, database credentials and their alerts (replication lag,
  connections) are out of scope for now.
- **Public as well as private.** EC2 servers in public subnets, with a check of
  their public IP as an option, and internet-facing load balancers. Their
  public addresses come from AWS, so the egress policy allows them for those
  checks only.
- **NLBs as well as ALBs.** An NLB takes `tcp` on one of its listeners and
  `target_health`; Gateway Load Balancers are left out.
- **Auto Scaling groups as resources.** A group's `ping`, `tcp` and `http`
  run on every instance in service, read from AWS at each run, which is what
  watching a scaling fleet's health API needs; `group_health` adds the group's
  own view and its capacity. Instances are not alerted on one by one.

**Open**

1. **Alert per resource or per check?** *Recommended: per resource.* A dead
   server should send one alert that lists its failing checks, not three.
2. **Separate notification kinds (`infra_*`) or reuse `down` / `recovered`?**
   *Recommended: separate.* The messages carry different facts (failing checks,
   AWS state), and admins may want infrastructure alerts on other channels than
   site alerts.
3. **Website checks and private addresses: `block` or `allow` by default?**
   *Recommended: `block`*, since an EC2 deployment is exactly where `allow` is
   dangerous, and private reach now belongs to infrastructure. It is a breaking
   change for anyone checking a LAN today, so it goes in the release notes,
   with `GET /monitoring/websites/blocked` to find the affected checks.
4. **Resources only from AWS, never typed-in addresses?** *Recommended: yes.*
   Addresses stay in sync with AWS, and a stopped or deleted resource is known
   as such. The cost: anything AWS cannot describe (an on-premises host over a
   VPN) cannot be watched. ECS services and other containers are watched
   through their load balancer's target groups.
5. **Project managers: diagnose and discover?** *Yes, in their projects' VPCs,
   without security-group hints.* Resources must come from AWS, so
   adding one needs discovery; diagnose is no more than adding a check and
   deleting it, which they can already do. Both are rate-limited.
6. **One AWS resource, one project?** *Recommended: yes* (unique per VPC and
   AWS id). A load balancer two teams care about belongs to one project, and
   the other team's people are added as extra recipients.
7. **Is the public dashboard acceptable?** Watchly on a public IP now has a
   route into the VPC. At minimum restrict 443 to known ranges. Better: put it
   behind a VPN or an ALB with WAF and, later, SSO.
8. **CloudWatch metrics?** *Yes, for `db_metrics` and `ec2_metrics` only.*
   `GetMetricData` is billed per metric, so each check reads only what applies
   to its resource (CPU credits on a burstable instance, IOPS on provisioned
   volumes, the forecast's history once an hour), and its interval sets the
   cost; see [§4](#4-aws-setup). Everything else comes from the probes and the
   free `Describe*` calls.

---

## 14. Phases

| Phase | Scope | Needs AWS APIs |
| ----- | ----- | :-: |
| **0. Egress guard** | `egress.py`, website checks public-only, `blocked_address`, `WEBSITE_PRIVATE_TARGETS`, `EGRESS_DENY_CIDRS`, W1. Ship before deploying into a VPC. | no |
| **1. AWS monitoring** | VPCs, discovery, import and sync; resources (servers, public and private; Auto Scaling groups; ALBs and NLBs, internal and internet-facing) with `ping`, `tcp`, `http`, `target_health` and `group_health` checks, a server's to its private or public IP, a group's on every instance; per-resource outages, degraded state, VPC unreachable; diagnose without hints; overview, events, maintenance, recipients, notifications, `/self`; screens 1, 2 (without the map), 3, 4 and 5 | yes |
| **2. AWS depth** | `ec2_status`, security-group hints on the map, an infrastructure section in the monthly report, CSV export of resources | yes |
| **3. Other accounts** | VPCs in other AWS accounts, through an `assume_role_arn` on the VPC (`sts:AssumeRole`). Network reach still goes through peering or a transit gateway. | yes |
