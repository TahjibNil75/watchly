# Website monitoring

How a URL gets watched, how an outage turns into email, and every API in the
project as it stands today.

---

## 1. The model

Nothing is monitored on its own — a **URL always lives under a project**. The
project's **members** are alerted about every site in it, and **each site can
add its own recipients** on top — or instead, when it stops inheriting the
project's.

```
Project ──┬── members (users)  ─┐  inherited unless the site sets
          ├── extra_emails      ┤  inherit_project_recipients = false
          │                     ├─→ alert recipients
          └── Website ──┬── recipients (users) ─┤
                        ├── alert_emails ───────┘
                        └── WebsiteCheck (one row per poll)
```

| table                | what it holds                                              |
| -------------------- | ---------------------------------------------------------- |
| `projects`           | a client or product; owner, members, extra alert addresses |
| `project_members`    | which users are responsible for which project              |
| `websites`           | one URL, how to check it, and its live outage state        |
| `website_recipients` | which users are alerted about one particular site          |
| `website_checks`     | the result of every poll — the evidence behind each alert, including why it failed and where its time went; purged after `CHECK_RETENTION_DAYS` |
| `website_check_hourly` | each site's checks summed per UTC hour — what charts and long-range uptime read; kept |

Deleting a project deletes its websites, which deletes their check history.

---

## 2. Files in this package

| file          | role                                                             |
| ------------- | ---------------------------------------------------------------- |
| `models.py`   | `Website` (config + live state) and `WebsiteCheck` (poll history) |
| `schemas.py`  | request/response contracts and their validation rules            |
| `checker.py`  | the HTTP probe (and, every few hours, the certificate read) — turns a URL into a `CheckResult` |
| `service.py`  | CRUD, filtering, and "which sites are due for a check"           |
| `history.py`  | hourly rollups, the stats read from them, and purging old checks |
| `routes.py`   | the HTTP endpoints below                                          |

The pieces they talk to live one level up:

| file                            | role                                        |
| ------------------------------- | ------------------------------------------- |
| `../service.py`                 | the outage state machine, plus slow-response and SSL-expiry tracking |
| `../scheduler.py`               | the background loop that drives everything, and the monthly reports |
| `../projects/`                  | projects and membership                     |
| `../alerts/`                    | what each notification looks like, per channel (email, Slack, webhook) |
| `../notifications/`             | per-project switches and editable wording, and the monthly report |

---

## 3. How a check actually runs

```
scheduler._loop()                every MONITOR_TICK_SECONDS (default 60s)
  └─ run_tick_locked()           Postgres advisory lock: only one worker proceeds
      └─ MonitoringService.run_due_checks()
          ├─ WebsiteService.due_for_check()      enabled, and interval elapsed
          ├─ checker.check_website()   × N       concurrent, one shared client
          └─ MonitoringService.record_result()   sequential, per site
              ├─ INSERT website_checks
              ├─ advance status / down_since / counters
              ├─ COMMIT                          ← before sending, always
              └─ dispatch() → email / slack / webhook
```

Two properties worth preserving if you change this:

- **State commits before alerts send.** A hung mail server can never cause the
  same alert to fire again on the next tick.
- **`checker.check_website` never raises.** DNS failure, TLS error, timeout and
  a 500 are all normal outcomes returned as `CheckResult(is_up=False, ...)`. If
  it raised, one bad site would stop the whole tick.

A site counts as up when its status code equals `expected_status` (default
`200`) after following redirects.

Each `website_checks` row keeps what the probe learned, not just the verdict:
the status and reason phrase, the `error_type` (see below), the diagnostic
headers, and how long each step took — `dns_ms`, `connect_ms`, `tls_ms` and
`first_byte_ms` (request sent → response headers back), summed over any
redirects. A step that did not finish, or that a reused connection skipped, is
`null`.

`error_type` is the key to group incidents by:

