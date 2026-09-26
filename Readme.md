# Watchly

FastAPI backend with async SQLAlchemy 2.0, PostgreSQL, Pydantic v2 and Alembic.

## Layout

```
app/
  auth/
    routes.py        # signup / login / forgot password / confirm new email
    service.py       # AuthService — lookups, registration, authentication
    schemas.py       # SignupRequest / LoginRequest / TokenResponse / *Response
    dependencies.py  # get_current_user, require_roles (Bearer + RBAC)
    mail.py          # the temporary-password email
  user/
    routes.py        # /users/me (profile, password, email change), list, read, change role, suspend/reactivate
    service.py       # UserService — own account, directory, role changes, suspension
    schemas.py       # Profile* / PasswordChangeRequest / EmailChange* / RoleUpdateRequest / UserListResponse
    mail.py          # the email confirming a new address
  invitations/
    routes.py        # invite, list, revoke (admin/DevOps); preview + accept (public)
    service.py       # InvitationService — who may grant which role, token lifecycle
    schemas.py       # InvitationCreate / InvitationRead / AcceptInvitationRequest
    mail.py          # the invitation email's wording (sent by core/mail.py)
  monitoring/
    routes.py        # aggregates the sub-routers below
    service.py       # MonitoringService — the outage state machine
    scheduler.py     # background loop that drives the checks
    projects/
      models.py      # Project + project_members + Slack settings
      schemas.py     # Pydantic contracts
      service.py     # ProjectService — CRUD + membership
      routes.py      # project + member endpoints
    websites/
      models.py      # Website + website_recipients + WebsiteCheck
      schemas.py     # Pydantic contracts
      service.py     # WebsiteService — CRUD, due-selection
      checker.py     # the HTTP probe
      routes.py      # website endpoints
    alerts/          # what gets sent, and how each channel draws it
      base.py        # NotificationKind, the channel-neutral Message, Alerter interface
      events.py      # outage / SSL / slow / monthly-report events
      email.py       # SMTP — HTML + plain-text layouts
      slack.py       # Block Kit + per-project bot token and channel
      webhook.py     # generic JSON POST (off unless configured)
    notifications/   # who wants what, in which words
      catalog.py     # the kinds, their placeholders, the built-in wording
      templating.py  # {{placeholder}} substitution, escaping per channel
      service.py     # settings: project override > global > built-in default
      dispatcher.py  # look up settings, render, fan out to channels
      reports.py     # monthly uptime report: stats, schedule, send
      models.py      # notification_settings, report_deliveries
      routes.py      # settings, preview and report endpoints
  core/
    config.py        # pydantic-settings, DB DSNs, JWT settings
    security.py      # generate_hash_password / verify_password (bcrypt), emailed-link tokens
    mail.py          # one-link account emails (invitations, email confirmation) over SMTP
    handlers.py      # 422 handler that redacts passwords from echoed input
    crypto.py        # Fernet encryption for stored Slack tokens
    permissions.py   # who may manage, see, suspend and invite whom
  utils/
    jwt.py           # access-token helpers (refresh tokens live in the DB)
  db/
    base.py          # DeclarativeBase + TimestampMixin
    session.py       # async engine, session factory, get_db dependency
    seed.py          # idempotent admin seeder
    models/
      user.py        # User model + UserRole enum
      invitation.py  # Invitation model + status
      refresh_token.py # RefreshToken: one row per issued refresh token
  schemas/
    user.py          # UserCreate / UserRead / UserUpdate / UserInDB
  main.py            # FastAPI app
alembic/             # migration environment (async)
alembic.ini
docker-compose.yml   # PostgreSQL 16 + API + web UI
Dockerfile           # API image (migrates and seeds on start)
docker/              # container entrypoint
```

## Setup

New to the project? [`doc/local-setup.md`](doc/local-setup.md) walks through
running Watchly from a fresh clone, seeing alert emails locally, developing
with hot reload, and troubleshooting.

### Everything in Docker

```bash
docker compose up -d --build
```

| service | URL | what it is |
| ------- | --- | ---------- |
| `web` | http://localhost:8080 | React UI; nginx also proxies `/api` to the API |
| `api` | http://localhost:8000/docs | FastAPI + the monitoring scheduler |
| `db`  | `localhost:5432` | PostgreSQL 16, data in the `watchly_pgdata` volume |

The API container runs `alembic upgrade head` and seeds the admin every time
it starts. Both steps are idempotent, so sign in at http://localhost:8080 as
`admin` / `Admin@123`.

A `.env` file is optional. Without one, the defaults from `app/core/config.py`
apply (and the API warns that `SECRET_KEY` is the default). With one, every
setting in it reaches the API, except `POSTGRES_HOST` and `POSTGRES_PORT`:
compose points those at the `db` container. Set `WEB_PORT`, `API_PORT` or
`POSTGRES_PORT` to move the published ports.

