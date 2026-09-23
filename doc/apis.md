# API reference

Every endpoint in Watchly, with a one-line description.

**Base URL:** `/api/v1` (health check is at the root)
**Auth:** `Authorization: Bearer <access token>` on everything except signup,
login and `/health`.

Interactive docs run at `/docs` when the app is up. For how the pieces fit
together, see [`hld.md`](hld.md).

---

## At a glance

| # | method | endpoint | what it does |
| - | ------ | -------- | ------------ |
| 1 | `POST` | `/auth/signup` | Register a new account; always created as a `Viewer`. |
| 2 | `POST` | `/auth/login` | Log in with a username **or** email and get an access/refresh token pair. |
| 3 | `GET` | `/users/me` | Return the signed-in user's own profile. |
| 4 | `GET` | `/users` | List users, with role and active-state filters. |
| 5 | `GET` | `/users/{user_id}` | Return one user by id. |
| 6 | `PATCH` | `/users/{user_id}/role` | Assign a different role to a user. |
| 7 | `PATCH` | `/users/{user_id}/suspend` | Block a user from logging in and kill their current session. |
| 8 | `PATCH` | `/users/{user_id}/reactivate` | Lift a suspension and restore access. |
| 9 | `GET` | `/monitoring/projects` | List projects the caller is allowed to see. |
| 10 | `POST` | `/monitoring/projects` | Create a project and name the members responsible for it. |
| 11 | `GET` | `/monitoring/projects/{project_id}` | Return one project with its members. |
| 12 | `PATCH` | `/monitoring/projects/{project_id}` | Update a project's name, description, active flag or extra alert emails. |
| 13 | `DELETE` | `/monitoring/projects/{project_id}` | Delete a project and stop monitoring every site under it. |
| 14 | `POST` | `/monitoring/projects/{project_id}/members` | Add one or more users as responsible members (they start receiving alerts). |
| 15 | `DELETE` | `/monitoring/projects/{project_id}/members/{user_id}` | Remove a member so they stop receiving that project's alerts. |
| 16 | `GET` | `/monitoring/websites` | List monitored sites and their current up/down status. |
| 17 | `POST` | `/monitoring/websites` | Start monitoring a URL under a project, optionally with its own recipients. |
| 18 | `GET` | `/monitoring/websites/{website_id}` | Return one monitored site with its live outage state. |
| 19 | `PATCH` | `/monitoring/websites/{website_id}` | Update a site's URL, interval, timeout, alert budget, alert emails or enabled flag. |
| 20 | `DELETE` | `/monitoring/websites/{website_id}` | Stop monitoring a site and delete its check history. |
| 21 | `POST` | `/monitoring/websites/{website_id}/recipients` | Add one or more users alerted about this site only. |
| 22 | `DELETE` | `/monitoring/websites/{website_id}/recipients/{user_id}` | Stop alerting a user about this site. |
| 23 | `GET` | `/monitoring/websites/{website_id}/checks` | Return recent check results, newest first — the evidence behind alerts. |
| 24 | `POST` | `/monitoring/websites/{website_id}/check` | Probe a site immediately instead of waiting for the next scheduled tick. |
| 25 | `GET` | `/monitoring/notifications` | The global notification settings: wording and email/Slack switches per kind. |
| 26 | `PUT` | `/monitoring/notifications/{kind}` | Set the global wording and switches for one kind (admin/DevOps). |
| 27 | `DELETE` | `/monitoring/notifications/{kind}` | Reset a kind's global settings to the built-in wording (admin/DevOps). |
| 28 | `POST` | `/monitoring/notifications/preview` | Render a notification with sample data, exactly as it would be sent. |
| 29 | `GET` | `/monitoring/projects/{project_id}/notifications` | A project's settings after inheriting from global. |
| 30 | `PUT` | `/monitoring/projects/{project_id}/notifications/{kind}` | Override one kind for a project. |
| 31 | `DELETE` | `/monitoring/projects/{project_id}/notifications/{kind}` | Remove a project's override so it inherits again. |
| 32 | `POST` | `/monitoring/projects/{project_id}/report` | Send a monthly uptime report now instead of waiting for the 1st. |
| 33 | `GET` | `/health` | Liveness check; also reports whether the monitoring loop is running. |