| error_type           | meaning                                            |
| -------------------- | -------------------------------------------------- |
| `dns_error`          | the hostname did not resolve                       |
| `connect_error`      | connection refused, reset, or no route to the host |
| `connect_timeout`    | no TCP connection within the timeout               |
| `tls_error`          | the TLS handshake failed — e.g. an expired or untrusted certificate |
| `read_timeout`       | connected, but no response within the timeout      |
| `unexpected_status`  | answered, but not with `expected_status`           |
| `too_many_redirects`, `protocol_error`, `invalid_url`, `timeout` | as named |

To time DNS apart from the TCP connect, probes resolve the hostname
themselves (`_TimedBackend` in `checker.py`) rather than leaving it to
httpcore, which does both in one step. The connect then tries the addresses the
way anyio would have, Happy Eyeballs included.

---

## 4. When alerts fire

| transition                          | what happens                              |
| ----------------------------------- | ----------------------------------------- |
| UP → DOWN (first failed check)      | **alert immediately**                     |
| still DOWN, alerts 2…`max_down_alerts` | alert with cumulative downtime         |
| still DOWN, past the budget         | keep checking, **stay silent**            |
| DOWN → UP                           | **one** recovery alert with total downtime |
| UP → UP                             | silent                                    |

With the defaults (5-minute interval, `max_down_alerts = 4`) one outage
produces:

```
down → still_down(5m) → still_down(10m) → still_down(15m) → [silence] → recovered(40m)
```

No repeat "site is up" mail — recovery is announced once, then nothing until the
next outage.

---

## 5. Who receives an alert

**Every project must have at least one channel — email, Slack, or both.** It is
chosen when the project is created and enforced on every later change: you
cannot clear the last email recipient while Slack is unset, remove the last
member, or drop Slack while it is the only channel. There are no separate
endpoints for configuring a channel; it is all part of the project payload.

`GET /monitoring/projects/{id}` reports what is set as `alert_channels`, e.g.
`["email"]` or `["email", "slack"]`.

Sites inherit their project's channels and need nothing of their own — but
each can have its own recipients, and **a site must keep at least one
channel too**: one that stops inheriting the project's recipients needs a
recipient or address of its own (or Slack), and its last one cannot be
removed. `GET /monitoring/websites/{id}` reports the site's `alert_channels`.

### Email

Five sources, merged and de-duplicated case-insensitively in
`MonitoringService.recipients_for`:

1. **Project members** — every site in the project. Suspended users are skipped.
2. **`project.extra_emails`** — non-user addresses for the whole project
   (a client contact, a shared inbox).
3. **Site recipients** — users alerted about one site only, managed with
   `POST`/`DELETE /monitoring/websites/{id}/recipients`. Suspended users are
   skipped. They need not be project members.
4. **`website.alert_emails`** — non-user addresses for one site only.
5. **`ALERT_DEFAULT_EMAILS`** — environment-wide, every alert.

1 and 2 are skipped for a site with **`inherit_project_recipients: false`**, so
only its own list (3, 4) and the global defaults are emailed. Use it when
different people own different sites in one project:

```bash
# Only users 11 and 12 hear about the checkout site; the rest of the project does not.
curl -X POST $BASE/monitoring/websites/$SID/recipients -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"recipient_ids": [11, 12]}'
curl -X PATCH $BASE/monitoring/websites/$SID -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"inherit_project_recipients": false}'
```

Recipients go in first: opting out while the site has none of its own is
refused with `422` (unless Slack is set up), since nobody would be
emailed. Slack is unaffected by the flag.

Subjects are project-qualified:

```
[DOWN] Monstar People / Marketing site is not responding
```

### Slack

Each project carries **its own bot token and channel id**, so every client gets
a separate private channel. A site may override the channel while reusing the
project's token, or bring **a token of its own** — which works even when the
project has no Slack, and is how Slack is set up from the "Add website" form.