After changing code, rebuild the affected service with
`docker compose up -d --build api` (or `web`). `docker compose logs -f api`
shows the scheduler and probe output. `docker compose down` stops everything,
and `docker compose down -v` also deletes the database.

### Running the API on the host

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Start only PostgreSQL:

```bash
docker compose up -d db
```

Run migrations, then seed the admin user:

```bash
alembic upgrade head
python -m app.db.seed
```

Run the API:

```bash
uvicorn app.main:app --reload
```

Docs at http://127.0.0.1:8000/docs, health check at `/health`.

Run the web UI (needs Node 20+):

```bash
cd frontend && npm install && npm run dev
```

It serves on http://localhost:5173 and proxies API calls to port 8000. See
[`frontend/README.md`](frontend/README.md).

- [`doc/local-setup.md`](doc/local-setup.md) — running it locally from scratch,
  step by step
- [`doc/hld.md`](doc/hld.md) — high-level design: architecture, data model,
  key flows and known limits
- [`doc/apis.md`](doc/apis.md) — every endpoint with a one-line description
- [`app/monitoring/websites/README.md`](app/monitoring/websites/README.md) —
  how the monitoring pipeline works end to end

## Seeded admin

| field    | value             |
| -------- | ----------------- |
| username | `admin`           |
| email    | `admin@gmail.com` |
| password | `Admin@123`       |
| role     | `Admin`           |

Overridable via the `FIRST_ADMIN_*` variables in `.env`. `seed_admin()` is
idempotent — it skips if the username or email already exists.

## Auth

### `POST /api/v1/auth/signup`

Registers an account. The role is **always** `Viewer` — it is not accepted from
the payload, so it cannot be escalated by the client.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"username":"jane","email":"jane@example.com","full_name":"Jane Doe",
       "password":"Str0ng@Pass","confirm_password":"Str0ng@Pass"}'
```

`201` returns the created user plus an access token, and sets the refresh
token as an httpOnly cookie (see [Staying signed in](#staying-signed-in-and-logging-out)):

```json
{
  "user": { "id": 2, "username": "jane", "role": "Viewer", "...": "..." },
  "tokens": {
    "access_token": "eyJ...",
    "token_type": "bearer",
    "expires_in": 1800
  }
}
```

| status | when                                            |
| ------ | ----------------------------------------------- |
| `201`  | created                                         |
| `409`  | email or username already registered            |
| `422`  | passwords do not match, or a field is invalid   |

### `POST /api/v1/auth/login`

`identifier` is matched against **username or email** — one field, either value.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"identifier":"jane@example.com","password":"Str0ng@Pass"}'
```

Returns the same `{user, tokens}` shape as signup, and stamps `last_activity`
on the user.

| status | when                                                   |
| ------ | ------------------------------------------------------ |
| `200`  | authenticated                                          |
| `401`  | no such user **or** wrong password (indistinguishable) |
| `403`  | credentials valid but the account is inactive          |
| `422`  | a field is missing or invalid                          |

Two deliberate choices here, both to stop account enumeration:

- An unknown identifier still runs a bcrypt verification against a dummy hash,
  so response timing does not reveal which accounts exist (measured: ~171ms vs
  ~175ms, a ratio of 1.02).
- `is_active` is only checked *after* the password is proven, so a wrong
  password returns `401` whether or not the account exists or is disabled.

### Staying signed in and logging out

Login, signup and accepting an invitation set a `watchly_refresh` cookie:
`HttpOnly`, `SameSite=Strict`, `Path=/api/v1/auth`, and `Secure` unless
`REFRESH_COOKIE_SECURE=false`. `POST /auth/refresh` trades it for a new access
token and replaces it. Each refresh token works once. Presenting a used one
revokes the whole session, since someone else must hold a copy.

```bash
curl -c jar -b jar -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' -d '{"identifier":"admin","password":"Admin@123"}'
curl -c jar -b jar -X POST http://127.0.0.1:8000/api/v1/auth/refresh
```

(curl only sends a `Secure` cookie over HTTPS, or to `localhost`.)

`POST /auth/logout` revokes the session and clears the cookie. That device's
access token is a JWT and stays usable until its `exp` even after that, so keep
`ACCESS_TOKEN_EXPIRE_MINUTES` short.

Ending *all* of a user's sessions revokes their access tokens as well. Each
token carries the user's `session_version` (claim `sv`), which a password
change, a sign-in with a temporary password and a suspension all bump;
`get_current_user` already loads the user on every request, so a token with an
older version is refused with `401` at no extra cost. Tokens issued before this
existed count as version 0 and keep working until the account's first bump.

### Protecting your own routes

`app/auth/dependencies.py` is reusable for any endpoint:

```python
from fastapi import Depends
from app.auth.dependencies import get_current_user
from app.db.models.user import User

@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> UserRead:
    return UserRead.model_validate(user)
```

