# Website monitoring

How a URL gets watched (or a host pinged, a domain's DNS records looked up, or
a database knocked on),
how an outage turns into email, and every API in the project as it stands today.

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
| `websites`           | one URL (or host to ping, or domain to look up), how to check it, and its live outage state |
| `website_recipients` | which users are alerted about one particular site          |
| `website_checks`     | the result of every poll — the evidence behind each alert, including why it failed and where its time went; purged after `CHECK_RETENTION_DAYS` |
| `website_check_hourly` | each site's checks summed per UTC hour — what charts and long-range uptime read; kept |
| `maintenance_windows` | stretches of time when a site is expected to fail, e.g. a deployment: no checks or alerts during them; purged `CHECK_RETENTION_DAYS` after they end |

Deleting a project deletes its websites, which deletes their check history.

---

## 2. Files in this package

| file          | role                                                             |
| ------------- | ---------------------------------------------------------------- |
| `models.py`   | `Website` (config + live state), `WebsiteCheck` (poll history) and `MaintenanceWindow` |
| `schemas.py`  | request/response contracts and their validation rules            |
| `checker.py`  | the HTTP probe (and, every few hours, the certificate read; once a day, the domain lookup) — turns a URL into a `CheckResult`; hands ping checks to `pinger.py` and DNS checks to `dns_probe.py` |
| `domain_lookup.py` | when a host's domain registration expires, with which registrar, and its nameservers — over RDAP, or WHOIS for registries without it |
| `security_headers.py` | which security headers a response sends, and how well each is set |
| `pinger.py`   | the ICMP probe — pings a host a few times and reports replies, round trips and packet loss |
| `dns_probe.py` | the DNS probe — asks several resolvers for one record and judges whether they agree with each other and with the pinned values |
| `db_probe.py` | the database probe — opens a session with a PostgreSQL, MySQL, Redis or MongoDB server as far as its first answer, without logging in |
| `service.py`  | CRUD, filtering, maintenance windows, and "which sites are due for a check" |
| `history.py`  | hourly rollups, the stats read from them, and purging old checks |
| `routes.py`   | the HTTP endpoints below                                          |

The pieces they talk to live one level up:

| file                            | role                                        |
| ------------------------------- | ------------------------------------------- |
| `../service.py`                 | the outage state machine, plus slow-response, SSL-expiry and domain-expiry tracking |
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
          ├─ WebsiteService.due_for_check()      enabled, not in maintenance, and interval elapsed
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
`200`) after following redirects, and — if the site sets them — its body has
the text in `must_contain` and lacks the text in `must_not_contain`
(case-sensitive, first 1 MB, so GET or POST only).

`request_headers` adds up to 10 headers to every request: a token for a page
behind a login, a `Host` for one virtual host, or a `User-Agent` in place of
Watchly's, which a site's own header replaces. Each value is encrypted like a
bot token and never returned; the API shows names and a masked tail. On
update the list is replaced whole, and a header sent with `value: null` keeps
the value stored under its name. Headers follow redirects; httpx drops only
`Authorization` when one leaves the host.

A failed probe is repeated `retries_on_failure` times (default 1),
`CHECK_RETRY_DELAY_SECONDS` apart, inside `check_website`. Only the last result
is recorded, so a one-off blip leaves no failed check and raises no alert; set
it to 0 on a site that must alert on the first failure.

Each `website_checks` row keeps what the probe learned, not just the verdict:
the status and reason phrase, the `error_type` (see below), the diagnostic
headers, and how long each step took — `dns_ms`, `connect_ms`, `tls_ms` and
`first_byte_ms` (request sent → response headers back), summed over any
redirects. A step that did not finish, or that a reused connection skipped, is
`null`. It also keeps the `redirects` followed (each hop's URL, status and
`location`; `null` when there were none) and the `content_length` of the
decoded body. The site's page shows these under a check's **Details**, with the
step times as a bar and the diagnostic headers (including `content-encoding`
and `server-timing`).

`error_type` is the key to group incidents by:

