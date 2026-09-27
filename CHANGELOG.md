# Changelog

All notable changes to Watchly are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
Watchly uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The
version covers the REST API under `/api/v1`, the settings in `.env`, and
upgrading an existing database: a release that breaks any of them is a new
major version.

## [Unreleased]

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

[Unreleased]: https://github.com/TahjibNil75/watchly/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/TahjibNil75/watchly/releases/tag/v1.0.0