`get_current_user` rejects missing, malformed and expired access tokens and
returns `403` for a deactivated account. Use `get_current_claims` when you only need the claims and want to skip
the user lookup.

### JWT utils

`app/utils/jwt.py` has no FastAPI imports, so services, workers and scripts can
reuse it. It raises `TokenError`; the caller decides the HTTP status.

```python
from app.utils.jwt import create_access_token, decode_access_token, TokenError

token = create_access_token(subject=str(user.id),
                            extra_claims={"username": user.username, "role": user.role.value})

try:
    claims = decode_access_token(token)
except TokenError:
    ...
```

Claims carried: `sub`, `type`, `iat`, `nbf`, `exp`, `jti`, plus anything in
`extra_claims`. `decode_access_token` enforces `type: access`. Refresh tokens
are not JWTs; `AuthService` issues, rotates and revokes them.

Set `SECRET_KEY` in `.env` (`openssl rand -hex 32`) — the app logs a warning at
startup while it is still the default.

## User management

Everyone who signs up on their own is a `Viewer`; anyone who is
[invited](#inviting-users) gets the role the invitation names. **Admin and
DevOps are peers** — both can administer users; no other role can.

| endpoint                             | who                           |
| ------------------------------------ | ----------------------------- |
| `GET /api/v1/users/me`               | any signed-in user            |
| `PATCH /api/v1/users/me`, `POST /api/v1/users/me/password`, `POST`/`DELETE /api/v1/users/me/email` | any signed-in user, about themselves |
| `POST /api/v1/auth/confirm-email`    | anyone holding the emailed link |
| `POST /api/v1/auth/forgot-password`  | anyone (same answer whether or not the address has an account) |
| `GET /api/v1/users`                  | admin, DevOps, project mgr    |
| `GET /api/v1/users/{id}`             | admin, DevOps, project mgr    |
| `PATCH /api/v1/users/{id}/role`      | admin, DevOps\*               |
| `PATCH /api/v1/users/{id}/suspend`   | admin, DevOps, project mgr\*  |
| `PATCH /api/v1/users/{id}/reactivate`| admin, DevOps, project mgr\*  |
| `POST /api/v1/invitations`           | admin, DevOps\*               |
| `GET /api/v1/invitations`            | admin, DevOps                 |
| `PATCH /api/v1/invitations/{id}/revoke` | admin, DevOps\*            |

\* Which *targets* each may act on depends on the target's role — see
**Who may manage whom** below.

```bash
curl -X PATCH http://127.0.0.1:8000/api/v1/users/7/role \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' -d '{"role":"Developer"}'

curl -X PATCH http://127.0.0.1:8000/api/v1/users/7/suspend \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Listing supports `limit` (1–100, default 50), `offset`, `role` and `is_active`:

```bash
curl -G http://127.0.0.1:8000/api/v1/users \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode 'role=Viewer' --data-urlencode 'limit=20'
```

| status | when                                              |
| ------ | ------------------------------------------------- |
| `200`  | done                                              |
| `401`  | no or invalid token                               |
| `403`  | caller is not admin/DevOps, or is editing itself  |
| `404`  | no such user                                      |
| `422`  | unknown role, or a bad path/query value           |

### Who may manage whom

The whole policy lives in [`app/core/permissions.py`](app/core/permissions.py).
Suspension and role changes have different rules, so they are two tables.

**Suspend / reinstate** — `SUSPENDABLE_BY`:

| actor ╲ target      | admin | DevOps | project manager | developer | viewer | self |
| ------------------- | ----- | ------ | --------------- | --------- | ------ | ---- |
| **admin**           | ✅    | ✅     | ✅              | ✅        | ✅     | ❌   |
| **DevOps**          | ❌    | ❌     | ✅              | ✅        | ✅     | ❌   |
| **project manager** | ❌    | ✅     | ❌              | ✅        | ✅     | ❌   |
| developer, viewer   | ❌    | ❌     | ❌              | ❌        | ❌     | ❌   |

The same table decides who may unlock an account suspended for
`MAX_FAILED_LOGIN_ATTEMPTS` (default 5) wrong passwords in a row: reactivating
it clears the count. If nobody may — the only admin locked themselves out —
reinstate it from the server with `python -m app.db.reactivate <username>`.

This is a table, not a hierarchy — DevOps and project managers can each suspend
the other. Note a project manager cannot suspend a fellow project manager.

**Change role** — `can_change_role()`:

| actor ╲ target      | admin | DevOps | others | self |
| ------------------- | ----- | ------ | ------ | ---- |
| **admin**           | ✅    | ✅     | ✅     | ❌   |
| **DevOps**          | ❌    | ❌     | ✅     | ❌   |
| everyone else       | ❌    | ❌     | ❌     | ❌   |

Project managers can suspend but cannot re-role anyone. That separation is what
keeps suspension rules honest: a role that could re-role its way around a
suspension restriction would make the restriction meaningless — demote the
target first, then suspend. DevOps is barred from re-rolling admins and DevOps
peers for exactly this reason.

**Invite** — `INVITABLE_BY` / `can_invite_role()`:

| actor ╲ role handed out | admin | DevOps | project manager | developer | viewer |
| ----------------------- | ----- | ------ | --------------- | --------- | ------ |
| **admin**               | ✅    | ✅     | ✅              | ✅        | ✅     |
| **DevOps**              | ❌    | ✅     | ✅              | ✅        | ✅     |
| everyone else           | ❌    | ❌     | ❌              | ❌        | ❌     |

Unlike re-roling, this is about the role an invitation *hands out*, not what the
recipient already is — they have no account yet — so DevOps may invite another
DevOps. To let another role invite, add a row to `INVITABLE_BY`; the endpoints'
guard is built from its keys.

Reading the directory (`GET /users`, `GET /users/{id}`) is open to all three
management roles, since you cannot pick someone to suspend without finding them
first.

Other rules worth knowing:

- **Authorization reads the role and `is_active` from the database**, never from
  the token. A demoted or suspended user loses access on their very next
  request instead of keeping it until their access token expires. The token's
  `role` claim is a convenience for the frontend only — never trust it for
  access control.
- **Nobody can change or suspend their own account**, which is also what keeps
  the system administrable: the actor is always an active admin/DevOps user and
  is never the target, so no single action can leave you with zero
  administrators.

### Suspension

Suspending sets `is_active = False`. There is no separate flag and no migration
for it — `is_active` is exactly this state. It has two effects, both already
enforced:

- **Login refuses the account**: `POST /auth/login` returns `403 "This account
  has been suspended."` even with the correct password. A *wrong* password still
  returns the usual `401`, so the endpoint never reveals that an account is
  suspended to someone who cannot authenticate as it.
- **Existing tokens stop working**: `get_current_user` re-reads `is_active` on
  every request, so a token minted before the suspension is refused with `403`
  immediately rather than staying live until it expires.

`PATCH /users/{id}/reactivate` reverses it. Both endpoints are idempotent.

To guard your own endpoints:

```python
from app.auth.dependencies import (
    require_role_manager,   # admin, DevOps
    require_user_manager,   # admin, DevOps, project manager
    require_roles,
)
from app.db.models.user import UserRole

@router.post("/deploys", dependencies=[Depends(require_role_manager)])
async def create_deploy(): ...

# or an arbitrary set
approver = require_roles(UserRole.ADMIN, UserRole.PROJECT_MANAGER)
```

The route dependency is the coarse gate — "does this role have any business
here at all". The per-target rules (`can_change_role`, `can_suspend_user`) are
enforced in the service layer, where the target is known.

## Inviting users

An admin or DevOps user invites someone by email and picks their role up front.
The invitee opens the link, chooses a username and password, and lands signed in
with that role. Who may hand out which role is the `INVITABLE_BY` table above.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/invitations \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"email":"jane@example.com","role":"Developer"}'
```

`201` returns the invitation (never the token) plus `email_sent`. The invitation
is saved even if the email cannot be delivered — SMTP or `ALERT_DASHBOARD_URL`
not set up, or the mail server failed — and `email_sent: false` says so.

| status | when                                                                  |
| ------ | --------------------------------------------------------------------- |
| `201`  | saved (check `email_sent`)                                            |
| `403`  | not admin/DevOps, or you may not hand out that role                   |
| `409`  | the address already has an account                                    |
| `422`  | bad email, unknown role, or no role (there is no default)             |

- **One live invitation per address.** Inviting an address again replaces the
  old invitation: its link stops working and a fresh one is emailed. That is how
  you resend one. DevOps cannot replace or revoke an invitation for a role it
  could not have sent (an Admin invitation, say).
- **The link works once** and expires after `INVITATION_EXPIRE_DAYS` (default 7).
- **The token is the proof of owning the address.** It is emailed and never
  returned by the API; only its SHA-256 digest is stored. Accepting takes the
  email and role from the invitation, not from the request.
- **The sender must still qualify.** If the person who sent an invitation is
  suspended, demoted below the role they offered, or deleted before it is
  accepted, the link stops working. Otherwise suspending a compromised account
  would leave the invitations it already sent live.
- Invitations use the alert SMTP settings (`SMTP_*`) and build the link from
  `ALERT_DASHBOARD_URL`, but are not alerts: `ALERT_EMAIL_ENABLED` does not
  affect them.

`GET /invitations` lists them (newest first; filter with `status=pending`,
`accepted`, `revoked` or `expired`), and `PATCH /invitations/{id}/revoke` kills
a link. The invitee's two calls need no sign-in — `POST /invitations/preview`
shows which address and role a token is for, and `POST /invitations/accept`
creates the account and returns the same user-plus-tokens body as signup. The
token goes in the body, not the URL, to keep it out of access logs. A dead link
answers `410` with the reason; an unknown token, `404`.

In the web UI: **Users → Invite user** (the role list offers only roles you may
grant), and the invitee's page is `/accept-invite?token=…`.

## Uptime monitoring

The core feature: poll each registered site on its own interval, and email the
right people the moment it stops answering. A site is a URL to request over
HTTP(S), a host to **ping** (see [Ping checks](#ping-checks)), or one **DNS
record** of a domain (see [DNS checks](#dns-checks)).

### Projects come first

A site is always monitored **under a project**. The project's **members** are
alerted about all of its sites, and each site can add **its own recipients**.
The flow is:

**1. Create the project and name the people responsible for it.**

```bash
curl -X POST http://127.0.0.1:8000/api/v1/monitoring/projects \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Monstar People","description":"Client marketing site",
       "member_ids":[3,7],"extra_emails":["client@theirdomain.com"]}'