| error_type           | meaning                                            |
| -------------------- | -------------------------------------------------- |
| `dns_error`          | the hostname did not resolve                       |
| `connect_error`      | connection refused, reset, or no route to the host |
| `connect_timeout`    | no TCP connection within the timeout               |
| `tls_error`          | the TLS handshake failed — e.g. an expired or untrusted certificate |
| `read_timeout`       | connected, but no response within the timeout      |
| `unexpected_status`  | answered, but not with `expected_status`           |
| `content_missing`    | answered as expected, but the body lacks `must_contain` |
| `content_forbidden`  | answered as expected, but the body has `must_not_contain` |
| `too_many_redirects`, `protocol_error`, `invalid_url`, `timeout` | as named |

To time DNS apart from the TCP connect, probes resolve the hostname
themselves (`_TimedBackend` in `checker.py`) rather than leaving it to
httpcore, which does both in one step. The connect then tries the addresses the
way anyio would have, Happy Eyeballs included.

### Ping checks

A site with `check_type` `ping` keeps a host name or IP address in `url`
(validated and normalized by `ping_host()` in `schemas.py`; the type cannot
change after creation). `check_website` hands it to `pinger.ping()`, which:

- resolves a name once, with `AI_ADDRCONFIG` so a dual-stack name is not pinged
  over a protocol this server lacks (its time goes in `dns_ms`);
- sends `ping_count` echo requests `PACKET_INTERVAL` (0.5 s) apart while
  listening, then waits up to `timeout_seconds` after the last — a silent host
  costs about `(count − 1) × 0.5 s + timeout`, not `count × timeout`;
- counts a reply slower than `timeout_seconds` as lost.

The check is **up when any request is answered**. `retries_on_failure` still
applies to a check with no replies. Each `website_checks` row keeps
`packets_sent` / `packets_received`, `rtt_min_ms` / `rtt_avg_ms` /
`rtt_max_ms` / `jitter_ms` (fractional ms) and the `ip_address` pinged;
`response_time_ms` is the average round trip, rounded, so rollups, charts, the
slow alert and the monthly report work unchanged. The hourly rollup also sums
the packets, which is where `packet_loss_percent` in the stats comes from.

| error_type      | meaning                                                   |
| --------------- | --------------------------------------------------------- |
| `no_reply`      | no echo reply within the timeout                          |
| `dns_error`     | the host name did not resolve                             |
| `unreachable`   | a router or the host answered "destination unreachable" (not seen with unprivileged sockets on Linux) |
| `ttl_exceeded`  | the request looped until its TTL ran out                  |
| `network_error` | this server could not send to the address, e.g. no route or no IPv6 |

Pings use **unprivileged ICMP (datagram) sockets**, which Linux allows only to
the groups in `net.ipv4.ping_group_range`; `docker-compose.yml` opens it for
the API container, and recent Docker does so by default. `PING_PRIVILEGED=true`
uses raw sockets instead, which need root or `CAP_NET_RAW`. When no ICMP socket
can be opened at all, `pinger` raises `IcmpUnavailableError`: that is this
server's problem, not the host's, so the scheduler logs it and records nothing
(no false outage), and `POST .../check` answers `503` with the fix.

### DNS checks

A site with `check_type` `dns` keeps a domain name in `url` (`dns_name()` in
`schemas.py`: a host name, never an IP address or a URL; underscores allowed,
for names like `_dmarc.example.com`) and the record to watch in
`dns_record_type`: `A`, `AAAA`, `CNAME`, `MX` or `TXT`. `check_website` hands
it to `dns_probe.lookup()`, which queries every resolver in `DNS_RESOLVERS`
directly and at once — UDP, then TCP when the answer is truncated — with
`timeout_seconds` (default 5) for each, following a CNAME chain to the type
asked for as a stub resolver does.

Records are compared as text, each written one way whether a resolver returned
it (`dns_probe.record_text`) or a person pinned it (`schemas.dns_value`): an
address compressed, a name in lower case without its final dot, an MX value as
`preference host`, a TXT value with its strings joined and unquoted. A record
set is sorted, so the order a resolver lists it in does not matter.

