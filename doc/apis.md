# API reference

Every endpoint in Watchly, with a one-line description.

**Base URL:** `/api/v1` (health check is at the root)
**Auth:** `Authorization: Bearer <access token>` on everything except signup
(and its status), login, forgot password, confirming a new email address (`/auth/confirm-email`),
the two invitee endpoints (`/invitations/preview`, `/invitations/accept`),
asking for an account (`POST /account-requests`) and `/health`.

Those public endpoints except `GET /auth/signup` and `/health` are
rate-limited per client (an IPv4 address, or an IPv6 /64), whether the request
succeeds or not. Past a limit they answer `429` with `Retry-After` in seconds:

| setting | default | counts |
| ------- | ------- | ------ |
| `RATE_LIMIT_LOGIN` | `10/minute` | `POST /auth/login` |
| `RATE_LIMIT_SIGNUP` | `5/hour` | `POST /auth/signup` |
| `RATE_LIMIT_FORGOT_PASSWORD` | `5/hour` | `POST /auth/forgot-password` |
| `RATE_LIMIT_EMAIL_LINKS` | `20/minute` | `POST /auth/confirm-email`, `/invitations/preview` and `/invitations/accept`, together |
| `RATE_LIMIT_ACCOUNT_REQUESTS` | `5/hour` | `POST /account-requests` |

`RATE_LIMIT_ENABLED=false` turns them off. `POST /auth/refresh` and
`/auth/logout` are not limited: their token cannot be guessed, and a refused
refresh would sign people out.

A user who signed in with a temporary password (`must_change_password: true`)
gets `403` from everything except `GET /users/me` and `POST /users/me/password`
until they choose a new one.

Interactive docs run at `/docs` when the app is up. For how the pieces fit
together, see [`hld.md`](hld.md).

---

## At a glance