```

**2. Register the URLs to watch under it.**

```bash
curl -X POST http://127.0.0.1:8000/api/v1/monitoring/websites \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"project_id":1,"name":"Marketing site",
       "url":"https://www.monstarpeople.com/","check_interval_seconds":300}'
```

Users 3 and 7 now receive every alert for every site in that project. Change the
membership and the recipients change with it.

**3. Optionally, give a site recipients of its own.** Any number of users
(`recipient_ids`) and plain addresses (`alert_emails`), set at create time or
later:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/monitoring/websites/1/recipients \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"recipient_ids":[11,12]}'
```

Users 11 and 12 now hear about this site only, on top of 3 and 7. To alert
**only** the site's own list, so the rest of the project stops getting mail
for it, also send `PATCH .../websites/1` with
`{"inherit_project_recipients": false}`.

| endpoint                                                 | who                        |
| -------------------------------------------------------- | -------------------------- |
| `GET /api/v1/monitoring/projects`                        | any signed-in — *scoped*   |
| `GET /api/v1/monitoring/projects/{id}`                   | any signed-in — *scoped*   |
| `GET /api/v1/monitoring/projects/{id}/report.csv`        | any signed-in — *scoped*   |
| `POST /api/v1/monitoring/projects`                       | admin, DevOps, project mgr |
| `PATCH /api/v1/monitoring/projects/{id}`                 | admin, DevOps, **owner**   |
| `DELETE /api/v1/monitoring/projects/{id}`                | admin, DevOps, **owner**   |
| `POST /api/v1/monitoring/projects/{id}/members`          | admin, DevOps, **owner**   |
| `DELETE /api/v1/monitoring/projects/{id}/members/{uid}`  | admin, DevOps, **owner**   |
| `GET /api/v1/monitoring/websites`                        | any signed-in — *scoped*   |
| `GET /api/v1/monitoring/websites/summary`                | any signed-in — *scoped*   |
| `GET /api/v1/monitoring/websites/events`                 | any signed-in — *scoped*   |
| `GET /api/v1/monitoring/websites/{id}`                   | any signed-in — *scoped*   |
| `GET /api/v1/monitoring/websites/{id}/checks`            | any signed-in — *scoped*   |
| `GET /api/v1/monitoring/websites/{id}/stats`             | any signed-in — *scoped*   |
| `POST /api/v1/monitoring/websites`                       | admin, DevOps, **owner**   |
| `PATCH /api/v1/monitoring/websites/{id}`                 | admin, DevOps, **owner**   |
| `DELETE /api/v1/monitoring/websites/{id}`                | admin, DevOps, **owner**   |
| `POST /api/v1/monitoring/websites/{id}/recipients`       | admin, DevOps, **owner**   |
| `DELETE /api/v1/monitoring/websites/{id}/recipients/{uid}` | admin, DevOps, **owner** |
| `POST /api/v1/monitoring/websites/{id}/check`            | admin, DevOps, **owner**   |