`dns_probe.judge` decides the check:

1. **Down** when fewer than half the resolvers return the record. The
   `error_type` is the most common failure among them, ties going to the most
   telling.
2. **Down** with `dns_mismatch` when `dns_expected_values` are pinned and any
   resolver returns other records, or answers `nxdomain` / `no_records`.
   Timeouts and failures at a minority of resolvers are tolerated.
3. Otherwise **up**, though the resolvers may still disagree (`consistent`
   false), which the UI shows as degraded.

| error_type      | meaning                                                   |
| --------------- | --------------------------------------------------------- |
| `nxdomain`      | the name does not exist                                   |
| `no_records`    | the name exists, but has no record of this type           |
| `servfail`      | the resolver failed — also its answer for broken DNSSEC   |
| `refused`       | the resolver refused to answer                            |
| `timeout`       | no answer within `timeout_seconds`                        |
| `network_error` | this server could not reach the resolver                  |
| `dns_error`     | a malformed answer, a CNAME chain that never ends, or another rcode |
| `dns_mismatch`  | a resolver's answer contradicts the pinned values         |

Each `website_checks` row keeps the whole outcome in the `dns` JSONB column —
record type, pinned values, the agreed `records`, `consistent`, and every
resolver's `records`, `ttl`, `time_ms` and error — and `response_time_ms` is the
resolvers' average answer time, so rollups, charts, the slow alert and the
monthly report work unchanged.

`websites.url` is unique per `(url, check_type, dns_record_type)`, with the
record type null for other checks and `NULLS NOT DISTINCT`, so a domain can be
pinged and have each of its records watched at once.

### Database checks

