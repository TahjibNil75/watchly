# Changelog

All notable changes to Watchly are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
Watchly uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The
version covers the REST API under `/api/v1`, the settings in `.env`, and
upgrading an existing database: a release that breaks any of them is a new
major version.

## [Unreleased]

### Added

- Docker monitoring, on by default (`DOCKER_ENABLED`): a new `docker` project
  type, a Docker page, and the `/api/v1/monitoring/docker` API. Each host runs
  the separate [Watchly Docker agent](https://github.com/TahjibNil75/watchly-docker-agent),
  which pushes container state, resource use and Docker events to
  `POST /api/v1/docker/ingest` with the host's token; nothing on the host is
  opened. See [`doc/docker.md`](doc/docker.md).
- Alerts for a container down (after `DOCKER_DOWN_GRACE_SECONDS`) and back,
  unhealthy, killed for memory, in a restart loop, or over its CPU or memory
  threshold, and for a host whose agent stops reporting or loses Docker: eight
  new notification kinds, on every channel, with editable templates.
- Migration `0048_docker_monitoring`.
- An Overview page as the home page: one card per monitor and a list of
  everything down or failing across websites, infrastructure and Docker.
  Websites moved from `/` to `/websites`.

### Fixed

- A project's notification settings showed the deployment alerts under website
  projects instead of infrastructure ones.

### Changed

- The sidebar groups its links under Monitor and Workspace, shows each
  monitor's total (or, in red, how many things need a look), and keeps Profile,
  Users and Sign out in a menu on the signed-in user. Docker uses the whale icon.
- Log files keep only the last `LOG_RETENTION_HOURS` (default 6); older lines
  are deleted. This replaces size rotation, so `LOG_MAX_BYTES` and
  `LOG_BACKUP_COUNT` no longer do anything.

## [1.1.0] - 2026-10-07

Infrastructure monitoring for AWS, database checks, and more for each website.

### Added

#### Infrastructure monitoring

- AWS infrastructure monitoring, off until `INFRA_AWS_ENABLED=true`: an
  Infrastructure page and the `/api/v1/monitoring/infra/aws` API. See
  [`doc/infraapi.md`](doc/infraapi.md).
- EC2 servers (public and private subnets), Application and Network Load
  Balancers, Auto Scaling groups and RDS databases, read from AWS. A resource
  has up to 10 checks (ping, TCP, HTTP, target health, group health, database
  status and metrics); the resource, not the check, goes down and alerts.
- Several AWS accounts, each with its own credentials: Watchly's own (the
  instance role), or a stored access key encrypted at rest, either of which may
  assume a role in the account. Accounts and their VPCs belong to
  infrastructure projects.
- One alert when a whole VPC is unreachable, instead of one per resource.
- Auto Scaling scale-out and scale-in notifications, opt-in, with an optional
  health check of each new instance.
- CodeDeploy: deployments are announced, and the resources they deploy to are
  silenced while one runs and for `DEPLOY_SETTLE_SECONDS` after it.
- Capacity alerts: a server's CPU, CPU credits, volume burst balance, IOPS and
  disks (`ec2_metrics`); a database's storage forecast, connections against
  `max_connections`, provisioned IOPS, burst balance and CPU credits; and an
  account's unattached Elastic IPs and Elastic IP quota
  (`CAPACITY_QUOTA_PERCENT`).
- Diagnose runs and VPC tests, with a hint for what to fix.

#### Monitoring

- Database checks for PostgreSQL, MySQL, Redis and MongoDB: Watchly connects to
  the endpoint and records what the server says, without logging in.
- Domain expiry warnings from RDAP (WHOIS for registries without it), and an
  alert when a domain's nameservers change.
- A site's page now shows its certificate (issuer, names covered, validity, TLS
  version, cipher, HTTP/2 and the chain), the CDN in front of it, the server it
  is served from, the redirects a check followed, and a grade for five
  security headers.
- History charts where the time goes: DNS lookup, TCP connect, TLS handshake and
  time to first byte.
