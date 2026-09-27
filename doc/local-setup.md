# Running Watchly locally

This guide takes you from a fresh clone to a working Watchly on your machine:
the web UI, the API with its monitoring scheduler, and PostgreSQL. It then shows
how to see alert emails without a real mail provider, and how to work on the
code with hot reload.

For what the pieces are and how they fit together, see [`hld.md`](hld.md). For
every endpoint, see [`apis.md`](apis.md).

---

## Prerequisites

| you need | for | check with |
| -------- | --- | ---------- |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/), running | everything | `docker compose version` → v2.24 or newer |
| Git | getting the code | `git --version` |
| Python 3.14 | only [hot-reload development](#develop-with-hot-reload) of the API | `python3 --version` |
| Node.js 20.19+ or 22.12+ | only [hot-reload development](#develop-with-hot-reload) of the UI | `node --version` |

To just run the app, Docker is all you need.

---

## Run everything in Docker

### 1. Get the code

```bash
git clone <repo-url> watchly
cd watchly
```

### 2. Create your `.env`

```bash
cp .env.example .env
```

You can skip this file and the app will still start on built-in defaults. It's
worth making now, for two reasons:

- **`SECRET_KEY`** signs login tokens. Replace the placeholder with the output
  of `openssl rand -hex 32`.
- **`SMTP_*`** controls whether alert emails go out. Leave it blank for now.
  [See alert emails locally](#see-alert-emails-locally) sets it up.

Leave `POSTGRES_HOST` as `localhost`. Inside Docker, compose points the API at
the database container regardless of what the file says.

### 3. Start the stack

```bash
docker compose up -d --build
```

The first run downloads base images and builds two images, which takes a few
minutes. Later starts take seconds.

On every start the API container applies database migrations. You never run
them by hand in Docker.

### 4. Check it's up

```bash
docker compose ps
```

All three should be running, and `watchly-postgres` and `watchly-api` should
say `(healthy)`:

```
NAME               STATUS
watchly-api        Up 20 seconds (healthy)
watchly-postgres   Up 26 seconds (healthy)
watchly-web        Up 14 seconds
```

```bash
curl http://localhost:8080/health
# {"status":"ok","version":"1.0.0","monitoring":"on"}
```

### 5. Create the admin account

Open **http://localhost:8080** and click **Create an account**. There is no
built-in admin: on an empty database, the first account to sign up becomes
the admin, and every later one is a viewer.

Do this straight away. Until that first account exists, anyone who can reach
the app can sign up and take the admin role.

After that, anyone who can reach the app can still sign up as a viewer. For an
invite-only team, set `ALLOW_PUBLIC_SIGNUP=false` in `.env` and restart the API
(`docker compose up -d api`): signup then stops once the admin exists, and
people join through **Users → Invite user** instead.

| what | where |
| ---- | ----- |
| Web UI | http://localhost:8080 |
| Interactive API docs | http://localhost:8000/docs |
| PostgreSQL | `localhost:5432`, user `postgres`, password `postgres`, database `watchly` |

---

## Try it out

Here's a quick tour that exercises the whole monitoring loop.

1. **Create a project.** Go to **Projects → New project**, give it a name, and
   tick yourself under *Responsible members*. Every project needs someone or
   something to alert, so the form refuses one with no members, no extra
   emails, no Slack, no Telegram and no WhatsApp.
2. **Add a website.** On the project page, go to **Add website**, then enter a
   name and a URL such as `example.com` (`https://` is added for you).
3. **Probe it now.** On the website page, click **Check now**. You'll see the
   HTTP status and response time, and the check appears in *Recent checks*.
   After that, the scheduler probes it on its interval (every 5 minutes by
   default).
4. **Watch an outage.** Add a second website pointing at a port nothing
   listens on, such as `http://127.0.0.1:9/`, and click **Check now**. It goes
   **down**, the outage banner appears, and a `down` alert is recorded. That
   alert is emailed only once SMTP is set up.
5. **Add a teammate.** In a private window, sign up a second account at
   http://localhost:8080/signup (or, with `ALLOW_PUBLIC_SIGNUP=false`, invite
   them from **Users → Invite user**). Every account after the first is a
   *viewer* unless invited with another role, and sees nothing yet. Back as
   admin, change their role on the **Users** page, or add them as a project
   member so they see that project's sites.

To monitor something running **on your own machine**, use
`http://host.docker.internal:<port>/` rather than `localhost`. Inside the API
container, `localhost` means the container itself.

---

## See alert emails locally

[Mailpit](https://mailpit.axllent.org/) is a local mail catcher. It accepts
any email and shows it in a web inbox, so you can see real alerts without a
mail provider.

**1. Start Mailpit:**

```bash
docker run -d --name watchly-mailpit -p 8025:8025 -p 1025:1025 axllent/mailpit
```

**2. Point Watchly at it** by setting these in `.env`:

```dotenv
SMTP_HOST=host.docker.internal
SMTP_PORT=1025
SMTP_USE_TLS=false
```

**3. Apply the change:**

```bash
docker compose up -d
```

Use `up -d` rather than `restart`. Compose only picks up `.env` changes when it
recreates a container, and `restart` doesn't.

**4. Trigger an alert.** Click **Check now** on a site that's down, then open
**http://localhost:8025**. You'll see a message like
`[DOWN] My project / Broken site is not responding`, sent to the project's
members plus everyone in `ALERT_DEFAULT_EMAILS`.

When you're done, remove Mailpit with `docker rm -f watchly-mailpit` and blank
out `SMTP_HOST` again.

### Try the other notifications

With Mailpit running, everything Watchly can send is one click away:

- **See and edit the wording without waiting for an outage.** Sign in as admin,
  open **Notifications**, expand any kind and edit its subject or message. The
  preview underneath renders the real email, Slack, Telegram and WhatsApp
  message as you type.
- **Get a monthly uptime report now.** Once a project has a month of checks,
  open it and click **Send last month's report now**. Left alone, it goes out on
  the 1st at 06:00 UTC (`MONTHLY_REPORT_DAY`, `MONTHLY_REPORT_HOUR_UTC`).
- **Trigger a slow-response alert.** Set a low **Slow after (ms)** on a site
  (under *Request options*), then click **Check now** a few times
  (`SLOW_RESPONSE_CHECKS` slow checks in a row, three by default).
- **Invite someone.** Sign in as admin, open **Users → Invite user**, pick an
  address and a role, and the invitation lands in Mailpit. Open the link in a
  private window to accept it. The link is built from `ALERT_DASHBOARD_URL`,
  which Compose already points at the web UI.
- **SSL expiry** warns at 14, 7, 3 and 1 days. A site's certificate date is
  shown on its page once it has been read.

Slack and Telegram work the same way, per project: give a project a bot token
and channel (or chat) and the same messages appear there. A single site can also
get its own from the **Slack alerts (optional)** or **Telegram alerts
(optional)** section of its form. WhatsApp is set up on the project too — an
access token, the sending number's Phone number ID and the numbers to alert —
and a site can alert numbers of its own; it needs an approved message template
first (see [the websites README](../app/monitoring/websites/README.md#whatsapp)).
Switch any kind off for a project, on any channel, from the project's
**Notifications** section.

---

## Develop with hot reload

The Docker images hold a copy of the code, so edits only show up after a
rebuild. For day-to-day work, run the part you're changing outside Docker and
leave the rest in it.

### Working on the UI only

Run the database and API in Docker and the UI on the Vite dev server:

```bash
docker compose up -d db api
docker compose stop web        # only if it was already running

cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**. The page reloads as you save. The dev server
forwards `/api` to the API on port 8000.

### Working on the API

Run only PostgreSQL in Docker and the API on your machine:

```bash
docker compose up -d db
docker compose stop api web    # frees port 8000 if they were running

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

alembic upgrade head           # create or update the tables

uvicorn app.main:app --reload
```

The API now runs at http://localhost:8000 and restarts when you save a Python
file. Run the UI with `npm run dev` as above, and use http://localhost:5173.

Outside Docker, the API reads `.env` directly. That means:

- `POSTGRES_HOST=localhost` is correct, as in `.env.example`.
- For Mailpit, use `SMTP_HOST=localhost` instead of `host.docker.internal`.
- Monitored URLs on your machine can use `localhost` normally.

When you change a model, create a migration with
`alembic revision --autogenerate -m "describe change"`, review it, then run
`alembic upgrade head`.

---

## Everyday commands

| to | run |
| -- | --- |
| Start everything | `docker compose up -d` |
| Rebuild after changing code | `docker compose up -d --build` (or `--build api` / `--build web`) |
| Apply `.env` changes | `docker compose up -d` |
| Follow API and scheduler logs | `docker compose logs -f api` |
| Stop everything, keep data | `docker compose down` |
| Stop everything, **delete the database** | `docker compose down -v` |
| Open a SQL shell | `docker compose exec db psql -U postgres -d watchly` |

**After pulling new code**, run `docker compose up -d --build`. Migrations
apply on start. If you develop on the host instead, run
`pip install -r requirements.txt`, `alembic upgrade head` and
`cd frontend && npm install`.

**To start over from an empty database**, run `docker compose down -v`, then
`docker compose up -d`. Then sign up again; the first account becomes the admin.

---

## Troubleshooting

**`Bind for 0.0.0.0:8080 failed: port is already allocated`** (or 8000 or 5432)

Something else is using that port. Pick free ones in `.env`, then run
`docker compose up -d`:

```dotenv
WEB_PORT=8081
API_PORT=8001
POSTGRES_PORT=5433
```

If you change `POSTGRES_PORT` and develop the API on the host, the host API
follows it automatically, because it reads the same variable.

**I changed `POSTGRES_PASSWORD` and nothing happened**

It only applies when the database volume is first created; PostgreSQL keeps
its original credentials. Reset with `docker compose down -v` (this deletes
all data).

**I signed up, but my account is a viewer, not the admin**

Only the first account on an empty database becomes the admin, so another
account already existed. A database from an older version still has the
seeded `admin` account (password `Admin@123` unless you changed it): sign in
as that admin and change your role on the **Users** page. To start over
instead, run `docker compose down -v` (this deletes all data) and sign up
again.

**"Too many failed sign-ins" when signing in as the admin**

`MAX_FAILED_LOGIN_ATTEMPTS` (default 5) wrong passwords in a row lock sign-in
for `LOGIN_LOCKOUT_MINUTES` (default 15). Wait for it to run out, sign in with a
temporary password from **Forgot password**, or lift it from the server. The
same command reactivates a suspended admin when no other admin can:

```bash
python -m app.db.reactivate <username>                       # API on the host
docker compose exec api python -m app.db.reactivate <username>   # API in Docker
```

**The UI shows `502 Bad Gateway`, or "API unreachable" in the sidebar**

The API container isn't running. Check `docker compose ps`, then
`docker compose logs api` for the reason. A common cause is database
credentials in `.env` that don't match an existing volume (see above).

**"Cannot reach the Watchly API. Is the backend running?"**

This is the dev server on port 5173 failing to reach port 8000. Start the API
with `docker compose up -d api` or `uvicorn app.main:app --reload`.

**I keep getting signed out**

The app renews its 30-minute access token with the refresh cookie, so a
session lasts until it goes unused for `REFRESH_TOKEN_EXPIRE_DAYS` (7 by
default). If you are signed out sooner:

- The cookie is `Secure`. Browsers accept that over plain HTTP only on
  `localhost`, so opening the app at a LAN address or another hostname loses
  it. Use `localhost`, or set `REFRESH_COOKIE_SECURE=false` for that setup.
- Sending the same refresh token twice ends the session on purpose, since it
  looks like theft. Scripts that call `/auth/refresh` must not run in parallel.

Changing `SECRET_KEY` invalidates access tokens but not sessions. It also makes
stored Slack, Telegram and WhatsApp tokens unreadable unless
`SLACK_TOKEN_ENCRYPTION_KEY` is set separately.

**Sites go down and nobody gets an email**

`SMTP_HOST` is blank. The API says so at startup:

```bash
docker compose logs api | grep -i smtp
# Monitoring is on but SMTP_HOST is unset — outages will be detected and recorded, but no email alerts will go out.
```

Set up [Mailpit](#see-alert-emails-locally) or a real provider (see *Email
setup* in the [technical reference](reference.md#email-setup)).

**A site on my machine always shows as down**

It's probably using `localhost`, which inside Docker is the API container
itself. Use `http://host.docker.internal:<port>/` instead.

**My code changes don't show up**

The Docker images contain a copy of the code. Rebuild with
`docker compose up -d --build`, or use [hot reload](#develop-with-hot-reload).