| # | method | endpoint | what it does |
| - | ------ | -------- | ------------ |
| 1 | `POST` | `/auth/signup` | Register the first account on an empty database, as the `Admin`. Refused once it exists: people join by invitation. |
| 1a | `GET` | `/auth/signup` | Whether signup is open, so the UI knows whether to offer it (public). |
| 2 | `POST` | `/auth/login` | Log in with a username **or** email; get an access token and a refresh cookie. |
| 3 | `POST` | `/auth/refresh` | Trade the refresh cookie for a new access token; the cookie is replaced every time. |
| 4 | `POST` | `/auth/logout` | Revoke the session in the refresh cookie and clear it. |
| 5 | `POST` | `/auth/forgot-password` | Email a temporary password; signing in with it leads to choosing a new one (public). |
| 6 | `POST` | `/auth/confirm-email` | Finish an email change from the link sent to the new address (public). |
| 7 | `GET` | `/users/me` | Return the signed-in user's own profile. |
| 8 | `PATCH` | `/users/me` | Change your own full name. |
| 9 | `POST` | `/users/me/password` | Change your own password, given the current one. |
| 10 | `POST` | `/users/me/email` | Ask to change your email; a link is sent to the new address to confirm it. |
| 11 | `DELETE` | `/users/me/email` | Cancel a pending email change. |
| 12 | `GET` | `/users` | List users, with role and active-state filters. |
| 13 | `GET` | `/users/{user_id}` | Return one user by id. |
| 14 | `PATCH` | `/users/{user_id}/role` | Assign a different role to a user. |
| 15 | `PATCH` | `/users/{user_id}/suspend` | Block a user from logging in and kill their current session. |
| 16 | `PATCH` | `/users/{user_id}/reactivate` | Lift a suspension and restore access. |
| 17 | `POST` | `/invitations` | Email someone a one-time link that creates their account with a chosen role. |
| 18 | `GET` | `/invitations` | List invitations, newest first, filterable by status. |
| 19 | `PATCH` | `/invitations/{invitation_id}/revoke` | Kill an invitation's link. |
| 20 | `POST` | `/invitations/preview` | Show an invitee which address and role a token is for (public). |
| 21 | `POST` | `/invitations/accept` | Accept an invitation: create the account and sign in (public). |
| 21a | `POST` | `/account-requests` | Ask the admins for an account (public). |
| 21b | `GET` | `/account-requests` | List account requests, newest first, filterable by status. |
| 21c | `PATCH` | `/account-requests/{request_id}/approve` | Approve a request: email the address an invitation to join as a `Viewer`. |
| 21d | `PATCH` | `/account-requests/{request_id}/reject` | Reject a request and email the address to say so. |
| 21e | `PATCH` | `/account-requests/{request_id}/unblock` | Undo a rejection, so that address may ask again. |
| 22 | `GET` | `/monitoring/projects` | List projects the caller is allowed to see. |
| 23 | `POST` | `/monitoring/projects` | Create a project and name the members responsible for it. |
| 24 | `GET` | `/monitoring/projects/{project_id}` | Return one project with its members. |
| 25 | `PATCH` | `/monitoring/projects/{project_id}` | Update a project's name, description, active flag or extra alert emails. |
| 26 | `DELETE` | `/monitoring/projects/{project_id}` | Delete a project and stop monitoring every site under it. |
| 27 | `POST` | `/monitoring/projects/{project_id}/members` | Add one or more users as responsible members (they start receiving alerts). |
| 28 | `DELETE` | `/monitoring/projects/{project_id}/members/{user_id}` | Remove a member so they stop receiving that project's alerts. |
| 29 | `GET` | `/monitoring/websites` | List monitored sites and their current up/down status. |
| 30 | `GET` | `/monitoring/websites/summary` | Count the sites you can see by state: up, down, pending, maintenance, paused. |
| 31 | `GET` | `/monitoring/websites/events` | Recent outages, recoveries, slow spells and expiring certificates on the sites you can see. |
| 32 | `POST` | `/monitoring/websites` | Start monitoring a URL under a project, optionally with its own recipients. |
| 33 | `GET` | `/monitoring/websites/{website_id}` | Return one monitored site with its live outage state. |
| 34 | `PATCH` | `/monitoring/websites/{website_id}` | Update a site's URL, interval, timeout, alert budget, alert emails or enabled flag. |
| 35 | `DELETE` | `/monitoring/websites/{website_id}` | Stop monitoring a site and delete its check history. |
| 36 | `POST` | `/monitoring/websites/{website_id}/recipients` | Add one or more users alerted about this site only. |
| 37 | `DELETE` | `/monitoring/websites/{website_id}/recipients/{user_id}` | Stop alerting a user about this site. |
| 38 | `GET` | `/monitoring/websites/{website_id}/checks` | Return recent check results, newest first — the evidence behind alerts. |
| 39 | `GET` | `/monitoring/websites/{website_id}/stats` | Uptime and response time over 24h, 7d, 30d or 90d, with a bucketed series for charts. |
| 40 | `POST` | `/monitoring/websites/{website_id}/check` | Probe a site immediately instead of waiting for the next scheduled tick. |
| 40a | `POST` | `/monitoring/websites/{website_id}/maintenance` | Start maintenance now (e.g. for 30 minutes), or schedule it: no checks or alerts until it ends. |
| 40b | `POST` | `/monitoring/websites/{website_id}/maintenance/end` | End the maintenance in effect now. |
| 40c | `DELETE` | `/monitoring/websites/{website_id}/maintenance/{window_id}` | Cancel maintenance that has not started yet. |
| 41 | `GET` | `/monitoring/notifications` | The global notification settings: wording and email/Slack/Telegram/WhatsApp switches per kind. |
| 42 | `PUT` | `/monitoring/notifications/{kind}` | Set the global wording and switches for one kind (admin/DevOps). |
| 43 | `DELETE` | `/monitoring/notifications/{kind}` | Reset a kind's global settings to the built-in wording (admin/DevOps). |
| 44 | `POST` | `/monitoring/notifications/preview` | Render a notification with sample data, exactly as it would be sent. |
| 45 | `GET` | `/monitoring/projects/{project_id}/notifications` | A project's settings after inheriting from global. |
| 46 | `PUT` | `/monitoring/projects/{project_id}/notifications/{kind}` | Override one kind for a project. |
| 47 | `DELETE` | `/monitoring/projects/{project_id}/notifications/{kind}` | Remove a project's override so it inherits again. |
| 48 | `GET` | `/monitoring/projects/{project_id}/report.csv` | Download a monthly uptime report as CSV, one row per site — for anyone who can see the project. |
| 49 | `POST` | `/monitoring/projects/{project_id}/report` | Send a monthly uptime report now instead of waiting for the 1st. |
| 50 | `GET` | `/health` | Liveness check; also reports the running version and whether the monitoring loop is running. |

---

## Auth

Public. No token required.