- Request headers an HTTP check sends, encrypted at rest.
- A project monitors either websites or infrastructure, chosen when it is
  created.

#### Accounts and security

- Request an account from the sign-in page; an Admin or DevOps user approves it
  (which sends an invitation as a Viewer) or rejects it.
- Rate limits on the endpoints that need no sign-in (`RATE_LIMIT_*`), answering
  `429` with `Retry-After`.
- Restrict the API by visitor country, and email a user who signs in from a
  country they have not used before (`COUNTRY_*`).
- An egress policy that keeps website checks to public addresses
  (`WEBSITE_PRIVATE_TARGETS`, `EGRESS_DENY_CIDRS`).

#### Running Watchly

- Logs in `LOG_DIR`, one file per part of the backend, rotated at
  `LOG_MAX_BYTES`.
- A redesigned landing page and dashboard.

### Changed

- Signup is always invite-only: once the admin exists, every sign-up is
  refused, and everyone else asks for an account or is invited.
- The default `SSL_EXPIRY_ALERT_DAYS` is `14,7` (was `14,7,3,1`). Set it in
  `.env` to keep the old warnings.

### Removed

- `ALLOW_PUBLIC_SIGNUP`. Anyone who relied on `true` letting visitors register
  as viewers should now invite them, or let them request an account.

### Upgrading

Back up the database first. Migrations 0029 to 0047 run on start. They add
tables and columns, and every new setting has a default. Migration 0042 also
moves any AWS accounts from the earlier preview of infrastructure monitoring
into the projects that used them.

## [1.0.0] - 2026-09-27

The first release.

### Added

#### Monitoring

- HTTP(S) checks on each site's own interval, with the expected status, a
  timeout, retries before a failure counts, and body rules (`must_contain` /
  `must_not_contain`).
- Ping checks for hosts: replies, round trips, jitter, and packet-loss alerts.
- DNS checks for A, AAAA, CNAME, MX and TXT records across several public
  resolvers. Pin the values you expect, or be alerted when the records change.
- Early warnings: SSL certificates about to expire (at 14, 7, 3 and 1 days by
  default) and slow responses.
- Maintenance windows: start one from a site's page for a deployment ("Start
  maintenance for 30 min" / "End now"), or schedule one ahead. The site is not
  checked and nobody is alerted until it ends.
- Uptime and response-time history from 24 hours to 90 days, read from hourly
  rollups, with CSV export. Raw checks are kept for `CHECK_RETENTION_DAYS`.

#### Alerting

- Email (any SMTP provider), Slack, Telegram, WhatsApp (Meta's Cloud API) and a
  generic JSON webhook.
- One Slack thread, or Telegram reply chain, per outage: the down alert,
  reminders up to each site's alert limit, then the recovery.
- Project members hear about every site in the project, and a site can add its
  own recipients, Slack channel, Telegram chat or WhatsApp numbers.
- Editable wording, and a switch per alert kind and channel, globally and per
  project, with a preview of the message as it will be sent.
- A monthly uptime report per project, sent on the 1st and downloadable as CSV.
- An in-app feed of outages, recoveries and warnings.

#### Teams and accounts

- Projects with owners and members, and five roles: Admin, DevOps, Project
  Manager, Developer and Viewer.
- Email invitations, invite-only mode (`ALLOW_PUBLIC_SIGNUP=false`), and
  suspending and reactivating accounts.
- Short-lived access tokens with rotating refresh-token cookies, lockout after
  repeated wrong passwords, password reset by email, and email changes
  confirmed by a link sent to the new address.
- Slack, Telegram and WhatsApp tokens are encrypted at rest.

#### Running Watchly

- One `docker compose up` runs PostgreSQL 16, the API and the web app behind
  nginx; database migrations run on every start.
- A React web app with a public landing page, a live dashboard, per-site
  history and notification settings.
- A REST API under `/api/v1`, with interactive docs at `/docs`. `/health`
  reports the running version.

[Unreleased]: https://github.com/TahjibNil75/watchly/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/TahjibNil75/watchly/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/TahjibNil75/watchly/releases/tag/v1.0.0
