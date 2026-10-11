# How the Docker agent and Watchly connect

A small agent, [watchly-docker-agent](https://github.com/TahjibNil75/watchly-docker-agent),
runs on each Docker host and **pushes** the host's state to Watchly over HTTPS.
Watchly never connects back, so the host needs no open port, VPN or SSH. The
wire format is the agent's
[`docs/protocol.md`](https://github.com/TahjibNil75/watchly-docker-agent/blob/main/docs/protocol.md).

## How they get connected

1. **Watchly issues a token.** *Docker → Add host* creates a host and a token
   (`wdk_…`). The token is shown once, and only its SHA-256 hash is stored.
   *New token* replaces it and revokes the old one at once.
2. **You run the agent** with `WATCHLY_URL` (the address you open Watchly at)
   and `WATCHLY_TOKEN`.
3. **The agent pushes.** It sends `POST $WATCHLY_URL/api/v1/docker/ingest` with
   `Authorization: Bearer <token>`, a gzip JSON body and `X-Watchly-Schema: 1`.
4. **Watchly finds the host by the token's hash.** A token can only push for its
   own host and can read nothing back. The first accepted push moves the host
   from *pending* to *online*.
5. **Watchly answers with settings.** Every response carries the host's
   `interval_s` and `ignore` patterns, so you change them in Watchly and never
   on the server.

If it doesn't connect, the status code says why: `401` is a wrong or rotated
token, `404` is a wrong `WATCHLY_URL` or `DOCKER_ENABLED=false`, and `429` is
the per-token rate limit (`RATE_LIMIT_DOCKER_INGEST`). The agent keeps
retrying and never exits.

## How monitoring happens

```
 agent                                        Watchly
 ─────                                        ───────
 reads the Docker socket (GET only)
 listens to the Docker event stream
        │
        ├─ heartbeat every 30 s ───────────►  POST /docker/ingest
        └─ event push ~2 s after a crash,       1. check the token
           OOM kill, unhealthy, stop …          2. update the host
                                                3. update every container
        ◄──────────── { interval_s, ignore }    4. store samples and events
                                                5. decide alerts, then send them

                                              scheduler, every tick
                                                 host quiet too long → offline
                                                 roll up samples, purge old rows
```

- **Every push lists all containers.** A heartbeat adds CPU, memory, network
  and disk rates. An event push has no metrics, so alerts don't wait for the
  next heartbeat.
- **Containers are matched by name.** A recreated container (new id, same name)
  keeps its history. One missing from a push is marked *removed*, and doesn't
  alert.
- **Alerts come from the state.** Watchly compares each push with the last one
  and decides: container down (after a grace period), back up, unhealthy, out
  of memory, restart loop, running hot, host offline and back. The row changes
  are committed first, then the alerts are sent, so one alert never goes out
  twice. See [`docker.md`](docker.md#what-alerts) for each rule.
- **Docker unreachable.** If the agent reports it can't reach Docker, the host
  becomes *docker_down* and one alert goes out. The containers keep their last
  state instead of being called down.
- **Events are deduplicated** on the id the agent gives each one, so a retried
  push doesn't repeat them.

## When the agent goes quiet

Since the agent pushes, silence is the signal. The scheduler's liveness job
(`DockerMonitor.mark_offline` in `app/monitoring/docker/monitor.py`) marks a
host offline when it hasn't pushed for `DOCKER_OFFLINE_AFTER_SECONDS` (120 s)
or 3 of its intervals, whichever is longer. That sends **one** alert for the
host, and its containers don't alert until it reports again. Events alone never
prove a host is alive, because an idle host sends only heartbeats.

## What Watchly keeps

Raw samples for 48 h, hourly rollups for the 7, 30 and 90 day charts, and a
feed of Docker's events plus Watchly's decisions. See
[`docker.md`](docker.md#history-and-retention).

## Where the code is

| What | Where |
| --- | --- |
| Ingest endpoint, token check, size limits | `app/monitoring/docker/routes.py` |
| Push → state, samples, alerts | `app/monitoring/docker/ingest.py` |
| Host token, add/rotate host | `app/monitoring/docker/service.py` |
| Offline detection, rollups, retention | `app/monitoring/docker/monitor.py`, `app/monitoring/scheduler.py` |

For setup, alerts, settings and security, see [`docker.md`](docker.md). The
endpoints are in [`apis.md`](apis.md#docker).