**owner** = the project manager who created the project. Admin and DevOps
override ownership everywhere; a project manager can only touch their own
projects and the sites under them. To let any project manager manage any
project, change the `PROJECT_MANAGER` branch of `can_manage_project` in
[`app/core/permissions.py`](app/core/permissions.py) to return `True`.

*scoped* = admin and DevOps see everything; **everyone else — project managers
included — sees only the projects they own or are a member of**, and the sites
under them, plus any single site they are a recipient of. Add people as members
or site recipients when creating the project or site, or later from its page.
Someone in no project and on no site sees an empty dashboard. A project or site outside what they can see
returns `404` rather than `403`, so ids cannot be probed for existence. Controlled by `can_view_all_projects` in
[`app/core/permissions.py`](app/core/permissions.py).

Deleting a project stops monitoring every site under it, and deletes their
history.

`POST .../websites/{id}/check` probes immediately through the same state
machine — the quickest way to test a new site or your SMTP credentials.

### Who receives an alert

In order, de-duplicated:

1. **The project's members** — every site in the project. Suspended users are
   skipped.
2. **`extra_emails` on the project** — addresses that are not user accounts but
   should hear about every site in the project: a client contact, a shared
   on-call inbox.
3. **The site's recipients** — users alerted about this one site. Suspended
   users are skipped. They do not have to be project members.