A site with `check_type` `database` keeps an endpoint in `url`, always
`host:port` (`[v6]:port` for IPv6; `database_endpoint()` in `schemas.py`, the
port defaulting to the engine's), and the protocol in `db_engine`:
`postgresql`, `mysql`, `redis` or `mongodb`. It needs **no user name or
password**: `check_website` hands it to `db_probe.probe()`, which goes as far
as the server's first real answer and never logs in. A connection URL is taken
apart for its host and port, but one with credentials in it is refused, so
they are never stored; the web form drops them before sending.

| engine | what the probe does | up | down |
| --- | --- | --- | --- |
| PostgreSQL | asks for TLS (uses it when offered), starts a session as `watchly` | an authentication request (`asks for a SCRAM-SHA-256 password`), or a refusal by `pg_hba.conf` | SQLSTATE class 53, 57 or 58: too many clients, starting up, shutting down, in recovery |
| MySQL, MariaDB | reads the greeting (version), then logs in as `watchly` with no password, over TLS when offered, and hangs up | a greeting, whatever the login brings | an error instead of the greeting (`1040` too many connections, shutting down, offline) |
| Redis, Valkey | `PING` | `PONG`, `NOAUTH`, any other refusal of Watchly | `LOADING`, `BUSY`, `MASTERDOWN`, `TRYAGAIN`, max clients reached |
| MongoDB, DocumentDB | `hello` (`isMaster` on servers too old for it) | primary, secondary, arbiter, mongos, standalone | a member that is neither (starting up, recovering, never initiated), or an error |

Nothing answering, or something answering that is not the engine (wrong port,
or a proxy with nothing behind it) is down too, which is what a TCP check
cannot tell: a proxy takes connections while its database is gone.

Why the MySQL probe logs in: MySQL and MariaDB count a client that hangs up
mid-handshake against `max_connect_errors` (100 by default) and then block its
address until `FLUSH HOSTS`. A failed login does not count. Tested on MariaDB
11 with `max_connect_errors=3`: three greeting-only connections blocked the
address, while repeated probes never did. A login refused for want of TLS
(`require_secure_transport`) does count, hence TLS whenever the server offers
it. The cost: MariaDB logs `Access denied for user 'watchly'` as a warning
(`log_warnings=2`, its default) on each check; MySQL 8.4 logs nothing at its
default verbosity. PostgreSQL logs nothing: it does not log a client that
hangs up at the password prompt.

Redis and MongoDB cannot switch to TLS mid-connection, so `db_tls` asks for
it from the start (ElastiCache with in-transit encryption, DocumentDB,
Atlas). Certificates are never verified; nothing secret is sent.
`timeout_seconds` (default 5) applies to each step: connecting, TLS, the
answer.

| error_type      | meaning                                                   |
| --------------- | --------------------------------------------------------- |
| `connect_timeout` | nothing answered: a firewall or security group drops the packets, or the host is down |
| `connect_refused` | the host is up, but nothing listens on the port         |
| `tls_error`     | the TLS handshake failed or never finished                |
| `read_timeout`  | connected, but the answer did not come                    |
| `connection_closed` | the server hung up before answering: not the engine, a proxy with nothing behind it, or (Redis, MongoDB) a server that needs TLS |
| `protocol_error` | something answered, but not as the engine does          |
| `db_unavailable` | the server says it cannot take connections now          |
| `db_too_many_connections` | the server says it has no connection to spare  |

Each `website_checks` row keeps what the server said in the `database` JSONB
column (`DbResult.as_dict()`), the address connected to in `ip_address`, and
the steps in `dns_ms`, `connect_ms`, `tls_ms` and `first_byte_ms` (the
request to the server's answer), so rollups, the time breakdown, the slow
alert and the monthly report work unchanged. The egress policy applies as for
HTTP and ping checks: a database at a private address is refused under
`WEBSITE_PRIVATE_TARGETS=block`, and is watched from an infrastructure
project instead. No domain registration is looked up for an endpoint.

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

A pinged host that answers some pings but not all is not down. When it loses
at least `packet_loss_threshold_percent` of them (`PACKET_LOSS_THRESHOLD_PERCENT`,
20, when unset) for `PACKET_LOSS_CHECKS` checks in a row, a **packet loss**
alert goes out once, then nothing for `PACKET_LOSS_ALERT_COOLDOWN_SECONDS` —
the same shape as the slow-response alert, which for a ping watches the average
round trip.

A DNS check with nothing pinned remembers, in `websites.dns_records`, the
records the resolvers last agreed on (`DnsResult.records`: the same set from
every resolver that returned one, and at least half did). When they agree on
different ones, a **DNS records changed** alert goes out once, with what was
added and removed. A check whose resolvers disagree changes nothing, so a
change still propagating is one alert when it completes, not one per check. A
pinned check still remembers the records, but a different answer is an outage
there, so no change alert fires. Changing the domain or the record type forgets
them, and a new record type clears the pinned values unless new ones are sent.

### Certificates and domains

The first successful HTTP check, and one every `CDN_CHECK_INTERVAL_SECONDS` (6
hours) after, also looks for a CDN (`cdn.py`): in the response headers
(`cf-ray`, `x-amz-cf-id`, `x-vercel-id`, a `server` or `via` naming it, ...) and
in the host's DNS aliases (a CNAME ending `.cloudfront.net`, `.fastly.net`,
...). The site keeps the providers found with their evidence, the CNAME chain
and the cache status (`HIT`/`MISS`, the `Age`). It is a hint and never alerts:
a CDN that strips its headers and sits behind plain address records goes
unseen, and a recognised one says nothing about whether it is set up well.

An HTTP check also records the address its host answered on (`ip_address`, the
first hop's when redirected). The first successful check, any check that finds
an address not seen lately, and one every `SERVER_CHECK_INTERVAL_SECONDS` (a
day) after, look that address up (`server_info.py`): its reverse (PTR) name, and
its network (ASN, name, announced range, registry country) from Team Cymru's
IP-to-ASN service, which answers over plain DNS and is sent only that address.
The site keeps it as `server`, with the addresses seen lately (`ips_seen`) and
whether `Alt-Svc` advertises HTTP/3. The country is where the block is
registered, not where a CDN or anycast address answers from. A private
address is not looked up. Set `SERVER_CHECK_ENABLED=false` to turn it off.

Every `SSL_CHECK_INTERVAL_SECONDS` (6 hours) an HTTPS site's certificate is
read without verifying it, so even an expired or untrusted one can be
described: its end date, who it was issued to and by, the names it covers
(`ssl_sans`, up to 100), when it began, the TLS version, the cipher suite
(`ssl_cipher`), whether the server agrees to HTTP/2 (`ssl_alpn`, `h2` or
`http/1.1`; the checks themselves stay on HTTP/1.1) and the certificates it
sent (`ssl_chain`, leaf first). It warns at each
of `SSL_EXPIRY_ALERT_DAYS` (14 and 7 days left) and once more if it
expires; a renewed certificate re-arms the warnings.

Once a day (`DOMAIN_CHECK_INTERVAL_SECONDS`) every check whose target is a
host name — an HTTP site's host, a pinged host name, a DNS check's domain —
looks up the domain it belongs to (`domain_lookup.py`). The registry is asked
over RDAP, the server IANA's bootstrap file names for the TLD; a TLD without
one (`.io`) is asked over WHOIS instead. Candidates go shortest first, so
`www.shop.example.co.uk` finds `example.co.uk`. What was learned is kept on
the site: `domain_name`, `domain_expires_at`, `domain_registrar`, and
`domain_error` when the last lookup failed (retried after an hour) or the
registry does not publish an expiry date at all (`.de`). IP addresses are
not looked up.

A **domain expiring** alert fires at each of `DOMAIN_EXPIRY_ALERT_DAYS` (14
and 7 days left) and once more if the registration lapses, like the
certificate's. A domain usually serves several of a project's sites, and
each looks it up, but each warning goes out once per project: the first site
to cross a threshold sends it and the rest only record that it was crossed.
So a site's own extra recipients hear about the domain only if that site is
the one that found it.

The same lookup keeps the nameservers the registry delegates the domain to
(`domain_nameservers`). When it names others than last time, a **nameservers
changed** alert goes out: whoever runs the new ones answers for every site
and mailbox on the domain, so an unplanned change is how a hijack looks. The
first set learned raises nothing, and a lookup that names none (`.de`) leaves
them as they were. Each change alerts once per project, like the expiry
warnings: a site that sees the move stamps `domain_nameservers_changed_at`,
and another site on the domain that sees the same move within two lookup
intervals stays quiet.

### Security headers

Every HTTP check that comes up keeps, on the site, the final response's
`Strict-Transport-Security`, `Content-Security-Policy`, `X-Frame-Options`,
`X-Content-Type-Options` and `Referrer-Policy` (`security_headers`). Only the
latest is kept, not one per check. `security_headers.py` grades them when the
site is read, each `ok`, `weak` or `missing`:

| header | weak when |
| ------ | --------- |
| HSTS | max-age is missing, 0 or under 180 days; missing on plain HTTP, where browsers ignore it |
| CSP | no `script-src` or `default-src`, or scripts allow `'unsafe-inline'` (without a nonce or hash) or `'unsafe-eval'` |
| X-Frame-Options | not `DENY` or `SAMEORIGIN`; a CSP `frame-ancestors` counts instead |
| X-Content-Type-Options | not `nosniff` |
| Referrer-Policy | `unsafe-url` or `no-referrer-when-downgrade` |

The grade is the number that are ok: A for five, B for four, down to F. It
does not alert.

### Maintenance windows

A deployment takes a site down for a minute and would page everyone. A
**maintenance window** — started by hand ("Start maintenance for 30 min" on the
site's page, `POST .../maintenance` with `duration_minutes`) or scheduled ahead
(`starts_at` and `ends_at`) — stops that:

| during the window | after it |
| ----------------- | -------- |
| `due_for_check` skips the site: no checks, no alerts, no uptime lost | the site is overdue, so it is checked on the next tick |
| a check made by hand is recorded, but moves no state and alerts nobody — not even `last_checked_at` | that check picks up from the state before the window: still down alerts, and a site down before the window and up now announces its recovery |

"End now" (`POST .../maintenance/end`) sets the window's `ends_at` to now; a
window that has not started can be cancelled (`DELETE
.../maintenance/{window_id}`). A site's windows never overlap, and one lasts at
most 7 days. `record_result` asks the database whether a window covers now
rather than trusting the loaded site, so a window that starts while a probe is
in flight still silences it. The dashboard counts a site in maintenance under
`maintenance`, not its status, and the `status` sort does not put it first
even when it is down.

`Website.maintenance_windows` loads only the windows that have not ended (its
join compares `ends_at` with `now()`), from which `maintenance` (in effect now)
and `upcoming_maintenance` are read.

In Slack, one outage is one thread: the down alert is posted to the channel,
the still-down alerts reply under it, and the recovery replies there too while
also showing in the channel. The thread's `ts` is kept on the website
(`slack_thread_ts`, `slack_thread_channel`) until the recovery. Telegram does
the same with replies: the still-down alerts and the recovery reply to the down
alert, whose id is kept in `telegram_thread_message_id` and
`telegram_thread_chat`. WhatsApp has no threads: each alert is a message of its
own.

---

## 5. Who receives an alert

**Every project must have at least one channel — email, Slack, Telegram,
WhatsApp, or any mix.** It is chosen when the project is created and enforced
on every later change: you cannot clear the last email recipient while no chat
channel is set, remove the last member, or drop Slack, Telegram or WhatsApp
while it is the only channel. There are no separate endpoints for configuring a
channel; it is all part of the project payload.

`GET /monitoring/projects/{id}` reports what is set as `alert_channels`, e.g.
`["email"]` or `["email", "slack", "telegram", "whatsapp"]`.

Sites inherit their project's channels and need nothing of their own — but
each can have its own recipients, and **a site must keep at least one
channel too**: one that stops inheriting the project's recipients needs a
recipient or address of its own (or Slack, Telegram or WhatsApp), and its last
one cannot be removed. `GET /monitoring/websites/{id}` reports the site's `alert_channels`.

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
refused with `422` (unless Slack, Telegram or WhatsApp is set up), since nobody
would be emailed. Slack, Telegram and WhatsApp are unaffected by the flag.

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

### Telegram

Optional, and set up exactly like Slack: each project carries **its own bot
token and chat id**, a site may override the chat while reusing the project's
bot, or bring **a bot of its own** — which works even when the project has no
Telegram.

| level | field | effect |
| ----- | ----- | ------ |
| project | `telegram_bot_token` | the bot to send as; encrypted at rest |
| project | `telegram_chat_id` | default chat for all the project's sites |
| project | `telegram_enabled` | mute the project's Telegram without discarding the settings |
| site | `telegram_chat_id` | that one site sends here instead |
| site | `telegram_bot_token` | that one site sends as this bot; needs the site's `telegram_chat_id`, and ignores the project's mute |

The same rules as Slack apply: a site's chat needs a bot from somewhere, its
own bot needs its own chat, and `PATCH` with `telegram_chat_id: null` removes
the site's Telegram, token included.

Setting it up:

1. Message **@BotFather**, send `/newbot`, and copy the token it gives you
   (`123456789:AA…`).
2. Add the bot to the group that should get alerts — or, for a channel, add it
   as an administrator that can post messages. For alerts to one person, they
   open the bot and press **Start** first; Telegram will not let a bot start a
   conversation.
3. Find the chat id: send a message in the group, then open
   `https://api.telegram.org/bot<token>/getUpdates` and read `chat.id` (groups
   and channels are negative, e.g. `-1001234567890`). A public channel can use
   `@channelname` instead.
4. `PATCH /monitoring/projects/{id}` with `telegram_bot_token` and
   `telegram_chat_id` — or the same two fields on
   `POST`/`PATCH /monitoring/websites` for one site. The project and website
   forms have a **Telegram alerts (optional)** section for the same.

Messages use Telegram's HTML formatting and are trimmed to its 4096-character
limit (trailing facts first, such as the HTTP headers). Should Telegram ever
reject the formatting, the alert is resent as plain text. Common refusals are
translated in the log: `chat not found` becomes "no such chat, or the bot is
not in it — add the bot to the group or channel…", and a group that became a
supergroup names its new chat id.

**The token is write-only**, like Slack's: encrypted with the same key, never
returned, and shown as a masked `telegram_token_hint` such as
`123456789:…wxyz`. It is part of every Bot API URL, so it is masked in logs too.

`TELEGRAM_BOT_TOKEN` with `TELEGRAM_CHAT_ID` is an optional global firehose for
projects with no Telegram of their own, like `SLACK_WEBHOOK_URL`.

### WhatsApp

Optional, through Meta's **WhatsApp Cloud API**. A project carries a *sender*
— a Meta access token and the Phone number ID of the business number that
sends — and the numbers to alert. A site may alert **numbers of its own**
instead; they are still sent from the project's business number, so a site
cannot have WhatsApp when its project has none.

| level | field | effect |
| ----- | ----- | ------ |
| project | `whatsapp_access_token` | a system user's token; encrypted at rest |
| project | `whatsapp_phone_number_id` | the business number that sends — its id, not the number |
| project | `whatsapp_recipients` | numbers alerted for all the project's sites, up to 10 |
| project | `whatsapp_enabled` | mute the project's WhatsApp without discarding the settings |
| site | `whatsapp_recipients` | that one site alerts these numbers instead; `[]` goes back to the project's |

The three project fields are set together. `PATCH` with any of them `null`, or
`whatsapp_recipients: []`, removes the project's WhatsApp. Numbers need their
country code; `+880 1712-345678` and `008801712345678` are both stored as
`+8801712345678`.

**Why a template.** WhatsApp lets a business write freely to someone only
within 24 hours of that person's last message to it. Outside that window only
an approved *message template* is delivered — and a plain message sent outside
it is accepted by the API, then silently dropped. So alerts are sent as the
template named by `WHATSAPP_TEMPLATE_NAME` (`watchly_alert` by default), with
the headline and the details as its two variables. Setting it blank sends
formatted free-form text instead, which is fine for a trial with numbers that
have just written to the business, but not for real alerting.

Setting it up:

1. In [Meta for Developers](https://developers.facebook.com/), create an app
   of type **Business** and add the **WhatsApp** product. API setup gives you a
   test number, and its **Phone number ID**. A test number can only message the
   (up to five) numbers you add under *To*; add your own business number for
   real use.
2. In **Business settings → System users**, create a system user, assign it
   the app and the WhatsApp account, and generate a token with the
   `whatsapp_business_messaging` permission, set to never expire. The
   temporary token on the API setup page stops working after 24 hours.
3. In **WhatsApp Manager → Message templates**, create a template named
   `watchly_alert`, category **Utility**, language **English** (`en`), with
   this body, word for word:

   ```
   Watchly monitoring update: {{1}}

   {{2}}

   You are receiving this because your number is on the WhatsApp alert list of a Watchly project.
   ```

   Give samples such as `🚨 Marketing site is down` and `Marketing site did
   not respond as expected: HTTP 503`, and wait for it to be approved
   (usually minutes). Another name or language works too: set
   `WHATSAPP_TEMPLATE_NAME` and `WHATSAPP_TEMPLATE_LANGUAGE`. A project with
   its own WhatsApp Business account needs the template there as well.
4. `PATCH /monitoring/projects/{id}` with `whatsapp_access_token`,
   `whatsapp_phone_number_id` and `whatsapp_recipients` — or give one site its
   own `whatsapp_recipients`. The project and website forms have a **WhatsApp
   alerts (optional)** section for the same.

Each number gets its own copy, one after the other; the alert counts as
delivered if any number got it. Refusals are translated in the log: `131030`
becomes "the number is not on the test number's list of allowed recipients…",
`132001` "no approved template by that name and language…". A refusal about
the token or the template stops the remaining numbers, which would fail the
same way.

**The token is write-only**, like the others: encrypted with the same key,
never returned, and shown as a masked `whatsapp_token_hint` such as `EAA…wxyz`.

`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` and `WHATSAPP_RECIPIENTS`
together are an optional global firehose for projects with no WhatsApp of
their own.

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
| start, end, schedule or cancel maintenance | admin, DevOps, the owner of its project |

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
| `POST` | `/monitoring/websites/{website_id}/maintenance` | `201 403 404 409 422` |
| `POST` | `/monitoring/websites/{website_id}/maintenance/end` | `200 403 404 422` |
| `DELETE` | `/monitoring/websites/{website_id}/maintenance/{window_id}` | `200 403 404 409 422` |

List filters: `limit` (1–100), `offset`, `status` (`unknown`/`up`/`down`),
`is_enabled`, `project_id`, `check_type` (`http`/`ping`/`dns`), `q` (name or URL),
`in_maintenance`, `sort` (`id`, `name`, `status`). The summary takes `project_id`, `check_type`
and `q`. History takes `limit` (1–500), newest
first; stats take `range` (`24h`, `7d`, `30d`, `90d`) and `format` (`json`, or
`csv` to download the series).

Site recipients: create accepts `recipient_ids`; afterwards `POST .../recipients`
with `{"recipient_ids": [...]}` adds several at once (idempotent), and
`DELETE .../recipients/{user_id}` removes one. `alert_emails` is replaced
wholesale by `PATCH`, like the project's `extra_emails`.

`POST .../check` probes immediately through the same state machine, so it can
raise and clear alerts — the fastest way to test a new site or your SMTP
credentials. During maintenance it is recorded but raises and clears nothing.

Maintenance: `POST .../maintenance` with `{"duration_minutes": 30}` starts it
now, or with `starts_at` and `ends_at` schedules it; `reason` is optional.
`POST .../maintenance/end` ends the one in effect (a no-op when there is none),
and `DELETE .../maintenance/{window_id}` cancels one that has not started. All
three return the site, whose `maintenance` and `upcoming_maintenance` show the
windows.

### Auth

| method | path | status codes |
| ------ | ---- | ------------ |
| `POST` | `/auth/signup` | `201 403 409 422` |
| `GET` | `/auth/signup` | `200` |
| `POST` | `/auth/login` | `200 401 403 422` |
| `POST` | `/auth/forgot-password` | `202 422` |
| `POST` | `/auth/confirm-email` | `200 403 404 409 410 422` |

Signup creates only the first account, as the `Admin`; the role cannot be set
from the payload. Once it exists signup answers `403` and `GET /auth/signup`
reports `{"open": false}`: people join by invitation.
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

Signed in as the admin — the first account signed up on the install:

```bash
BASE=http://127.0.0.1:8000/api/v1
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"identifier":"<admin username>","password":"<password>"}' | jq -r .tokens.access_token)

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
| `PING_PRIVILEGED` | `false` | ping over raw sockets (root / `CAP_NET_RAW`) instead of unprivileged ones |
| `PACKET_LOSS_THRESHOLD_PERCENT` | `20` | share of pings lost that makes a check lossy; `0` turns packet-loss alerts off |
| `PACKET_LOSS_CHECKS` | `3` | lossy checks in a row before alerting |
| `PACKET_LOSS_ALERT_COOLDOWN_SECONDS` | `21600` | quiet time after a packet-loss alert |
| `DNS_RESOLVERS` | Cloudflare, Google, Quad9, OpenDNS | the resolvers every DNS check asks, as `Name=IP address`, comma-separated |
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
- **Eager loading matters.** `Website.project`, `Website.recipients`,
  `Website.maintenance_windows` and `Project.members` use `lazy="selectin"` because the alert path reads them from
  a background task, where a lazy load would raise `MissingGreenlet`.
