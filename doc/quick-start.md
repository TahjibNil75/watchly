# Watchly quick start

Get from nothing to your first outage alert in about ten minutes.

Watchly checks your websites, servers and DNS records on a schedule and tells
the right people when something breaks and when it is fixed. You run it
yourself with Docker.

- [How it works](#how-it-works)
- [1. Install](#1-install)
- [2. Create the admin account](#2-create-the-admin-account)
- [3. Set up email](#3-set-up-email)
- [4. Create a project](#4-create-a-project)
- [5. Add something to watch](#5-add-something-to-watch)
- [6. Invite your team](#6-invite-your-team)
- [What you need to know](#what-you-need-to-know)
- [Where to go next](#where-to-go-next)

## How it works

Everything in Watchly hangs off three ideas:

```
Project  ──  who is responsible, and where alerts go
  └── Site  ──  one thing to watch: a URL, a host to ping, or a DNS record
        └── Checks  ──  run on a timer; failures become alerts
```

1. A **project** groups sites with the people who look after them (its
   *members*) and the channels that hear about problems: email, Slack,
   Telegram, WhatsApp.
2. A **site** belongs to exactly one project. It is checked on its own
   interval, from every 30 seconds to once a day (5 minutes by default).
3. When a check fails, Watchly tries again a few seconds later, so a one-off
   blip alerts nobody. If it fails again the site is **down** and the project
   is alerted at once. While the site stays down you get a few reminders
   (4 alerts in total by default), then silence. When it answers again you get
   **one** recovery alert that says how long it was out.

Besides "down", Watchly warns you early about SSL certificates and domain
registrations about to expire, slow responses, packet loss and changed DNS
records.

## 1. Install

You need [Docker](https://docs.docker.com/get-docker/) with Compose v2.24 or
newer.

```bash
git clone https://github.com/TahjibNil75/watchly.git
cd watchly
cp .env.example .env
```

Open `.env` and change at least these:

| Setting | What to put |
| ------- | ----------- |
| `SECRET_KEY` | A random secret that signs logins. Generate one with `openssl rand -hex 32`. |
| `ALERT_DASHBOARD_URL` | The address people will open Watchly at, such as `https://watchly.example.com`. Links in emails and invitations point here. |
| `SMTP_*` | Your mail server. See [step 3](#3-set-up-email). |

Then start it:

```bash
docker compose up -d --build
```

Database migrations run by themselves on every start. When it is up:

| What | Where |
| ---- | ----- |
| The Watchly app | http://localhost:8080 |
| API documentation | http://localhost:8000/docs |

> [!NOTE]
> After editing `.env`, run `docker compose up -d` again. A plain `restart`
> does not reload it.

## 2. Create the admin account

Open http://localhost:8080 and choose **Create an account**.

> [!IMPORTANT]
> **The first person to sign up becomes the Admin.** There is no default
> account, so until you register one, anyone who can reach the app can claim
> admin. Do this straight away, before you share the address.

Once the admin exists, signup closes. Everyone else joins by
[invitation](#6-invite-your-team), or by asking for an account from the sign-in
page and waiting for an Admin or DevOps user to approve it.

## 3. Set up email

Watchly sends alerts, invitations and password resets through an SMTP server
you choose. **If `SMTP_HOST` is empty, no email is sent.** Outages are still
detected and recorded in the app, but nobody is told.

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-smtp-username
SMTP_PASSWORD=your-smtp-password
SMTP_USE_TLS=true            # port 587; use SMTP_USE_SSL=true for port 465
SMTP_FROM_EMAIL=alerts@yourdomain.com
SMTP_FROM_NAME=Watchly Alerts
```

Any provider works (AWS SES, Resend, Mailgun, Gmail with an app password, and
so on). `SMTP_FROM_EMAIL` should be on a domain your provider has verified, or
messages may be rejected. The README lists
[common provider settings](../Readme.md#set-up-email-alerts-smtp), and shows how
to catch test emails locally with Mailpit.

Slack, Telegram and WhatsApp are set up per project in the app. See
[step 4](#4-create-a-project).

## 4. Create a project

Go to **Projects → New project**.

| Field | Notes |
| ----- | ----- |
| **Name** | Must be **3 to 25 words**, for example "Acme marketing website". A one-word name is refused. |
| **Description** | Optional. If you write one, use **5 to 150 words**. |
| **Responsible members** | The users who get every alert for every site in the project. Tick yourself to start. |
| **Extra emails** | Addresses that are not user accounts, like a client or a shared on-call inbox. |
| **Slack / Telegram / WhatsApp** | Optional. A bot token and a channel, chat or phone numbers, per project. |

> [!IMPORTANT]
> **A project needs at least one place to send alerts**: a member, an extra
> email, or a Slack, Telegram or WhatsApp setup. A project with none is refused.

Which alerts a channel gets (down, recovery, warnings, monthly report) can be
switched on and off under **Notifications**.

## 5. Add something to watch

Open the project and choose **Add website** (or **Websites → Add website**).
First pick what to check. The type cannot be changed afterwards.

| Type | Watches | Down when |
| ---- | ------- | --------- |
| **HTTP(S)** | A URL | It does not answer, or answers with a status you did not expect, or the page is missing text it must contain (or has text it must not). |
| **Ping (ICMP)** | A server or IP address | No ping at all is answered. Packet loss and slow round trips are warnings. |
| **DNS records** | One record of a domain (A, AAAA, CNAME, MX or TXT) | The record stops resolving, or does not match the values you expect. |

Then fill in the form:

- **Project**, **Environment** (Development, Testing, UAT, Staging or
  Production) and **Name** are required.
- **URL / Host / Domain**: for a website, `example.com` is enough, because
  `https://` is added for you.
- **Check every**: how often to check. Shorter means faster alerts, but more
  load on the thing you watch.
- **Alerts per outage**: how many alerts one outage may send before Watchly
  goes quiet.
- **Retries before failing**: how many times to try again before calling it
  down. This is what keeps a single blip quiet.
- **Email alerts** (optional): add this one site's own people or addresses on
  top of the project's. Untick *Alert the project's members and extra emails*
  to alert only these people.

Save it, then open the site and click **Check now**. You see the status and
response time straight away, and the check appears under *Recent checks*. From
then on the scheduler takes over.

### See an alert work

Add a second site pointing at `http://127.0.0.1:9/`, where nothing listens, and
click **Check now**. It goes **down**, an outage banner appears and a down
alert is sent to the project. This proves the whole path, including email, in
under a minute.

> [!TIP]
> To watch a service running on your own machine, use
> `http://host.docker.internal:<port>/` instead of `localhost`. Inside the
> container, `localhost` means the container itself.

### Deploying? Pause the alerts

On a site's page, **Start maintenance** (for example 30 minutes) stops checks
and alerts until it ends, with no uptime lost. **End now** finishes it early.
You can also schedule a window ahead of time.

## 6. Invite your team

Only Admin and DevOps users can invite. Go to **Users → Invite user**, enter
their email and choose a role. They get an email with a link to set a password
and join. The link works for 7 days.

Most roles see only the projects they belong to (see below), so also **add each
person to the projects they look after**, on the project's page.

If someone asks for an account from the sign-in page instead, the request
appears under **Users → Account requests**, where an Admin or DevOps user can
approve or reject it.

## What you need to know

### Roles

| Role | Can do |
| ---- | ------ |
| **Admin** | Everything: all projects, all users, all settings. |
| **DevOps** | Same reach as Admin over projects and sites. Can invite people (as anything but Admin), approve account requests and manage users below Admin. |
| **Project Manager** | Create projects, and manage **their own** projects, sites and members. Cannot invite people. |
| **Developer** | View the projects and sites they belong to. Cannot change anything. |
| **Viewer** | Same as Developer: view only. The role everyone starts with unless an admin gives them another. |

### Who sees what

- **Admin and DevOps** see every project and site.
- **Everyone else** sees only projects they own or were added to as members, and
  sites they receive alerts for. Someone in no project sees an **empty
  dashboard**. If a new teammate says "I see nothing", add them to a project.
- Being a **member** both gives access and makes the person an alert recipient.

### Who gets an alert

For each alert, Watchly emails, without duplicates:

1. The project's members
2. The project's extra emails
3. The site's own recipients and addresses
4. `ALERT_DEFAULT_EMAILS` from `.env`, for everything everywhere

Suspended users are skipped. Slack, Telegram and WhatsApp work the same way
through each project's own setup, and a webhook (`ALERT_WEBHOOK_URL`) receives
every alert from every project.

In Slack, one outage is one thread: the down alert starts it, reminders reply
under it, and the recovery also shows in the channel. Telegram replies to the
down alert. WhatsApp sends each alert as its own message.

### Good to know

- **Dashboard URL matters.** If `ALERT_DASHBOARD_URL` is wrong, the links in
  invitations and alert emails point to the wrong place.
- **Deleting a project deletes its sites and their history**, and stops
  monitoring them.
- **History**: charts cover 24 hours to 90 days, and every site's history can
  be downloaded as CSV. Each project also gets a monthly uptime report.
- **Sign-in lockout**: five wrong passwords in a row lock sign-in for 15
  minutes. "Forgot password" emails a temporary password that works meanwhile.
- **Backups**: your data lives in the Postgres volume. Back it up before
  upgrading: `docker compose exec db pg_dump -U postgres watchly > backup.sql`

### Everyday commands

```bash
docker compose logs -f api        # follow the API and scheduler
docker compose up -d --build      # apply a new version or a changed .env
docker compose down               # stop everything, keep the data
docker compose down -v            # stop and DELETE the database
```

### Troubleshooting

| Problem | Likely cause |
| ------- | ------------ |
| No emails arrive | `SMTP_HOST` is empty or wrong. Check `docker compose logs api`, and remember `.env` changes need `docker compose up -d`. |
| "Project name must be 3 to 25 words" | The name is too short. Make it descriptive, such as "Acme marketing website". |
| A teammate sees an empty dashboard | They are in no project. Add them as a member. |
| Invitation link says it is dead | It expired (7 days) or was used. Send a new one from **Users**. |
| A local service is reported down | Use `host.docker.internal` instead of `localhost`. |
| Can't sign up | Signup closes once the admin exists. Ask an Admin for an invitation. |

## Where to go next

| Read | For |
| ---- | --- |
| [Readme](../Readme.md) | Features, alert channels and every setting in `.env` |
| [Local setup](local-setup.md) | Running with hot reload, and more troubleshooting |
| [Technical reference](reference.md) | Auth, roles, monitoring and alerting in depth |
| [Website monitoring](../app/monitoring/websites/README.md) | Slack, Telegram and WhatsApp setup, ping and DNS checks |
| [API reference](apis.md) | Every endpoint |