---

## Auth

Public. No token required.

### `POST /api/v1/auth/signup`
Register a new account. Requires `password` **and** `confirm_password`; the role
is always `Viewer` and cannot be set from the payload.
`201` · `409` taken · `422` mismatch or invalid field

### `POST /api/v1/auth/login`
Authenticate with `identifier` (username **or** email) plus `password`, and
receive the user plus an access/refresh token pair.
`200` · `401` wrong credentials or unknown user · `403` suspended · `422` invalid

> There is no logout endpoint — the client discards its tokens.

---

## Users

| who can call | |
| ------------ | - |
| `/users/me` | any signed-in user |
| everything else | admin, DevOps, project manager (per-target rules below) |

### `GET /api/v1/users/me`
Return the signed-in user's own profile, whatever their role.
`200`

### `GET /api/v1/users`
Paginated user directory.
Query: `limit` (1–100), `offset`, `role`, `is_active`
`200` · `403` not a management role

### `GET /api/v1/users/{user_id}`
Return a single user by id.
`200` · `403` · `404`

### `PATCH /api/v1/users/{user_id}/role`
Assign a role. Body: `{"role": "Developer"}`. Admin may re-role anyone; DevOps
may re-role ordinary users but not admins or other DevOps users; nobody may
change their own role.
`200` · `403` insufficient rank or self · `404` · `422` unknown role

### `PATCH /api/v1/users/{user_id}/suspend`
Set `is_active = false`: the user can no longer log in, and any token they
already hold stops working on the next request.
`200` · `403` insufficient rank or self · `404`

### `PATCH /api/v1/users/{user_id}/reactivate`
Lift a suspension and restore login.
`200` · `403` · `404`

**Who may suspend whom**

| actor ╲ target | admin | DevOps | project manager | developer | viewer | self |
| -------------- | ----- | ------ | --------------- | --------- | ------ | ---- |
| admin | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| DevOps | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ |
| project manager | ❌ | ✅ | ❌ | ✅ | ✅ | ❌ |
| developer, viewer | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

---

## Monitoring — projects

A project groups the URLs of one client or product, and carries the alerting
setup its sites inherit. **Every project must have at least one alert channel**
— email, Slack, or both — configured on the project itself; there are no
separate endpoints for setting up a channel.

**Reading** is scoped: admin, DevOps and project managers see every project;
viewers and developers see only projects they belong to, and anything else
returns `404` rather than `403`.
**Writing** needs admin, DevOps, or being the project's owner (the person who
created it).

### `GET /api/v1/monitoring/projects`
List projects visible to the caller.
Query: `limit` (1–100), `offset`, `is_active`, `owner_id`
`200`

### `POST /api/v1/monitoring/projects`
Create a project. **At least one alert channel is required** — email
(`member_ids` and/or `extra_emails`), Slack (`slack_bot_token` **with**
`slack_channel_id`), or both. The caller becomes its owner.
`201` · `403` not admin/DevOps/PM · `409` name taken · `422` no alert channel,
half-configured Slack, or unknown member id

### `GET /api/v1/monitoring/projects/{project_id}`
Return one project with its member list.
`200` · `404` missing **or** not visible to the caller

### `PATCH /api/v1/monitoring/projects/{project_id}`
Update `name`, `description`, `is_active`, `extra_emails`, or the Slack
settings (`slack_bot_token`, `slack_channel_id`, `slack_enabled`). Supplying
`extra_emails` replaces the whole list. Sending `slack_bot_token: null` (or
`slack_channel_id: null`) turns Slack off entirely — both halves are cleared.
A change that would leave the project with no channel at all is refused.
`200` · `403` not yours · `404` · `409` name taken · `422` would leave no
alert channel

### `DELETE /api/v1/monitoring/projects/{project_id}`
Delete the project, every site under it, and all their check history.
`204` · `403` · `404`

### `POST /api/v1/monitoring/projects/{project_id}/members`
Add responsible members. Body: `{"member_ids": [3, 7, 11]}` — several at once,
idempotent for anyone already on the project.
`200` · `403` · `404` · `422` unknown user id