| level | field | effect |
| ----- | ----- | ------ |
| project | `slack_bot_token` | the workspace to post as; encrypted at rest |
| project | `slack_channel_id` | default channel for all the project's sites |
| project | `slack_enabled` | mute the project's Slack without discarding the settings |
| site | `slack_channel_id` | that one site posts here instead |
| site | `slack_bot_token` | that one site posts as this bot; needs the site's `slack_channel_id`, and ignores the project's mute |

A site's channel needs a token from somewhere — its own or its project's — and
its own token needs its own channel; anything else is refused with `422`.
`PATCH` with `slack_channel_id: null` removes the site's Slack, token included.

Setting it up:

1. Create a Slack app, add the **`chat:write`** bot scope, install it, and copy
   the bot token (`xoxb-…`).
2. In the private channel run `/invite @YourBot` — Slack refuses to post to a
   private channel the bot has not joined.
3. Copy the channel id (channel → About → bottom of the panel). Not the
   `#name`; the API rejects that.
4. `PATCH /monitoring/projects/{id}` with `slack_bot_token` and
   `slack_channel_id` — or the same two fields on
   `POST`/`PATCH /monitoring/websites` for one site.
5. Watch the channel on the next outage, or force one with
   `POST /monitoring/websites/{id}/check` against a URL you expect to fail.

**The token is write-only.** It is encrypted with Fernet before storage and
never returned; reads give a masked `slack_token_hint` (plus `slack_configured`
on a project).
The key comes from `SLACK_TOKEN_ENCRYPTION_KEY`, or `SECRET_KEY` when that is
blank — **rotating either makes stored tokens unreadable** and they have to be
re-entered.

Slack answers `200 OK` even when it refuses a message, so the response body is
what decides success. Common refusals are translated: `not_in_channel` becomes
"the bot is not a member of that channel — run `/invite @YourBot` in it".

`SLACK_WEBHOOK_URL` still works as a global firehose for projects with no Slack
of their own.

Each alert carries HTTP status and reason, expected vs actual, error class
(`dns_error`, `connect_timeout`, `tls_error`, `unexpected_status`, …), response
time and how it split across DNS, connect, TLS and first byte, timeout,
redirect target, response size, downtime so far, `down_since`, consecutive
failures, and diagnostic headers (`server`, `retry-after`, `cf-ray`). A
slow-response alert names the slowest step. Sent as both plain text and HTML.

---

## 6. Permissions

**owner** = the project manager who created the project. Admin and DevOps
override ownership everywhere.

### Reading — what a dashboard shows

| role                            | sees                                      |
| ------------------------------- | ----------------------------------------- |
| admin, DevOps                   | every project and every site              |
| **project manager, viewer, developer** | **only projects they own or are a member of**, and the sites under them, **plus any site they are a recipient of** |

Access is granted by adding someone as a project member or a site recipient —
in the create payload (`member_ids`, `recipient_ids`) or later through the
`/members` and `/recipients` endpoints. Both also make them an alert recipient.
A project manager also sees the projects they created, without being a member.

A site recipient who is not in the project sees that site and its check history,
but not the project or its other sites — enough to follow up on the alerts they
receive. Anyone who belongs to no project and receives no site alerts sees an
empty dashboard. Asking for a project or site outside what they
can see returns **`404`, not `403`** — for writes as well as reads —
a resource they cannot access is indistinguishable from one that does not
exist, so ids cannot be probed. The `project_id` filter cannot be used to peek
either; it narrows within what they can already see.

Membership changes take effect on the next request — no cache to clear.

### Writing

| action                  | who                                     |
| ----------------------- | --------------------------------------- |
| create a project        | admin, DevOps, project manager          |
| edit/delete a project   | admin, DevOps, the owner                |
| add/remove members      | admin, DevOps, the owner                |
| add/edit/delete a site  | admin, DevOps, the owner of its project |
| add/remove site recipients | admin, DevOps, the owner of its project |
| trigger a check now     | admin, DevOps, the owner of its project |

