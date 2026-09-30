# Security policy

## Supported versions

Security fixes go into the latest release. Please upgrade before reporting.

| Version | Supported |
| ------- | --------- |
| 1.0.x   | Yes       |

## Reporting a vulnerability

**Please don't open a public issue.** Report it privately instead:

- **On GitHub:** the repository's **Security** tab → **Report a vulnerability**.
- **Or by email:** to the maintainer, [@TahjibNil75](https://github.com/TahjibNil75),
  at the address on their GitHub profile.

It helps to include the Watchly version (`/health` reports it), what an
attacker could do, and the steps to reproduce it.

You'll get an answer within a week. Once the problem is confirmed, a fix is
released as soon as it is ready, and the release notes credit you unless you'd
rather not be named.

## Running Watchly safely

Most problems in a self-hosted deployment come from its settings:

- **Set a real `SECRET_KEY`** (`openssl rand -hex 32`). It signs every session
  and, unless `SLACK_TOKEN_ENCRYPTION_KEY` is set, encrypts the stored Slack,
  Telegram and WhatsApp tokens. The API logs a warning while it is the default.
- **Sign up as the admin straight after the first start**, before the app is
  reachable by others: the first account on an empty database becomes the
  admin. After that signup is closed and people join only by invitation.
- **Serve it over HTTPS** behind a reverse proxy, and keep PostgreSQL off the
  public internet: the Compose file publishes it on `POSTGRES_PORT` for local
  development.
- **Tell the API which proxy to trust.** Sign-in, signup, forgot password and
  the emailed-link endpoints are rate-limited per client address. Behind a
  proxy that is not the Compose file's own nginx, set `FORWARDED_ALLOW_IPS` to
  its address; otherwise every visitor shares the proxy's limit, and one
  attacker can use it up for everyone.
- **Keep `.env` out of version control.** It holds your SMTP password and API
  tokens; `.env.example` is the template to commit.