### `POST /api/v1/auth/signup`
Register the first account. Requires `password` **and** `confirm_password`; the
role cannot be set from the payload. The first account on an empty database is
created as `Admin`; there is no seeded admin. Every later signup gets `403`, and
people join by [accepting an invitation](#post-apiv1invitationsaccept) sent by
an Admin or DevOps user. The `403` comes before
the checks for a taken email or username, so it reveals neither.
`201` · `403` invite-only · `409` taken · `422` mismatch or invalid field ·
`429` rate-limited

### `GET /api/v1/auth/signup`
`{"open": true}` while `POST /auth/signup` would accept someone: always on an
empty database, `{"open": false}` once the admin exists. No sign-in
needed; the sign-in pages use it to hide "Create an account".
`200`

### `POST /api/v1/auth/login`
Authenticate with `identifier` (username **or** email) plus `password`, and
receive the user plus an access token; the refresh token is set as a cookie
(see [the refresh cookie](#the-refresh-cookie)). A temporary password from
forgot password works too, while it lasts: it becomes the password, and the
user comes back with `must_change_password: true`.
`MAX_FAILED_LOGIN_ATTEMPTS` (default 5) wrong passwords in a row lock sign-in
for `LOGIN_LOCKOUT_MINUTES` (default 15), and signing in resets the count. The
attempt that trips the lock still answers `401`, like any wrong password; after
it, every attempt answers `429` with `Retry-After` until the lock runs out,
whether the password is right or not, and does not count. A temporary password
from [forgot password](#post-apiv1authforgot-password) still signs in, so the
owner is never shut out by someone else guessing. The lock does not suspend the
account or end its sessions. Someone allowed to reinstate the account can lift
it early with [reactivate](#patch-apiv1usersuser_idreactivate).
The lock guards one account; `RATE_LIMIT_LOGIN` guards against one client trying
a few passwords on every account instead, counting all its attempts.
`200` · `401` wrong credentials or unknown user · `403` suspended · `422` invalid
· `429` locked after too many wrong passwords, or rate-limited

### `POST /api/v1/auth/forgot-password`
Body: `{"email": "jane@example.com"}`. Emails that account a temporary password
that works for `TEMP_PASSWORD_EXPIRE_MINUTES` (default 60). Always `202` with
the same message, account or not, so it cannot be used to find out who has one;
the email is sent after the response for the same reason. The current password
keeps working until the temporary one is used, so asking for someone else
cannot lock them out, and signing in with the current password cancels the
temporary one. A second request within a minute is ignored. Suspended accounts
are sent nothing.
`202` · `422` invalid address · `429` rate-limited

### `POST /api/v1/auth/confirm-email`
Finish an email change started with `POST /users/me/email`. Body:
`{"token": "<from the email>"}`. No sign-in needed — the token went only to the
new address, so holding it proves the address is yours. The link works once.
`200` the updated user · `403` suspended · `404` no pending change matches (used,
cancelled or replaced) · `409` another account took the address meanwhile ·
`410` expired · `429` rate-limited

### `POST /api/v1/auth/refresh`
No body. Trades the refresh cookie for `{access_token, token_type, expires_in}`
and sets a replacement cookie. Each refresh token works once: presenting one
that was already used means someone else holds a copy, so the whole session is
revoked and everyone on it has to sign in again. A client must therefore never
send two refreshes at once. A session lasts `REFRESH_TOKEN_EXPIRE_DAYS`
(default 7) from its last refresh. Any failure clears the cookie.
`200` · `401` no cookie, or the session expired, was revoked or was reused ·
`403` suspended

### `POST /api/v1/auth/logout`
Revokes the session in the refresh cookie and clears the cookie. No sign-in
needed; always `204`. The access token cannot be revoked and works until it
expires, so the client should discard it.
`204`

#### The refresh cookie
Login, signup and accepting an invitation set `watchly_refresh`. It is
`HttpOnly`, so page scripts cannot read it; `SameSite=Strict`, so it is never
sent on a request another site starts; `Path=/api/v1/auth`, so it goes only to
refresh and logout; and `Secure` unless `REFRESH_COOKIE_SECURE=false`. The
token never appears in a response body, and only its SHA-256 digest is stored.
Suspending a user revokes all their sessions.

---

## Users

| who can call | |
| ------------ | - |
| `/users/me`, `/users/me/…` | any signed-in user, about themselves |
| everything else | admin, DevOps, project manager (per-target rules below) |

### `GET /api/v1/users/me`
Return the signed-in user's own profile, whatever their role — plus
`pending_email` and `pending_email_expires_at` while an email change awaits
confirmation.
`200`

### `PATCH /api/v1/users/me`
Change your full name: `{"full_name": "Jane Doe"}`, or `null`/`""` to clear it.
Any other field is refused — the email has its own endpoint below, and your
role and username are not yours to change.
`200` · `422` unknown field

### `POST /api/v1/users/me/password`
Body: `current_password`, `new_password`, `confirm_password`. Signs out every
session the account has — other devices included — and starts a new one for the
caller, whose refresh cookie is replaced in the response. Access tokens already
issued — the caller's included — are refused from the next request with `401`,
so the caller's client refreshes with its new cookie and every other device is
signed out at once. Signing in with a temporary password does the same, since it
replaces the old password. After signing in with a temporary password this is the only call
allowed — `current_password` is the temporary one — and it clears
`must_change_password`.
`200` · `400` current password wrong, or new = current · `422` mismatch or too short

### `POST /api/v1/users/me/email`
Body: `new_email`, `current_password`. Emails a confirmation link to the new
address (see `POST /auth/confirm-email`); the account keeps its current address
until then, and the response shows the new one as `pending_email`. Asking again
replaces the pending change, and the old link stops working. The link expires
after `EMAIL_CHANGE_EXPIRE_HOURS` (default 24). Like invitations, it needs SMTP
and `ALERT_DASHBOARD_URL`; the change is saved either way, and `email_sent`
says whether the email went out.
`202` · `400` current password wrong, or already your address · `409` another
account uses it · `422` invalid address

### `DELETE /api/v1/users/me/email`
Cancel a pending email change; its link stops working. Nothing happens if there
is none.
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
Lift a suspension and restore login. Also clears `failed_login_attempts` and
`locked_until`, even on an active account, so it lifts a sign-in lockout from
too many wrong passwords before it runs out.
`200` · `403` · `404`

**Who may suspend whom**

| actor ╲ target | admin | DevOps | project manager | developer | viewer | self |
| -------------- | ----- | ------ | --------------- | --------- | ------ | ---- |
| admin | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| DevOps | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ |
| project manager | ❌ | ✅ | ❌ | ✅ | ✅ | ❌ |
| developer, viewer | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

---

## Invitations

Invite someone by email and choose their role up front. Sending and managing
invitations is **admin and DevOps** only, and which roles each may hand out is a
table (`INVITABLE_BY` in `app/core/permissions.py`):

| actor ╲ role handed out | admin | DevOps | project manager | developer | viewer |
| ----------------------- | ----- | ------ | --------------- | --------- | ------ |
| admin | ✅ | ✅ | ✅ | ✅ | ✅ |
| DevOps | ❌ | ✅ | ✅ | ✅ | ✅ |
| everyone else | ❌ | ❌ | ❌ | ❌ | ❌ |

Every invitation comes back as `id`, `email` (lower-cased), `role`, `status`
(`pending`, `accepted`, `revoked` or `expired`), `invited_by_id`,
`invited_by_name`, `expires_at`, `accepted_at`, `revoked_at` and `created_at`.
The token is never part of it. Expired invitations are deleted on the next
monitoring tick, so `expired` is rarely seen, and only the newest
`INVITATION_HISTORY_KEEP` (default 50) accepted and revoked invitations are
kept, each.

### `POST /api/v1/invitations`
Body: `{"email": "jane@example.com", "role": "Developer"}`. The role is required.
Saves the invitation and emails the link (valid for `INVITATION_EXPIRE_DAYS`,
default 7, and usable once). The response adds `email_sent`: `false` means the
invitation exists but the email did not go out (SMTP or `ALERT_DASHBOARD_URL` not
set up, or the mail server failed) — send it again to replace it.
Inviting an address that already has a live invitation **replaces** it: the old
link stops working. DevOps cannot replace an invitation for a role it could not
have sent.
`201` · `403` not admin/DevOps, or may not grant that role · `409` the address
already has an account · `422` bad email or unknown/missing role

### `GET /api/v1/invitations`
Paginated, newest first.
Query: `limit` (1–100), `offset`, `status`
`200` · `403`

### `PATCH /api/v1/invitations/{invitation_id}/revoke`
Kill the link. Idempotent. Needs the right to have sent it, so DevOps cannot
revoke an invitation to Admin.
`200` · `403` · `404` · `409` already accepted

### `POST /api/v1/invitations/preview`
Public. Body: `{"token": "…"}` (from the email). Returns the `email`, `role`,
`invited_by_name` and `expires_at` it is for, without using it up. A POST so the
token stays out of access logs.
`200` · `404` no such invitation, or it expired and was deleted · `410` already
used, revoked or expired, or its
sender is suspended, demoted below the role they offered, or deleted ·
`429` rate-limited

### `POST /api/v1/invitations/accept`
Public. Body: `token`, `username`, `password`, `confirm_password`, and optionally
`full_name`. The email and role come from the invitation and cannot be sent.
Creates the user and returns `{user, tokens}` like signup, so the invitee is
signed in.
`201` · `404` · `409` username taken (the invitation is not used up) or the
address registered meanwhile · `410` as above · `422` passwords differ or invalid
field · `429` rate-limited

---

## Account requests

Someone without an account asks for one from the sign-in page, and an **admin
or DevOps** user answers. Approving sends the address an
[invitation](#invitations) to join as a `Viewer`, so the account is still only
created from an emailed link; rejecting emails the refusal.

Every request comes back as `id`, `email` (lower-cased), `full_name`, `message`,
`status` (`pending`, `approved` or `rejected`), `decided_by_id`,
`decided_by_name`, `approved_at`, `rejected_at` and `created_at`. Only the
newest `ACCOUNT_REQUEST_HISTORY_KEEP` (default 50) approved requests are kept.
A pending one stays until it is answered, or until its address gets an account
some other way; a rejected one stays for good, since it is what stops its
address asking again.

### `POST /api/v1/account-requests`
Public. Body: `{"email": "jane@example.com"}`, and optionally `full_name` and
`message` (why they want an account, up to 500 characters).
Saves the request and emails every active Admin and DevOps user. Always `202`
with the same `detail`, so it cannot be used to find out who has an account:
nothing is saved or sent when the address already has an account, an invitation
it can still accept, a request waiting, or a request that was rejected.
`202` · `422` bad email or field too long · `429` rate-limited

### `GET /api/v1/account-requests`
Paginated, newest first.
Query: `limit` (1–100), `offset`, `status`
`200` · `403`

### `PATCH /api/v1/account-requests/{request_id}/approve`
Creates a `Viewer` invitation for the address, sent by the caller, and emails
its link (valid for `INVITATION_EXPIRE_DAYS` and usable once). The response adds
`email_sent`: `false` means the request is approved and the invitation exists,
but the email did not go out — resend the invitation with `POST /invitations`.
Change the role once the account exists with `PATCH /users/{user_id}/role`.
`200` · `403` not admin/DevOps · `404` · `409` already approved or rejected, or
the address has an account by now (the request stays pending)

### `PATCH /api/v1/account-requests/{request_id}/reject`
Closes the request and emails the address that it was not approved. The address
may not ask again until an admin or DevOps user unblocks it (below) or invites
it directly (`POST /invitations`).
`email_sent` is `false` when that email did not go out. Undo it with `unblock`
below.
`200` · `403` · `404` · `409` already approved or rejected

### `PATCH /api/v1/account-requests/{request_id}/unblock`
Undoes a rejection by deleting the rejected request, so the address may ask for
an account again. Nobody is emailed and nothing is created: they must still ask,
and be approved. To give them access at once, invite them with
`POST /invitations`.
`204` · `403` · `404` · `409` the request is pending or approved

---

## Monitoring — projects

A project groups the URLs of one client or product, and carries the alerting
setup its sites inherit. **Every project must have at least one alert channel**
— email, Slack, Telegram, WhatsApp, or any mix — configured on the project
itself; there are no separate endpoints for setting up a channel.

**Reading** is scoped: admin and DevOps see every project; everyone else —
project managers included — sees only the projects they own or are a member of,
and anything else returns `404` rather than `403`, for writes too.
**Writing** needs admin, DevOps, or being the project's owner (the person who
created it).

### `GET /api/v1/monitoring/projects`
List projects visible to the caller.
Query: `limit` (1–100), `offset`, `is_active`, `owner_id`
`200`

### `POST /api/v1/monitoring/projects`
Create a project. **At least one alert channel is required** — email
(`member_ids` and/or `extra_emails`), Slack (`slack_bot_token` **with**
`slack_channel_id`), Telegram (`telegram_bot_token` **with**
`telegram_chat_id`), WhatsApp (`whatsapp_access_token` **with**
`whatsapp_phone_number_id` and `whatsapp_recipients`), or any mix. The caller
becomes its owner.
`201` · `403` not admin/DevOps/PM · `409` name taken · `422` no alert channel,
half-configured Slack, Telegram or WhatsApp, a phone number without its
country code, or unknown member id

### `GET /api/v1/monitoring/projects/{project_id}`
Return one project with its member list.
`200` · `404` missing **or** not visible to the caller

### `PATCH /api/v1/monitoring/projects/{project_id}`
Update `name`, `description`, `is_active`, `extra_emails`, the Slack
settings (`slack_bot_token`, `slack_channel_id`, `slack_enabled`) or the
Telegram settings (`telegram_bot_token`, `telegram_chat_id`,
`telegram_enabled`) or the WhatsApp settings (`whatsapp_access_token`,
`whatsapp_phone_number_id`, `whatsapp_recipients`, `whatsapp_enabled`).
Supplying `extra_emails` or `whatsapp_recipients` replaces the whole list.
Sending `slack_bot_token: null` (or `slack_channel_id: null`) turns Slack off
entirely — both halves are cleared; the same goes for Telegram, and for
WhatsApp with any of its three fields null (or `whatsapp_recipients: []`).
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
non-member is a no-op. Refused if they are the last member and no Slack,
Telegram or WhatsApp is configured.
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
`is_enabled`, `project_id`, `check_type` (`http`, `ping` or `dns`), `q` (name or URL
contains, case-insensitive), `in_maintenance` (`true`: only sites in a
maintenance window now; `false`: only the rest), `sort` (`id` default, `name`,
or `status`: down sites first, then by name; a site down during its
maintenance is not put first)
`200`

### `GET /api/v1/monitoring/websites/summary`
How many of the sites you can see are `up`, `down`, `unknown` (enabled, not
checked yet), in `maintenance` (enabled and in a maintenance window, whatever
their status) and `paused`, plus the `total` they add up to. Takes the list's `project_id`,
`check_type` and `q`, so a dashboard can page through one state and still show
every count.
`200`

### `GET /api/v1/monitoring/websites/events`
The alert feed the app shows as toasts: each `down`, `recovered`,
`slow_response`, `packet_loss`, `dns_changed` and `ssl_expiring` event on the
sites you can see, newest first, with its `website` (`id`, `name`, `url`, `check_type`,
`environment`), `occurred_at`,
the check's one-line `summary` (for `dns_changed`, the records now and before),
and the figures its kind uses
(`downtime_seconds`, `threshold_ms` with `response_time_ms`,
`ssl_expires_at`). Recorded with the check that raised it, whether or not any
channel is on for that kind; an outage appears once, when it starts. Kept for
`CHECK_RETENTION_DAYS`. `total` counts every match, which may be more than
`items`.
Query: `after_id` (only events after this one: poll with the newest id you
have), `limit` (1–100, default 20)
`200`

### `POST /api/v1/monitoring/websites`
Start monitoring a URL, pinging a host, or watching a DNS record. Requires
`project_id`; the project's members are alerted unless
`inherit_project_recipients` is false.
Body: `project_id`, `name`, `url`, and optionally `check_type` (`http`, the
default; `ping`: then `url` is a host name or IP address such as
`203.0.113.10` or `server.example.com`, not a URL; or `dns`: then `url` is a
domain name such as `example.com` or `_dmarc.example.com`; fixed once created),
`ping_count` (1–20, default 5: echo requests per ping check),
`packet_loss_threshold_percent` (1–99, `null` for the server default: a ping
check that loses at least this share while the host answers counts as lossy),
`dns_record_type` (`A`, the default, `AAAA`, `CNAME`, `MX` or `TXT`: the record a
DNS check looks up), `dns_expected_values` (up to 20, e.g. `["203.0.113.10"]`,
`["10 mx1.example.com"]` or a TXT value, quoted or not: any resolver answering
otherwise makes the check down with `dns_mismatch`; empty, the check learns
the records and sends `dns_changed` when they change),
`environment` (`development`,
`testing`, `uat`, `staging` or `production` — the web form requires it, the API
does not, so existing scripts keep working), `method`, `expected_status`,
`timeout_seconds` (for a ping, the wait for replies; default 2 instead of
10; for a DNS check, the wait for each resolver; default 5),
`check_interval_seconds` (≥30), `max_down_alerts`,
`retries_on_failure` (0–3, default 1: a failed check is repeated a few seconds
later, and only the last result is recorded and alerted on),
`must_contain` / `must_not_contain` (the body must have, or must not have, this
text — case-sensitive, first 1 MB; needs GET or POST; a failure has
`error_type` `content_missing` / `content_forbidden`),
`is_enabled`, `recipient_ids` (users alerted about this site only),
`alert_emails` (addresses alerted about this site only),
`inherit_project_recipients` (default `true`), `slack_channel_id` (post to
this channel instead of the project's), `slack_bot_token` (the site's own bot,
for when the project has no Slack; needs `slack_channel_id`), the same for
Telegram: `telegram_chat_id` and `telegram_bot_token`, and
`whatsapp_recipients` (WhatsApp these numbers instead of the project's, from
the project's business number; needs WhatsApp on the project)
`201` · `403` not your project · `404` no such project · `409` URL already
monitored with this check type (for DNS, this record of the domain) · `422`
invalid field, unknown recipient id, the site would have no alert channel, its
Slack, Telegram or WhatsApp settings send nowhere, a `url` that does not suit
the `check_type`, an expected value that does not suit the record type, or
content rules on a HEAD/OPTIONS request, a ping or a DNS check

### `GET /api/v1/monitoring/websites/{website_id}`
Return one site with its live state: `status`, `last_checked_at`, `down_since`,
`consecutive_failures`, `down_alerts_sent`; for a DNS check, `dns_record_type`,
`dns_expected_values` and `dns_records` (what the resolvers last agreed on) —
and its alerting setup:
`recipients`, `alert_emails`, `inherit_project_recipients`, `alert_channels`,
`slack_channel_id`, `telegram_chat_id`, `whatsapp_recipients`, and
`slack_token_hint` / `telegram_token_hint` when the site has its own bot —
and its `maintenance` (the window in effect now, or null) and
`upcoming_maintenance` (windows still to come, soonest first), each with `id`,
`starts_at`, `ends_at`, `reason` and `created_by_id`. The list returns the
same fields for every site.
`200` · `404` missing **or** not visible

### `PATCH /api/v1/monitoring/websites/{website_id}`
Update any of the check settings, `alert_emails` (replaces the whole list),
`inherit_project_recipients`, the site's Slack (`slack_channel_id: null`
removes it, token included; `slack_bot_token: null` goes back to the project's
token), the site's Telegram (`telegram_chat_id` and `telegram_bot_token`, the
same way), the site's WhatsApp numbers (`whatsapp_recipients`, replacing the
list; `[]` goes back to the project's), `retries_on_failure`, the content rules
(`null` removes one), or disable the site without deleting it. A change that would leave the site with no alert
channel is refused. `check_type` cannot change, and a new `url` must suit it.
A DNS check's `dns_expected_values` replaces the whole list (`[]` unpins); a new
`dns_record_type` or domain forgets the records learned so far, and a new
record type clears the pinned values unless new ones are sent.
`200` · `403` · `404` · `409` URL already monitored · `422` would leave no
alert channel, Slack, Telegram or WhatsApp settings that send nowhere, a `url`
that does not suit the check type, or expected values that do not suit the
record type

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
Recent check results, newest first — for each poll the status code and
reason, response time, error and `error_type`, final URL, diagnostic headers,
and the time spent in each step (`dns_ms`, `connect_ms`, `tls_ms`,
`first_byte_ms`; null when the step did not finish). Checks recorded before
these fields existed have them null. A ping check has `ping` instead: the
`address` pinged (what a host name resolved to), packets `sent` and `received`,
`loss_percent`, and round trips `min_ms` / `avg_ms` / `max_ms` / `jitter_ms`;
its `response_time_ms` is the average round trip, rounded, and `dns_ms` the
name lookup. A DNS check has `dns`: the `record_type`, the `expected` values
pinned at the time, the `records` the resolvers agreed on (null when they
disagreed or fewer than half answered), `consistent`, and each resolver's
`answers` — `resolver`, `address`, `records`, `ttl`, `time_ms`, and `error` /
`error_type` (`nxdomain`, `no_records`, `servfail`, `refused`, `timeout`,
`network_error`, `dns_error`); its `response_time_ms` is the resolvers' average
answer time.
Query: `limit` (1–500)
`200` · `404`

### `GET /api/v1/monitoring/websites/{website_id}/stats`
Uptime, average and 95th-percentile response time over `range` (`24h`, `7d`,
`30d` or `90d`; default `24h`), and `series`: every bucket in the range, oldest
first — UTC hours for 24h and 7d, UTC days for 30d and 90d, the last one still
filling. A bucket with no checks has `checks: 0` and nulls. Read from hourly
rollups, so it reaches back past the retention of raw checks. Uptime is the
share of checks that succeeded; response times count successful checks only,
as in the monthly report; the percentile is accurate to within about 25%.
For a ping check the response time is the average round trip, and
`packet_loss_percent` (whole range and per bucket; null for HTTP checks) the
share of pings lost — also a CSV column for ping checks. For a DNS check it is
the resolvers' average answer time.
`format=csv` downloads the series as a file instead (one row per bucket, blank
figures for an empty one).
`200` · `404` · `422` unknown range or format

### `POST /api/v1/monitoring/websites/{website_id}/check`
Probe now, through the same state machine as a scheduled check, so it can raise
and clear alerts. The fastest way to test a new site or your SMTP setup.
Returns the site, the check, and which notification it raised (if any): `down`,
`still_down`, `recovered`, `ssl_expiring`, `slow_response`, `packet_loss` or
`dns_changed`. During [maintenance](#maintenance-windows) the check is recorded
but raises and clears nothing.
`200` · `403` · `404` · `503` a ping check, and this server is not allowed to
send pings (see `PING_PRIVILEGED` in the [technical reference](reference.md#ping-checks))

#### Maintenance windows

For deployments and other work that is expected to take a site down. While a
window is in effect the scheduler does not check the site, so nothing alerts
and its uptime is not dinged. When the window ends the site is checked on the
next tick, picking up from the state it was in before the window: still down
alerts as usual, and a site that was down before the window and is up now
announces its recovery. A check made by hand during the window is kept in the
history but changes nothing and alerts nobody. A site's windows never overlap,
and one lasts at most 7 days (pause the site for longer). Ended windows are
kept for `CHECK_RETENTION_DAYS`, then purged. All three calls need rights over
the site's project, and return the site.

### `POST /api/v1/monitoring/websites/{website_id}/maintenance`
Start maintenance now, or schedule it. Body: `ends_at` **or**
`duration_minutes` (1–10080), plus optional `starts_at` (omitted, or already
past, means now) and `reason` (up to 255 characters, shown on the site's page).
Times without a timezone are read as UTC.

```json
{"duration_minutes": 30, "reason": "Deploying 2.4"}
{"starts_at": "2026-10-01T22:00:00Z", "ends_at": "2026-10-01T23:30:00Z", "reason": "Database upgrade"}
```
`201` the site, with the window in `maintenance` or `upcoming_maintenance` ·
`403` · `404` · `409` overlaps a window the site already has · `422` both or
neither of `ends_at` and `duration_minutes`, an end that is not after the start
or not in the future, or longer than 7 days

### `POST /api/v1/monitoring/websites/{website_id}/maintenance/end`
End the window in effect now; the site is checked again on the next tick. The
window stays on record, ending now. Does nothing when no window is in effect,
so a click that lands just after it ran out is harmless.
`200` · `403` · `404`

### `DELETE /api/v1/monitoring/websites/{website_id}/maintenance/{window_id}`
Cancel a window that has not started yet. One that has started is ended with
`POST …/maintenance/end` instead.
`200` · `403` · `404` no such upcoming window on this site · `409` it has
already started

A website also takes an optional `slow_threshold_ms` (send `null` for the
server default), an `environment` (send `null` to clear it; sites that predate
the field have none), and reports a read-only `ssl_expires_at` for HTTPS sites.

---

## Monitoring — notifications

`{kind}` is one of `down`, `still_down`, `recovered`, `ssl_expiring`,
`slow_response`, `packet_loss`, `dns_changed`, `monthly_report`. Settings resolve project → global → built-in
default, field by field; see the [technical reference](reference.md#what-else-watchly-tells-you-about) for what each kind is.

Every setting comes back as:
`kind`, `label`, `description`, `audience`, the effective `email_enabled`,
`slack_enabled`, `telegram_enabled`, `whatsapp_enabled`, `subject`, `body`,
where each came from (`sources`:
`project` / `global` / `default`), what this level itself stores (`overrides`,
null = inherits), what a blank field would fall back to (`inherited_subject`,
`inherited_body`), the built-in `default_subject` / `default_body`, and the
`placeholders` a template may use.

### `GET /api/v1/monitoring/notifications`
Global settings for every kind. Any signed-in user.
`200`

### `PUT /api/v1/monitoring/notifications/{kind}`
Replace the global overrides for one kind. Send every field
(`email_enabled`, `slack_enabled`, `telegram_enabled`, `whatsapp_enabled`,
`subject`, `body`): one left null or blank inherits, and all of them null
removes the override.
Admin/DevOps only.
`200` · `403` · `422` unknown placeholder, too long (subject 255, body 4000), or
it would switch "Site down" off on every channel

### `DELETE /api/v1/monitoring/notifications/{kind}`
Reset to the built-in wording and switches. Admin/DevOps only.
`200` · `403`

### `POST /api/v1/monitoring/notifications/preview`
Body: `kind`, and optionally `subject` and `body` (omitted means the built-in
default). Returns the rendered `subject`, `email_html`, `email_text`, and the
Slack `slack_text` and `slack_blocks`, the Telegram `telegram_html`, and the
WhatsApp `whatsapp_text` (the approved template filled in, or the free-form
text when `WHATSAPP_TEMPLATE_NAME` is blank), built from sample data by the
same code that sends real messages. Nothing is saved or sent.
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

### `GET /api/v1/monitoring/projects/{project_id}/report.csv`
The figures of the emailed monthly report as a CSV file, one row per site that
had checks: uptime, downtime, incidents, longest outage, average and 95th
percentile response time. For anyone who can see the project; sends nothing.
Query: `month` (`YYYY-MM`, a month that has ended; default last month)
`200` · `404` · `409` no checks that month, the month has not ended, or its
checks are older than `CHECK_RETENTION_DAYS` allows · `422` bad month

### `POST /api/v1/monitoring/projects/{project_id}/report`
Build and send a monthly uptime report now. Body (optional): `month` as
`YYYY-MM`, a month that has ended; defaults to last month. Follows the
project's notification switches. Returns the `month`, how many `sites` it
covered, `email_recipients`, `slack_configured`, `telegram_configured`,
`whatsapp_configured`, and `delivered_by` — empty means nothing was delivered
(switched off, no channel set up, or sending failed; see the log).
Does not stop the scheduled report.
`200` · `403` · `404` · `409` no checks that month, or the month has not ended

---

## Health

### `GET /health`
Public liveness check. Returns `{"status": "ok", "version": "1.0.0", "monitoring": "on"|"off"}`,
`version` being the running release.
`200`

---

## Conventions

**Status codes.** Beyond the ones listed, any authenticated endpoint may return
`401` (missing, malformed or expired access token; the frontend then tries
`POST /auth/refresh` once) and `403` (the account has been suspended).

**Roles.** `Viewer` · `Admin` · `DevOps` · `Project Manager` · `Developer`.
Everyone who signs up on their own starts as `Viewer`; an invited user starts with
the role their invitation named.

**Errors.** `{"detail": "..."}` for single errors; `422` returns FastAPI's
validation list. Password fields are redacted from `422` bodies.

**Pagination.** List endpoints take `limit` and `offset` and return
`{items, total, limit, offset}`. `total` counts what the caller may see, not
what exists.

**Timestamps.** ISO 8601, UTC.

**Secrets.** A project's or site's `slack_bot_token` and `telegram_bot_token`,
and a project's `whatsapp_access_token`, are write-only: they are encrypted
before storage and never appear in a response. Reads return a masked
`slack_token_hint` such as `xoxb-…9f2a`, `telegram_token_hint` such as
`123456789:…wxyz` or `whatsapp_token_hint` such as `EAA…wxyz` instead (and, for
a project, `slack_configured` / `telegram_configured` / `whatsapp_configured`).

---

## Not built yet

- Revoking one device's access token at sign-out. Logout revokes that
  session's refresh token, but its access token works until it expires.
  Ending every session (password change, temporary password, suspension)
  does revoke access tokens, through `users.session_version`.
- Audit trail for role changes, suspensions, invitations and project edits.
- Retrying a monthly report that nobody received. It is sent at most once per
  project per month; use `POST /monitoring/projects/{id}/report` to resend.
- An audit trail of who changed a notification template.