4. **`alert_emails` on the site** — plain addresses for this one site.
5. `ALERT_DEFAULT_EMAILS` from the environment — everything, everywhere.

A site with `inherit_project_recipients: false` skips 1 and 2, so only its own
list and the global defaults are emailed. Use it when different people own
different sites within one project.

Like a project, **every site must keep at least one alert channel**. A site
that has opted out needs at least one recipient or address of its own (or
Slack on the project). The `PATCH` or `DELETE` that would remove its last one
is refused with `422`.

`extra_emails` and `alert_emails` are replaced wholesale by `PATCH`. Project
members and site recipients have their own add/remove endpoints. Matching is
case-insensitive, so an address listed twice is only mailed once.

Alert subjects are project-qualified so someone on several projects can tell at
a glance which client is affected:

```
[DOWN] Monstar People / Marketing site is not responding
```

### When alerts fire

| transition                        | what happens                                  |
| --------------------------------- | --------------------------------------------- |
| UP → DOWN (first failed check)    | **alert immediately**                         |
| still DOWN, alerts 2…4            | alert with cumulative downtime                |
| still DOWN, past `max_down_alerts`| keep checking, **stay silent**                |
| DOWN → UP                         | **one** recovery alert with total downtime    |
| UP → UP                           | silent                                        |

With the defaults (5-minute interval, `max_down_alerts = 4`) an outage sends an
immediate alert plus three follow-ups at ~5, ~10 and ~15 minutes, then goes
quiet so a long outage does not flood inboxes. Recovery sends exactly one
"back up" mail and nothing further until the next outage.

Verified sequence across a simulated 40-minute outage:

```
down → still_down(5m) → still_down(10m) → still_down(15m) → [silence] → recovered(40m)
```

In Slack that outage is **one thread**, not five messages: the down alert is
posted to the channel, the still-down alerts reply under it, and the recovery
replies there too with "also send to channel", so the channel sees it's back up.

### Ping checks

A site with `check_type: "ping"` is a server or network device watched at the
IP level: instead of requesting a URL, each check sends `ping_count` ICMP echo
requests (default 5, half a second apart) to the host name or IP address in
`url`, and waits up to `timeout_seconds` (default 2) for the replies. Use it for
hosts with no web server, or next to a site's HTTP check, where a silent host
tells you the problem is below the application.

- **Down** when no ping is answered: the same outage alerts as a website, with
  `error_type` `no_reply`, `dns_error`, `unreachable` (a router sent back
  "destination unreachable"), `ttl_exceeded` or `network_error`.
- **Round trips** — min, average, max and jitter — are kept per check. The
  average is the check's response time, so charts, the slow alert (`slow_threshold_ms`,
  shown as "Latency alert above") and the monthly report read it.
- **Packet loss** — pings unanswered while others were — is kept per check and
  per hour, charted, and alerted on (below).

Pings use unprivileged ICMP sockets, which Linux allows only to the groups in
the `net.ipv4.ping_group_range` sysctl; `docker-compose.yml` opens it for the
API container. Elsewhere, open it for the API's group, or set
`PING_PRIVILEGED=true` and give the process `CAP_NET_RAW`. If the server cannot
ping at all, ping checks are skipped and logged rather than reported as down,
and "Check now" answers `503`.

### DNS checks

A site with `check_type: "dns"` watches one record of the domain in `url` —
`dns_record_type` `A` (the default), `AAAA`, `CNAME`, `MX` or `TXT`; e.g.
`example.com`, or `_dmarc.example.com` for a DMARC policy. Each check asks every
resolver in `DNS_RESOLVERS` at once — by default Cloudflare (1.1.1.1), Google
(8.8.8.8), Quad9 (9.9.9.9) and OpenDNS (208.67.222.222), each its own anycast
network with its own caches — and waits up to `timeout_seconds` (default 5) for
each. They are asked from the Watchly server, so this compares four independent
resolver networks, not four regions of the world.

- **Down** when the record does not resolve at most resolvers: `error_type`
  `nxdomain` (the name does not exist), `no_records` (it has no record of that
  type), `servfail` (also what validating resolvers answer for broken DNSSEC),
  `refused`, `timeout` or `network_error`. One resolver failing while the rest
  answer is recorded, not alerted.
