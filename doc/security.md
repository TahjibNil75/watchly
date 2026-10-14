# Security

What Watchly does to protect its users, its data and the servers it watches, and
where each protection lives in the code. For how to report a vulnerability and
how to run a deployment safely, see [SECURITY.md](../SECURITY.md).

This page describes what is implemented. [Known gaps](#known-gaps) at the end
lists what is not.

- [Signing in](#signing-in)
- [Sessions and tokens](#sessions-and-tokens)
- [Passwords](#passwords)
- [Roles and permissions](#roles-and-permissions)
- [Rate limits and request size](#rate-limits-and-request-size)
- [Stored secrets](#stored-secrets)
- [What a check may connect to](#what-a-check-may-connect-to)
- [Docker agent tokens](#docker-agent-tokens)
- [Network and deployment](#network-and-deployment)
- [Data handling and logs](#data-handling-and-logs)
- [Tests and supply chain](#tests-and-supply-chain)
- [Known gaps](#known-gaps)

---

## Signing in

| Protection | How it works | Code |
| --- | --- | --- |
| Bcrypt password hashes | Passwords are hashed with bcrypt and a per-password salt. Input is cut to bcrypt's 72-byte limit before hashing and before checking, so long passwords don't raise. | [app/core/security.py](../app/core/security.py) |
| Flat login timing | When no account matches, login still runs bcrypt against a dummy hash, so response time doesn't reveal which usernames or emails exist. | [app/auth/service.py](../app/auth/service.py) `authenticate` |
| Account lockout | `MAX_FAILED_LOGIN_ATTEMPTS` (5) wrong passwords in a row lock sign-in for `LOGIN_LOCKOUT_MINUTES` (15). The count is updated in one SQL statement, so a burst of parallel guesses can't all read the same count. The lock stops sign-in only: it never suspends the account or signs its owner out. | `AuthService.record_failed_login` |
| No account oracle | A locked account answers `401` with the same text as a wrong password or an unknown identifier. A locked account is not checked against the password and not counted further. A suspended account is only reported after the password is proven right. | `InvalidCredentialsError` |
| Lockout covers password prompts | Changing the password and requesting an email change also ask for the current password. A wrong one counts like a failed sign-in, and a locked account is refused with `429` before the password is looked at. A stolen access token can't be used to guess the real password. | [app/user/service.py](../app/user/service.py) `_check_current_password` |
| First account is the admin, once | On an empty database the first signup becomes Admin, then signup closes and people join by invitation or an approved account request. Racing first signups are serialised with a Postgres advisory lock, so only one wins. A closed signup answers before it checks for taken emails, so it doesn't reveal which exist. | `AuthService.signup` |
| Suspended accounts | A suspension takes hold on the next request, not when the access token expires. | [app/auth/dependencies.py](../app/auth/dependencies.py) |
| New-country email | With `COUNTRY_HEADER` set, a sign-in from a country the account hasn't used before sends a warning email. `COUNTRY_ALLOW` / `COUNTRY_DENY` turn other countries away with `403`. | [app/core/geo.py](../app/core/geo.py), [app/auth/countries.py](../app/auth/countries.py) |

### Forgot password

- Asking emails a **temporary password** (12 characters from an alphabet without
  look-alikes, about 69 bits). Only its bcrypt hash is stored, and it expires after
  `TEMP_PASSWORD_EXPIRE_MINUTES` (60).
- The **current password keeps working** until the temporary one is used, so a
  stranger who types your address can't lock you out.
- While a temporary password is live, asking again **does not replace it**. The
  owner gets a reminder email that one is waiting, so someone who knows the
  address can't keep cancelling the password you were just sent.
- Using the temporary password makes it the account's password, sets
  `must_change_password`, and ends every other session. Until a new password is
  chosen, the session can only change the password.
- The answer is the same whether or not the address has an account. Bcrypt runs
  either way.
- A temporary password still works while sign-in is locked, because it went only to
  the owner's mailbox.

---

## Sessions and tokens

- **Access tokens** are short-lived JWTs (`ACCESS_TOKEN_EXPIRE_MINUTES`, 30) signed
  with `SECRET_KEY`. They carry `sub`, `type`, `iat`, `nbf`, `exp`, a random `jti`
  and a session version `sv`. Decoding pins the algorithm and requires `exp`,
  `sub` and `type`. See [app/utils/jwt.py](../app/utils/jwt.py).
- **Roles are read from the database on every request**, never from the token. A
  demotion applies on the next request.
- **`session_version` revokes access tokens at once.** Changing the password,
  signing in with a temporary password and ending all sessions bump it, and a token
  with an old `sv` is refused with `401`.
- **Refresh tokens are opaque, not JWTs.** They are stored hashed (SHA-256), are
  single use, and are rotated on every refresh. See
  [app/db/models/refresh_token.py](../app/db/models/refresh_token.py).
  - The redeem query uses `FOR UPDATE`, so two requests with the same token queue
    and the second sees it as used.
  - **Reuse detection:** presenting a token that was already used revokes its whole
    family, which signs out both the thief and the owner.
  - Expired tokens are deleted on every scheduler tick and when a session starts.
- **The refresh cookie** is `httpOnly`, `SameSite=Strict`, `Secure` by default
  (`REFRESH_COOKIE_SECURE`) and scoped to `/api/v1/auth`. Page scripts can't read it,
  and it is never sent on a request started by another site, which is what protects
  `/refresh` and `/logout` from cross-site request forgery. See
  [app/auth/cookies.py](../app/auth/cookies.py).
- **Emailed links** (invitations, email change, password reset) use 256-bit random
  tokens (`secrets.token_urlsafe(32)`), store only their SHA-256, expire, and are
  carried in the URL **fragment**, so they don't reach server logs or `Referer`
  headers. Confirming an email change locks the row so two clicks can't race.

---

## Passwords

Every password someone *chooses* is checked by
[app/core/password_policy.py](../app/core/password_policy.py) (the frontend shows the
same rules as a checklist in `frontend/src/password.js`):

- at least 8 characters, not counting spaces;
- an uppercase letter, a lowercase letter, a number and a special character;
- must not contain the username, the email's local part, or the full name (whole or
  word by word, for parts of 3+ characters);
- a new password can't equal the current one.

Signing in and temporary passwords are not checked against these rules.

---

## Roles and permissions

Five roles: Admin, DevOps, Project Manager, Developer and Viewer. The rules are pure functions in one module,
[app/core/permissions.py](../app/core/permissions.py), and enforced by FastAPI
dependencies in [app/auth/dependencies.py](../app/auth/dependencies.py)
(`require_roles`, `require_role_manager`, `require_user_manager`, `require_inviter`).

| Rule | Detail |
| --- | --- |
| Who may hand out which role | One table, `INVITABLE_BY`, used for invitations **and** for changing a role. Admin may grant any role. DevOps may grant any role **except Admin**. Nobody else grants roles. |
| Who may change whose role | `can_change_role` (the target) **and** `can_assign_role` (the new role) must both pass. Admin may re-role anyone. DevOps may not touch an Admin or another DevOps user. Nobody may change their own role. |
| Who may suspend whom | The `SUSPENDABLE_BY` table. Admin may suspend anyone; DevOps and Project Managers only the roles listed for them. Nobody may suspend themselves. |
| Who may manage a project | Admin and DevOps manage all. A Project Manager manages the projects they created. Anyone who can manage a project can also see it. |
| Who sees what | Admin and DevOps see every project. Everyone else sees only projects they own or belong to. |
| Withdrawing an invitation | Uses the same test as sending it, so no one can undo an invitation they couldn't have sent. |
| Temporary-password sessions | `get_current_user` refuses everything except the password change while `must_change_password` is set. |

The frontend mirrors these rules in `frontend/src/roles.js` to hide actions, but the
API is what enforces them.

---

## Rate limits and request size

Two layers stop one client sending too many requests, and a third stops a request
from being too large.

### Per-client limits

[app/core/rate_limit.py](../app/core/rate_limit.py) counts requests in Postgres, so
every worker shares the counts. Each request bumps its counter in one upsert
*before* it is handled, so parallel requests can't slip past the limit together. It
is committed in its own session, so a request that later fails still counts. Past
the limit the API answers `429` with `Retry-After`, and logs one warning per window.

A client is an IPv4 address or an **IPv6 /64** (one host usually holds a whole /64,
so counting single addresses would be no limit).

| Setting | Default | Covers |
| --- | --- | --- |
| `RATE_LIMIT_LOGIN` | `10/minute` | Sign-in attempts, right or wrong, to any account |
| `RATE_LIMIT_SIGNUP` | `5/hour` | Signup |
| `RATE_LIMIT_FORGOT_PASSWORD` | `5/hour` | Forgot password, whichever addresses |
| `RATE_LIMIT_EMAIL_LINKS` | `20/minute` | Endpoints reached from an emailed link |
| `RATE_LIMIT_ACCOUNT_REQUESTS` | `5/hour` | Asking for an account from the sign-in page |
| `RATE_LIMIT_DOCKER_AUTH_FAILURES` | `30/minute` | Docker pushes with an unknown token, per client address |

### Per-user limits

Actions that cost something beyond the request itself are counted per signed-in
user instead:

| Setting | Default | Covers |
| --- | --- | --- |
| `RATE_LIMIT_CHECK_NOW` | `10/minute` | "Check now" on a website or an infrastructure resource (sends traffic to the monitored host) |
| `RATE_LIMIT_SEND_REPORT` | `5/hour` | "Send report" (mails every recipient of the project) |
| `RATE_LIMIT_INFRA_DIAGNOSE` | `30/minute` | Infrastructure diagnose |
| `RATE_LIMIT_DOCKER_INGEST` | `60/minute` | Docker pushes, **per host** |

`RATE_LIMIT_ENABLED=false` turns all of this off, for tests only.

### Trusting the client address

Limits are only as good as the address they key on. uvicorn believes
`X-Forwarded-For` only from `FORWARDED_ALLOW_IPS`. Compose sets that to **one
address**, the web container's pinned IP (`WEB_IP`), never a subnet, because a
subnet includes the bridge gateway, which is how a client of a published port
appears on Docker Desktop. Without this a client could send a fresh
`X-Forwarded-For` on every request and get a fresh limit each time. See
[docker-compose.yml](../docker-compose.yml) and
[docker/api-entrypoint.sh](../docker/api-entrypoint.sh).

### Request size

A JSON body is read in full before validation, so without a cap an unauthenticated
`POST /auth/login` could be fed gigabytes.

- **Caddy** answers `413` above `request_body max_size 1MiB` on `/api/*`
  ([frontend/Caddyfile](../frontend/Caddyfile)).
- **The API** enforces the same 1 MiB with `BodyLimitMiddleware`
  ([app/core/body_limit.py](../app/core/body_limit.py)), which covers direct access
  to the API port. It refuses a declared `Content-Length` before reading anything and
  cuts off a chunked body as it passes the limit.
- The two values must stay equal.

### Response size in checks

A monitored site can return an endless body or a compressed bomb. HTTP checks
**stream** the response and read at most `MAX_SEARCHED_BYTES` (1 MiB) of the
*decoded* body, then close the connection. gzip and deflate are inflated with a
capped `zlib` decompressor, because a few KB of network data can expand to hundreds
of MB before httpx counts anything. See `read_capped` in
[app/monitoring/websites/checker.py](../app/monitoring/websites/checker.py).

---

## Stored secrets

Slack, Telegram and WhatsApp tokens, SMTP passwords, custom request headers and AWS
secret keys are supplied through the API and have to be kept, so they are
**encrypted at rest** with Fernet (AES-128-CBC with HMAC-SHA256) in
[app/core/crypto.py](../app/core/crypto.py).

- The key is derived from `SLACK_TOKEN_ENCRYPTION_KEY` if set, otherwise from
  `SECRET_KEY`.
- The API **never returns** a stored secret. It returns a masked hint
  (`xoxb-…efgh`).
- `decrypt_secret` returns `None` instead of raising when a value can't be read, so
  one unreadable token doesn't stop the monitoring loop.
- Rotating the key makes existing ciphertexts unreadable; the integration stops and
  the secret has to be entered again.
- A `422` validation error redacts `password`, `confirm_password`,
  `current_password`, `new_password` and `token` instead of echoing them back, and
  the rejection is logged without the offending input
  ([app/core/handlers.py](../app/core/handlers.py)).
- Telegram bot tokens are scrubbed from log records and error text.

---

## What a check may connect to

Watchly makes HTTP, ping, TCP, DNS, mail and TLS requests to addresses that people
give it, from a server that may sit inside a VPC with an instance role. Without a
policy, a check of `http://169.254.169.254/` would read the instance's AWS
credentials, and one of `10.20.0.25:5432` would probe Watchly's own database. This
is server-side request forgery, and
[app/monitoring/egress.py](../app/monitoring/egress.py) is the defence.

- **Resolve, judge, then connect to the judged address.** Every probe resolves its
  target, each resolved address is checked, and only addresses that pass are
  connected to. Because it connects to the address it judged instead of resolving
  again, **DNS rebinding can't swap the address afterwards**. This applies on every
  hop of a redirect chain.
- **Always refused**, whatever the check: `0.0.0.0/8`, loopback, link-local
  (`169.254.0.0/16`, which holds the instance metadata service and Amazon DNS),
  multicast, reserved space, `fe80::/10`, `fd00:ec2::/32` (metadata over IPv6), the
  server's **own addresses** (read from its interfaces and EC2 metadata at startup),
  and anything in `EGRESS_DENY_CIDRS`. IPv4-mapped IPv6 addresses (`::ffff:a.b.c.d`)
  are unwrapped first.
- **Website checks** reach public addresses only (`WEBSITE_PRIVATE_TARGETS=block`,
  the default). Private resources are watched from the Infrastructure page instead.
- **Infrastructure probes** may reach only the CIDRs of the resource's own VPC, plus
  public addresses when the check goes over the internet.
- A refused check says why, and `GET /monitoring/websites/blocked` lists the blocked
  ranges.
- Mail checks verify the server's TLS certificate, and hang up after a failed login
  without sending anything.
- At startup the API warns loudly if `WEBSITE_PRIVATE_TARGETS=allow` is set.

---

## Docker agent tokens

The [Docker agent](docker-agent.md) pushes to `POST /api/v1/docker/ingest`.

- Each host has its own token (`wdk_` plus 256 random bits). Watchly stores **only
  its SHA-256 hash** and shows the token once, when it is created. *New token*
  revokes the old one immediately.
- A token can only push for its own host, and can read nothing from Watchly.
- The agent only makes outbound requests; nothing on the host is opened to Watchly.
- Authentication happens **before** any counting. An unknown token is counted
  against the sender's address (`RATE_LIMIT_DOCKER_AUTH_FAILURES`), so a flood of
  made-up tokens costs one rate-limit row, not one per token. A valid token is
  counted per host (`RATE_LIMIT_DOCKER_INGEST`), so many hosts behind one NAT don't
  share a limit.
- The ingest body is size-capped like every other request.

---

## Network and deployment

[docker-compose.yml](../docker-compose.yml) and the Caddyfile set safe defaults:

- **PostgreSQL** is published on `127.0.0.1` only, and Compose refuses to start
  without `POSTGRES_PASSWORD`. Docker's port publishing bypasses `ufw` and
  `firewalld`, so a `0.0.0.0` binding would have been reachable from the internet.
- **The API port** (`8000`) is published on `127.0.0.1` only. Caddy reaches it over
  the Compose network, so going around Caddy would skip its header handling.
- **The forged-country header** is stripped. `COUNTRY_HEADER` is trusted by the API
  for `COUNTRY_ALLOW` / `COUNTRY_DENY` and the new-country email, so Caddy deletes
  it from every request unless the connection comes from an address in
  `COUNTRY_TRUSTED_PROXIES` (default: none).
- **TLS** is left to whatever sits in front of the web container (Caddy runs with
  `auto_https off`, plain HTTP on `:80`). Put a TLS proxy or load balancer in front.
- Caddy sends `Referrer-Policy: no-referrer`, so page URLs aren't leaked to other
  sites. Hashed assets are cached forever; the app shell is `no-cache`.
- The API container runs as an unprivileged user (`watchly`, uid 1000) and has a
  health check; Compose starts the web container only once the API is healthy.
- `.env` holds the secrets and is git-ignored; `.env.example` is the template.
- At startup the API **warns** if `SECRET_KEY` is still the default or SMTP isn't
  configured.

---

## Data handling and logs

- **Bound parameters throughout.** Queries go through SQLAlchemy. The one hand-written
  statement, the advisory lock for the first signup, passes its key as a parameter.
- **CSV exports** prefix any cell that starts with `=`, `+`, `-` or `@` with `'`, so a
  site name can't run as a spreadsheet formula
  ([app/monitoring/exports.py](../app/monitoring/exports.py)).
- **Output is escaped**: HTML email and Telegram messages escape user text, and React
  escapes it in the dashboard.
- **Logs** are split per module under `LOG_DIR`, and each line carries a request id
  that is also returned as `X-Request-ID`. Passwords and tokens are not logged;
  failed sign-ins log the user id, not the password. Lines older than
  `LOG_RETENTION_HOURS` (6) are pruned. See
  [app/core/logging_config.py](../app/core/logging_config.py).
- **Cleanup** runs on each scheduler tick: expired refresh tokens, closed rate-limit
  windows, expired invitations and old check, event and Docker history are purged, so
  tables don't grow without bound.

---

## Tests and supply chain

- **Tests** ([tests/](../tests/)) run in CI (`pytest`) and cover the current-password
  lockout, temporary passwords, the body-size limit, per-user rate limits, the
  response-body cap (including compression bombs) and Docker ingest rate limiting.
- **CI** compiles the app, imports it, runs the tests and applies every migration to
  a fresh database ([.github/workflows/ci.yml](../.github/workflows/ci.yml)).
- **Dependabot** opens monthly update PRs for pip, npm, Docker base images and GitHub
  Actions ([.github/dependabot.yml](../.github/dependabot.yml)). Turning on security
  updates under *Settings → Code security* adds same-day PRs for new advisories.
- `.github/CODEOWNERS` routes changes for review.

---

## Known gaps

These are not implemented. They are listed so nobody assumes otherwise. Items are
from a read-through of the code on 2026-10-14; none were tested against a running
instance.

**Fix before a real deployment**

- **The default `SECRET_KEY` only logs a warning.** The app starts with the public
  key `dev-secret-change-me`, which lets anyone forge a session token and decrypt
  stored secrets from a database dump. It should refuse to start outside `DEBUG`.
- **The first visitor to a fresh install becomes Admin**, and `GET /auth/signup`
  says whether that is still possible. Sign up straight after the first start; a
  one-time setup token would remove the race.
- **No browser security headers.** Caddy sets only `Referrer-Policy`: no
  `Content-Security-Policy`, `frame-ancestors` / `X-Frame-Options`,
  `X-Content-Type-Options` or `Permissions-Policy`, so the dashboard can be framed.
  Set `Strict-Transport-Security` at the TLS proxy.
- **The access token is kept in `localStorage`**, where any script on the page can
  read it. The httpOnly refresh cookie already restores a session after a reload, so
  the access token could live in memory only.

**Smaller gaps**

- **Stored secrets follow a site to a new URL.** Request-header values and SMTP
  passwords are kept when a site's `url` changes, so someone who may edit a site but
  not read its secrets can point it at their own server and run "Check now".
- **Project Managers can use Watchly's own AWS credentials.** `auth_type: "default"`
  (the instance role) isn't limited to Admin and DevOps. Give that role read-only
  permissions only, and use `external_id` on cross-account roles.
- **Changing the email address** doesn't notify the old address or end other sessions.
- **The egress blocklist** unwraps IPv4-mapped IPv6 addresses only. NAT64
  (`64:ff9b::/96`), 6to4 (`2002::/16`) and IPv4-compatible addresses are judged as
  public IPv6, and `192.0.0.0/24`, `198.18.0.0/15` and `fec0::/10` aren't listed.
  Keep `WEBSITE_PRIVATE_TARGETS=block`, and add to `EGRESS_DENY_CIDRS` on hosts with
  NAT64.
- **Webhook URLs reach the logs.** A failed `ALERT_WEBHOOK_URL` or Slack webhook post
  logs the exception, and httpx includes the URL, which is the secret. Treat
  `./logs` as sensitive.
- **One key signs sessions and encrypts secrets** (unless `SLACK_TOKEN_ENCRYPTION_KEY`
  is set), and there is no key rotation.
- **`/health` is public** and reports the version and which features are on. FastAPI's
  `/docs`, `/redoc` and `/openapi.json` are served on the API port, which is bound to
  loopback by default.
- **Dependencies aren't pinned.** Python requirements use `>=` with no lock file or
  hashes, base images aren't pinned by digest, and Actions are pinned by tag, not
  commit SHA.
- **There is no MFA.** Admin and DevOps accounts control AWS credentials and alert
  channels.

**Design choices with a cost**

- Project Managers can suspend DevOps users, so one compromised Project Manager
  account can lock out the ops team.
- `WEBSITE_PRIVATE_TARGETS=allow` turns every website, ping and database check into a
  scanner of the internal network. Keep it `block` on any shared or cloud install.