### `DELETE /api/v1/monitoring/projects/{project_id}/members/{user_id}`
Remove one member; they stop receiving this project's alerts. Removing a
non-member is a no-op. Refused if they are the last member and Slack is not
configured.
`200` · `403` · `404` · `422` would leave no alert channel

---

## Monitoring — websites

**Reading** is scoped the same way as projects, except that a viewer or
developer can also see any site they are a **recipient** of. **Writing** needs
rights over the site's project.

**Who is emailed about a site:** the project's members and `extra_emails`
(unless the site sets `inherit_project_recipients: false`), the site's own
recipient users and `alert_emails`, and `ALERT_DEFAULT_EMAILS`. Suspended users
are skipped. Every site must keep at least one alert channel, just like its
project.

### `GET /api/v1/monitoring/websites`
List monitored sites with their current status, last check and outage state.
Query: `limit` (1–100), `offset`, `status` (`unknown`/`up`/`down`),
`is_enabled`, `project_id`
`200`

### `POST /api/v1/monitoring/websites`
Start monitoring a URL. Requires `project_id`; the project's members are
alerted unless `inherit_project_recipients` is false.
Body: `project_id`, `name`, `url`, and optionally `environment` (`development`,
`testing`, `uat`, `staging` or `production` — the web form requires it, the API
does not, so existing scripts keep working), `method`, `expected_status`,
`timeout_seconds`, `check_interval_seconds` (≥30), `max_down_alerts`,
`is_enabled`, `recipient_ids` (users alerted about this site only),
`alert_emails` (addresses alerted about this site only),
`inherit_project_recipients` (default `true`), `slack_channel_id`
`201` · `403` not your project · `404` no such project · `409` URL already
monitored · `422` invalid field, unknown recipient id, or the site would have
no alert channel

### `GET /api/v1/monitoring/websites/{website_id}`
Return one site with its live state: `status`, `last_checked_at`, `down_since`,
`consecutive_failures`, `down_alerts_sent` — and its alerting setup:
`recipients`, `alert_emails`, `inherit_project_recipients`, `alert_channels`.
`200` · `404` missing **or** not visible

### `PATCH /api/v1/monitoring/websites/{website_id}`
Update any of the check settings, `alert_emails` (replaces the whole list),
`inherit_project_recipients`, or disable the site without deleting it. A
change that would leave the site with no alert channel is refused.
`200` · `403` · `404` · `409` URL already monitored · `422` would leave no
alert channel

### `DELETE /api/v1/monitoring/websites/{website_id}`
Stop monitoring and delete the site's check history.
`204` · `403` · `404`

### `POST /api/v1/monitoring/websites/{website_id}/recipients`
Add users alerted about this site only. Body: `{"recipient_ids": [3, 7]}` —
several at once, idempotent for anyone already a recipient. They need not be
project members, and can see this site once added.
`200` · `403` · `404` · `422` unknown user id

### `DELETE /api/v1/monitoring/websites/{website_id}/recipients/{user_id}`
Stop alerting one user about this site. Removing a non-recipient is a no-op.
Refused if it would leave the site with no alert channel.
`200` · `403` · `404` · `422` would leave no alert channel

### `GET /api/v1/monitoring/websites/{website_id}/checks`
Recent check results, newest first — status code, response time, error and
final URL for each poll.
Query: `limit` (1–500)
`200` · `404`

### `POST /api/v1/monitoring/websites/{website_id}/check`
Probe now, through the same state machine as a scheduled check, so it can raise
and clear alerts. The fastest way to test a new site or your SMTP setup.
Returns the site, the check, and which notification it raised (if any): `down`,
`still_down`, `recovered`, `ssl_expiring` or `slow_response`.
`200` · `403` · `404`

A website also takes an optional `slow_threshold_ms` (send `null` for the
server default), an `environment` (send `null` to clear it; sites that predate
the field have none), and reports a read-only `ssl_expires_at` for HTTPS sites.

---

## Monitoring — notifications

`{kind}` is one of `down`, `still_down`, `recovered`, `ssl_expiring`,
`slow_response`, `monthly_report`. Settings resolve project → global → built-in
default, field by field; see the README for what each kind is.