- **Pinned values**: set `dns_expected_values` (e.g. `203.0.113.10`,
  `10 mx1.example.com`, or a TXT value, quoted or not) and any resolver
  answering anything else — other records, or that there are none — makes the
  check **down** with `dns_mismatch`. That catches a hijack or a poisoned cache
  seen by one resolver only, and tracks a change you make: update the values,
  and the check is down until every resolver serves the new records; the
  recovery says propagation is complete.
- **Nothing pinned**: the check learns the records the resolvers first agree
  on, and sends a **DNS records changed** alert when they agree on different
  ones — whether you changed them or someone with access to your DNS provider or
  registrar did. It waits for them to agree, so a change still propagating
  (resolvers serving the old records from cache, for up to their TTL) is one
  alert, not one per check. While they disagree, the site's page shows it in
  amber.
- The **response time** is the resolvers' average answer time, so charts, the
  slow alert and the monthly report work unchanged. Every check keeps each
  resolver's answer, TTL and time; the site's page shows the latest.

A domain can be pinged and have each of its record types watched side by side;
each record of a domain is watched once. The addresses of most names behind a
CDN or cloud load balancer rotate, and differ by resolver: watch their `CNAME`
instead, or switch the DNS change alert off for the project.

### What else Watchly tells you about

Beyond "down / still down / back up", each of these is its own notification
kind, on by default and switchable per project:

| kind | fires when | how often |
| ---- | ---------- | --------- |
| **SSL certificate expiring** | an HTTPS site's certificate has 14, 7, 3 or 1 days left (`SSL_EXPIRY_ALERT_DAYS`), and once more if it expires | once per threshold; renewing the certificate re-arms them |
| **Slow response** | a *successful* response is slower than the site's threshold for `SLOW_RESPONSE_CHECKS` (3) checks in a row | then quiet for `SLOW_ALERT_COOLDOWN_SECONDS` (6 h) |
| **Packet loss** | a pinged host answers, but loses at least its threshold of pings for `PACKET_LOSS_CHECKS` (3) checks in a row | then quiet for `PACKET_LOSS_ALERT_COOLDOWN_SECONDS` (6 h) |
| **DNS records changed** | a DNS check with no expected values: every resolver now returns other records than they last agreed on | once per change |
| **Monthly uptime report** | the 1st of each month, 06:00 UTC, for the month before | once per project per month |

The certificate is read every `SSL_CHECK_INTERVAL_SECONDS` (6 h) with
verification off, so the end date is known even for an already-expired or
untrusted certificate — the regular check is what reports those as **down**.
A site's own slow threshold is `slow_threshold_ms`; leave it unset to use
`SLOW_RESPONSE_THRESHOLD_MS` (3000). Set that to `0` to turn slow alerts off.
Likewise a host's `packet_loss_threshold_percent` falls back to
`PACKET_LOSS_THRESHOLD_PERCENT` (20), and `0` there turns packet-loss alerts off.

### The monthly uptime report

One message per project, to its members and extra emails (and to
`ALERT_DEFAULT_EMAILS`) and to its Slack channel: average uptime, incidents and
downtime as headline numbers, then every site worst-first with its uptime,
downtime, incidents and average response time.

The figures come from `website_checks` and are defined so they can be trusted:

- **Uptime** = successful checks ÷ all checks. It is never rounded up to 100%
  while a check failed (99.996% shows as 99.99%).
- **Downtime** runs from a site's first failed check to the next successful one,
  the same instant the recovery alert fires. An outage that spans the month
  boundary counts from the 1st; one still open at month end counts to the end.
- **Incidents** are the outages that overlapped the month.
- Sites with no checks that month (paused, or newly added) are left out and
  counted in a footnote.