Viewers and developers can never write, even to their own project's sites.

The rules live in `app/core/permissions.py` (`can_create_project`,
`can_manage_project`, `can_view_all_projects`). The scoping queries are
`visible_project_ids()` in `../projects/models.py`, applied by
`ProjectService.list` / `get_visible` and `WebsiteService.list` / `get_visible`,
and `recipient_website_ids()` in `models.py`, which the website side adds.

---

## 7. Endpoints

Everything is under `/api/v1` and needs `Authorization: Bearer <access token>`
except signup and login. Every route can also return `401` (no or invalid
token) and `403` (suspended account).

Monitoring reads are **scoped by project membership** (and site recipiency) for
viewers and developers — see §6. Their list endpoints return only what they
belong to, and anything else reads as `404`.

### Monitoring — projects

| method | path | status codes |
| ------ | ---- | ------------ |
| `GET` | `/monitoring/projects` | `200 422` |
| `POST` | `/monitoring/projects` | `201 403 409 422` |
| `GET` | `/monitoring/projects/{project_id}` | `200 404 422` |
| `PATCH` | `/monitoring/projects/{project_id}` | `200 403 404 409 422` |
| `DELETE` | `/monitoring/projects/{project_id}` | `204 403 404 422` |
| `POST` | `/monitoring/projects/{project_id}/members` | `200 403 404 422` |
| `DELETE` | `/monitoring/projects/{project_id}/members/{user_id}` | `200 403 404 422` |

List filters: `limit` (1–100), `offset`, `is_active`, `owner_id`.

### Monitoring — websites

| method | path | status codes |
| ------ | ---- | ------------ |
| `GET` | `/monitoring/websites` | `200 422` |
| `GET` | `/monitoring/websites/summary` | `200 422` |
| `POST` | `/monitoring/websites` | `201 403 404 409 422` |
| `GET` | `/monitoring/websites/{website_id}` | `200 404 422` |
| `PATCH` | `/monitoring/websites/{website_id}` | `200 403 404 409 422` |
| `DELETE` | `/monitoring/websites/{website_id}` | `204 403 404 422` |
| `POST` | `/monitoring/websites/{website_id}/recipients` | `200 403 404 422` |
| `DELETE` | `/monitoring/websites/{website_id}/recipients/{user_id}` | `200 403 404 422` |
| `GET` | `/monitoring/websites/{website_id}/checks` | `200 404 422` |
| `GET` | `/monitoring/websites/{website_id}/stats` | `200 404 422` |
| `POST` | `/monitoring/websites/{website_id}/check` | `200 403 404 422` |

List filters: `limit` (1–100), `offset`, `status` (`unknown`/`up`/`down`),
`is_enabled`, `project_id`, `q` (name or URL), `sort` (`id`, `name`, `status`).
The summary takes `project_id` and `q`. History takes `limit` (1–500), newest
first; stats take `range` (`24h`, `7d`, `30d`, `90d`).

Site recipients: create accepts `recipient_ids`; afterwards `POST .../recipients`
with `{"recipient_ids": [...]}` adds several at once (idempotent), and
`DELETE .../recipients/{user_id}` removes one. `alert_emails` is replaced
wholesale by `PATCH`, like the project's `extra_emails`.

`POST .../check` probes immediately through the same state machine, so it can
raise and clear alerts — the fastest way to test a new site or your SMTP
credentials.

### Auth

| method | path | status codes |
| ------ | ---- | ------------ |
| `POST` | `/auth/signup` | `201 409 422` |
| `POST` | `/auth/login` | `200 401 403 422` |
| `POST` | `/auth/forgot-password` | `202 422` |
| `POST` | `/auth/confirm-email` | `200 403 404 409 410 422` |