Every setting comes back as:
`kind`, `label`, `description`, `audience`, the effective `email_enabled`,
`slack_enabled`, `subject`, `body`, where each came from (`sources`:
`project` / `global` / `default`), what this level itself stores (`overrides`,
null = inherits), what a blank field would fall back to (`inherited_subject`,
`inherited_body`), the built-in `default_subject` / `default_body`, and the
`placeholders` a template may use.

### `GET /api/v1/monitoring/notifications`
Global settings for every kind. Any signed-in user.
`200`

### `PUT /api/v1/monitoring/notifications/{kind}`
Replace the global overrides for one kind. Send all four fields
(`email_enabled`, `slack_enabled`, `subject`, `body`): one left null or blank
inherits, and all four null removes the override. Admin/DevOps only.
`200` · `403` · `422` unknown placeholder, too long (subject 255, body 4000), or
it would switch "Site down" off on every channel

### `DELETE /api/v1/monitoring/notifications/{kind}`
Reset to the built-in wording and switches. Admin/DevOps only.
`200` · `403`

### `POST /api/v1/monitoring/notifications/preview`
Body: `kind`, and optionally `subject` and `body` (omitted means the built-in
default). Returns the rendered `subject`, `email_html`, `email_text`, and the
Slack `slack_text` and `slack_blocks`, built from sample data by the same code
that sends real messages. Nothing is saved or sent.
`200` · `422` unknown placeholder

### `GET /api/v1/monitoring/projects/{project_id}/notifications`
The project's settings, after inheriting from global. Anyone who can see the
project.
`200` · `404`

### `PUT /api/v1/monitoring/projects/{project_id}/notifications/{kind}`
Same body and rules as the global `PUT`, for one project. Requires admin/DevOps
or ownership of the project.
`200` · `403` · `404` · `422`

### `DELETE /api/v1/monitoring/projects/{project_id}/notifications/{kind}`
Remove the project's override; it follows global again.
`200` · `403` · `404`

### `POST /api/v1/monitoring/projects/{project_id}/report`
Build and send a monthly uptime report now. Body (optional): `month` as
`YYYY-MM`, a month that has ended; defaults to last month. Follows the
project's notification switches. Returns the `month`, how many `sites` it
covered, `email_recipients`, and `delivered_by` — empty means nothing was
delivered (switched off, SMTP/Slack not set up, or sending failed; see the log).
Does not stop the scheduled report.
`200` · `403` · `404` · `409` no checks that month, or the month has not ended

---

## Health

### `GET /health`
Public liveness check. Returns `{"status": "ok", "monitoring": "on"|"off"}`.
`200`

---

## Conventions

**Status codes.** Beyond the ones listed, any authenticated endpoint may return
`401` (missing, malformed or expired token, or a refresh token used as an
access token) and `403` (the account has been suspended).

**Roles.** `Viewer` · `Admin` · `DevOps` · `Project Manager` · `Developer`.
Everyone starts as `Viewer`.

**Errors.** `{"detail": "..."}` for single errors; `422` returns FastAPI's
validation list. Password fields are redacted from `422` bodies.

**Pagination.** List endpoints take `limit` and `offset` and return
`{items, total, limit, offset}`. `total` counts what the caller may see, not
what exists.

**Timestamps.** ISO 8601, UTC.

**Secrets.** A project's `slack_bot_token` is write-only: it is encrypted before
storage and never appears in a response. Reads return `slack_configured` and a
masked `slack_token_hint` such as `xoxb-…9f2a` instead.

---

## Not built yet

- `POST /auth/refresh` — the JWT utils support it (`decode_refresh_token`), but
  no endpoint exists, so refresh tokens currently have nowhere to be redeemed.
- Logout / server-side token revocation — deliberately left out; tokens stay
  valid until they expire.
- Audit trail for role changes, suspensions and project edits.
- Automatic pruning of `website_checks`; `WebsiteService.purge_old_checks()`
  exists but nothing calls it. When you add it, keep at least 35 days: the
  monthly uptime report is computed from these rows.
- Retrying a monthly report that nobody received. It is sent at most once per
  project per month; use `POST /monitoring/projects/{id}/report` to resend.
- An audit trail of who changed a notification template.