Each report is claimed in `report_deliveries` *before* it is sent, so a slow mail
server or a second worker cannot send it twice — which also means a report that
nobody received is not retried. If the API was down at the 1st, it is sent when
it comes back, but only within 3 days; a project deployed on the 20th is not
mailed last month's report. `POST /monitoring/projects/{id}/report` sends one
on demand (this is what **Send last month's report now** does), and does not
stop the scheduled one. `GET /monitoring/projects/{id}/report.csv` downloads
the same figures as a file for anyone who can see the project (**Download last
month's report** on the project page); a site's history has **Download CSV** too.

> Reports read `website_checks`, which is purged after `CHECK_RETENTION_DAYS`
> (at least 35). Whatever it is set to, the purge keeps every check last
> month's report may still read until this month ends.

### Customizing notifications

Every kind can be customized at two levels, and a project's own setting wins:

1. **Global** — set by admin/DevOps under **Notifications** in the UI.
2. **Per project** — on the project's page. Anything left alone follows global,
   and global left alone follows the built-in wording.

Each field resolves on its own, so a project can rewrite one subject and inherit
everything else.

| you can set | what it does |
| ----------- | ------------ |
| **Email on/off**, **Slack on/off** | per kind. The generic webhook has no switch — it is a global firehose. |
| **Subject** | the email subject, and the Slack notification text |
| **Message** | the text at the top of the email and Slack message |

The layout, colours, facts table and the "what happens next" note are fixed
per kind; the subject and message are yours. Templates use `{{placeholders}}`
such as `{{project}}`, `{{website}}`, `{{summary}}` or `{{downtime}}` (each
kind lists its own — the UI shows them as click-to-insert chips). It is plain
substitution, not a template language, and unknown placeholders are refused on
save with the list of valid ones. In Slack the message is mrkdwn, so `*bold*`
and mentions like `<!channel>` work there.

Values from the monitored site (error text, headers) are untrusted, so each
channel escapes them for its own markup: a hostile response cannot inject HTML
into an email or `<!channel>` into Slack. Your own template text is left alone.

The **Site down** alert cannot be switched off on every channel — at the global
level or for any project — because then nobody would hear about an outage.

The editor previews the exact email and Slack message with sample data as you
type. The same is available at `POST /monitoring/notifications/preview`.

### What an alert contains

Everything a developer needs before opening a terminal — HTTP status and reason,
expected vs actual, error class (`connect_timeout`, `connect_error`,
`unexpected_status`, …), response time, timeout, redirect target, response size,
downtime so far, `down_since`, consecutive failures, and diagnostic response
headers (`server`, `retry-after`, `cf-ray`, …). Mail goes out as both plain text
and HTML.

```
[STILL DOWN] Monstar People / Marketing site has been down for 10m

Project               Monstar People
Website               Marketing site
URL                   https://www.monstarpeople.com/
Status                DOWN
HTTP status           503 Service Unavailable
Expected status       200
Error type            unexpected_status
Response time         87 ms
Down since            2026-08-30 12:10:00 UTC
Consecutive failures  3
Alert                 3 of 4
Header: retry-after   120
```

### Email setup

Any SMTP provider works — set `SMTP_HOST` and friends in `.env`:

| provider | host | port |
| -------- | ---- | ---- |
| AWS SES  | `email-smtp.<region>.amazonaws.com` | 587 (STARTTLS) |
| Resend   | `smtp.resend.com` (username `resend`, password = API key) | 587 |
| Mailgun  | `smtp.mailgun.org` | 587 |

Use `SMTP_USE_TLS=true` for port 587, or `SMTP_USE_SSL=true` for port 465.
Slack is set up per project; the generic-webhook channel switches on as soon as
`ALERT_WEBHOOK_URL` is set, and `SLACK_WEBHOOK_URL` is a fallback for projects
with no Slack of their own.

### The scheduler

A background asyncio task started in the app's lifespan wakes every
`MONITOR_TICK_SECONDS` (default 60) and probes whatever is due, so per-site
intervals are honoured without a separate worker process. Probes within a tick
run concurrently over one shared HTTP client.

Running `uvicorn --workers N` would otherwise mean N copies of every alert, so
each tick takes a Postgres advisory lock and skips if another worker holds it.
Set `MONITORING_ENABLED=false` to run an API-only instance.

Two operational notes:

- State is committed **before** alerts are sent, so a slow or broken mail server
  can never cause the same alert to be re-sent on the next tick.
- A site edited, checked by hand or deleted while its scheduled probe runs has
  that probe's result dropped, not recorded: it was taken with settings the
  site no longer has, such as a DNS check's old expected values, and would
  alert about them. The next check uses the new settings.
- `website_checks` grows one row per site per interval. Each tick sums it into
  `website_check_hourly` (kept, and what charts read) and then deletes checks
  older than `CHECK_RETENTION_DAYS` (default 90, minimum 35), a few batches per
  tick. It never deletes a check not yet rolled up, nor one last month's report
  may still need.
- Each outage, recovery, slow spell, packet-loss spell, DNS change and expiring certificate is also written to
  `website_events`, in the same commit as the state it describes, whether or
  not any channel is on for it. The app polls it for its toasts. Events are
  purged after `CHECK_RETENTION_DAYS` too.

## Migrations

```bash
alembic revision --autogenerate -m "describe change"   # needs a reachable DB
alembic upgrade head
alembic downgrade -1
```

`alembic/env.py` reads the DSN from `app.core.config.settings` and runs through
an async engine, so `alembic.ini` intentionally leaves `sqlalchemy.url` blank.

## User roles

`UserRole` is a native PostgreSQL enum named `user_role`:

| member            | stored value      |
| ----------------- | ----------------- |
| `VIEWER`          | `Viewer`          |
| `ADMIN`           | `Admin`           |
| `DEVOPS`          | `DevOps`          |
| `PROJECT_MANAGER` | `Project Manager` |
| `DEVELOPER`       | `Developer`       |

Adding or renaming a role means an `ALTER TYPE ... ADD VALUE` migration, not
just an edit to the Python enum.