Signup always creates a `Viewer`; the role cannot be set from the payload.
Login accepts a username **or** an email in the `identifier` field. A suspended
user is refused with `403` until reactivated.

### Users

| method | path | status codes |
| ------ | ---- | ------------ |
| `GET` | `/users/me` | `200` |
| `PATCH` | `/users/me` | `200 422` |
| `POST` | `/users/me/password` | `200 400 422` |
| `POST` | `/users/me/email` | `202 400 409 422` |
| `DELETE` | `/users/me/email` | `200` |
| `GET` | `/users` | `200 403 422` |
| `GET` | `/users/{user_id}` | `200 403 404 422` |
| `PATCH` | `/users/{user_id}/role` | `200 403 404 422` |
| `PATCH` | `/users/{user_id}/suspend` | `200 403 404 422` |
| `PATCH` | `/users/{user_id}/reactivate` | `200 403 404 422` |

### Health

`GET /health` — unauthenticated; reports whether the monitoring loop is running.

---

## 8. Worked example

```bash
BASE=http://127.0.0.1:8000/api/v1
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"identifier":"admin","password":"Admin@123"}' | jq -r .tokens.access_token)

# 1. a project, its responsible members, and a client contact
PID=$(curl -s -X POST $BASE/monitoring/projects \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Monstar People","member_ids":[3,7],
       "extra_emails":["client@theirdomain.com"]}' | jq -r .id)

# 2. the URL to watch, with two extra people alerted about this site only
SID=$(curl -s -X POST $BASE/monitoring/websites \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"project_id\":$PID,\"name\":\"Marketing site\",
       \"url\":\"https://www.monstarpeople.com/\",\"check_interval_seconds\":300,
       \"recipient_ids\":[11,12]}" \
  | jq -r .id)

# 3. prove it works without waiting for the next tick
curl -s -X POST $BASE/monitoring/websites/$SID/check \
  -H "Authorization: Bearer $TOKEN" | jq '{status:.website.status,
      http:.check.status_code, ms:.check.response_time_ms, alert:.alert_sent}'

# 4. the evidence trail
curl -s "$BASE/monitoring/websites/$SID/checks?limit=10" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.[] | "\(.checked_at) up=\(.is_up) \(.status_code)"'
```

---

## 9. Configuration

| variable | default | meaning |
| -------- | ------- | ------- |
| `MONITORING_ENABLED` | `true` | run the background loop in this process |
| `MONITOR_TICK_SECONDS` | `60` | how often to look for due checks |
| `DEFAULT_CHECK_INTERVAL_SECONDS` | `300` | per-site default |
| `DEFAULT_TIMEOUT_SECONDS` | `10` | per-request timeout |
| `DEFAULT_MAX_DOWN_ALERTS` | `4` | 1 immediate + 3 follow-ups |
| `ALERTS_ENABLED` | `true` | master switch for all channels |
| `ALERT_DEFAULT_EMAILS` | — | comma-separated, added to every alert |
| `SMTP_HOST` … | — | any provider: SES, Resend, Mailgun, Postmark |

Set `MONITORING_ENABLED=false` to run an API-only instance. With
`uvicorn --workers N` the advisory lock keeps exactly one worker checking, so
alerts are not duplicated.

---

## 10. Operational notes

- **Raw checks are purged; hourly rollups are kept.** Every tick re-sums the
  last hour or two into `website_check_hourly`, then deletes checks older than
  `CHECK_RETENTION_DAYS` in batches (see `history.py`). The 95th percentile is
  read from response-time buckets per hour, so it can be added across hours;
  changing `RESPONSE_BUCKETS_MS` means re-rolling history.
- **Alert recipients are resolved at send time**, so changing project membership
  or a site's recipients takes effect on the next check with no other action.
- **Eager loading matters.** `Website.project`, `Website.recipients` and
  `Project.members` use `lazy="selectin"` because the alert path reads them from
  a background task, where a lazy load would raise `MissingGreenlet`.
