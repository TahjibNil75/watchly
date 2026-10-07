# Docker monitoring

Watchly watches the containers on your own servers through a small agent that
runs next to them. It is a separate program,
[watchly-docker-agent](https://github.com/TahjibNil75/watchly-docker-agent).
The agent only makes **outbound** HTTPS requests to Watchly, so the server needs
no open port, VPN or SSH access. Your firewall only has to allow outbound
traffic.

It is on by default (`DOCKER_ENABLED=true`).

## How it works

```
 your server                                     Watchly
┌───────────────────────────────┐   HTTPS POST  ┌────────────────────────────┐
│ containers ◄── Docker API ──┐ │  every 30 s + │ /api/v1/docker/ingest      │
│                             │ │  after events │   → container state        │
│        watchly-agent ───────┘ ├──────────────►│   → samples, feed          │
│  (reads the socket, GET only) │ ◄──── config ─│   → alerts                 │
└───────────────────────────────┘               │ scheduler: hosts gone quiet│
                                                └────────────────────────────┘
```

- **A heartbeat every interval.** The default is 30 s, and you set it per host
  in Watchly. It carries every container's state, health, restart count, last
  exit code, OOM flag, and CPU, memory, network and disk rates.
- **A small push about 2 s after a Docker event**, such as a crash, an OOM
  kill, a healthcheck turning unhealthy, or a stop. Alerts don't wait for the
  next heartbeat.
- **Settings come back on every push.** Watchly answers each push with the
  host's interval and the containers to skip, so you change them in Watchly and
  never on the server.
- **Hosts that go quiet.** Watchly's scheduler marks a host offline when its
  agent stops pushing.

The agent uses about 10 MiB of memory and close to 0% CPU. The wire format is
specified in the agent's
[`docs/protocol.md`](https://github.com/TahjibNil75/watchly-docker-agent/blob/main/docs/protocol.md).

## Connect a host

1. **Create a Docker project.** Go to *Projects → New project → Docker* and set
   up who gets alerted, as for any project.
2. **Add the host.** Go to *Docker → Add host*, pick the project and name the
   host. Watchly shows the agent token **once**, along with a ready-to-paste
   `docker run` command and a compose service. Only a hash of the token is
   kept, so if you lose it, make a new one with *New token* on the host's page.
3. **Run the agent on the server** with that command. The page updates by
   itself once the agent first reports.

```sh
docker run -d --name watchly-agent --restart unless-stopped \
  -e WATCHLY_URL=https://watchly.example.com \
  -e WATCHLY_TOKEN=wdk_… \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --cpus 0.1 --memory 64m \
  ghcr.io/tahjibnil75/watchly-docker-agent:latest
```

`WATCHLY_URL` is the address you open Watchly at. The agent posts to
`$WATCHLY_URL/api/v1/docker/ingest`, which nginx forwards to the API like any
other `/api` request.

To check the setup without leaving anything running, use `--rm` instead of
`-d` and add `check` at the end.

To skip a container, label it `watchly.ignore=true`, or add a name pattern
(`buildkit_*`) to the host's *Settings*.

## What alerts

All Docker alerts go to the project's members and extra emails,
`ALERT_DEFAULT_EMAILS`, and the project's Slack, Telegram and WhatsApp. They
also go to the webhook. Each kind can be switched off per channel, and its
wording changed, under *Notifications*.

| Kind | When |
| --- | --- |
| Container down | A container Watchly saw running stopped (exited, dead, or stuck restarting) and stayed so `DOCKER_DOWN_GRACE_SECONDS` (60). The grace period keeps a redeploy quiet. The alert says how it stopped: exit code, OOM kill, or `docker stop`. A container first seen stopped never alerts, since it may be a finished job. |
| Container back up | It runs again, with how long it was down. |
| Container unhealthy | It runs, but its own Docker healthcheck reports unhealthy. |
| Container out of memory | Docker reported an `oom` event, or the container exited OOM-killed. |
| Container restart loop | Its restart policy restarted it `DOCKER_RESTART_LOOP_COUNT` (3) times within `DOCKER_RESTART_LOOP_WINDOW_SECONDS` (10 min). |
| Container running hot | It stayed over `DOCKER_CPU_ALERT_PERCENT` of the host's CPU (all cores), or over `DOCKER_MEMORY_ALERT_PERCENT` of its memory limit, for `DOCKER_PROBLEM_CHECKS` heartbeats. |
| Docker host offline | The agent stayed quiet for `DOCKER_OFFLINE_AFTER_SECONDS` (or 3 intervals, whichever is longer), or it reports that it cannot reach Docker. This is **one** alert for the host. Its containers show as *unknown* and don't alert until it reports again. |
| Docker host back | It reports again, with how long it was offline. |

"Running hot", unhealthy, OOM and restart loop each alert as they start, then
at most every `DOCKER_PROBLEM_ALERT_COOLDOWN_SECONDS` (1 h) per container.

On a container's page, **Mute alerts** keeps recording its state but stops it
from alerting anyone. Use it for a container you stop on purpose.

A container that disappears from its host (`docker rm`, `compose down`) is
marked *removed* and doesn't alert. It is forgotten after
`DOCKER_REMOVED_CONTAINER_DAYS`. Containers are known by name, so a recreated
container (new id, same name) keeps its history.

## History and retention

- **Raw samples.** The agent sends one sample per running container per
  heartbeat. Watchly keeps these for `DOCKER_SAMPLE_RETENTION_HOURS` (48 h) and
  charts the last day from them in 5-minute buckets.
- **Hourly rollups.** Samples are rolled up by the hour and kept like website
  checks. They chart 7, 30 and 90 days.
- **The feed.** Docker's own events plus Watchly's decisions are kept
  `CHECK_RETENTION_DAYS`.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `DOCKER_ENABLED` | `true` | Docker projects, the Docker page and the ingest endpoint. Off, they all answer 404. |
| `DOCKER_OFFLINE_AFTER_SECONDS` | `120` | Quiet time before a host is offline (at least 3 of its intervals) |
| `DOCKER_DOWN_GRACE_SECONDS` | `60` | Stopped time before a container is down |
| `DOCKER_RESTART_LOOP_COUNT` / `_WINDOW_SECONDS` | `3` / `600` | What makes a restart loop |
| `DOCKER_CPU_ALERT_PERCENT` | `90` | Share of the host's CPU; `0` turns CPU alerts off |
| `DOCKER_MEMORY_ALERT_PERCENT` | `90` | Share of the container's memory limit (of the host's memory when it has none); `0` turns memory alerts off |
| `DOCKER_PROBLEM_CHECKS` | `3` | Heartbeats over a threshold before it alerts |
| `DOCKER_PROBLEM_ALERT_COOLDOWN_SECONDS` | `3600` | Quiet time between alerts about the same problem |
| `DOCKER_SAMPLE_RETENTION_HOURS` | `48` | Raw samples kept |
| `DOCKER_REMOVED_CONTAINER_DAYS` | `7` | How long removed containers stay listed |
| `RATE_LIMIT_DOCKER_INGEST` | `60/minute` | Pushes per agent token |

## Security

- **Tokens.** Each host has its own token (`wdk_…`). Watchly stores only its
  SHA-256 hash, and *New token* revokes the old one at once. A token can only
  push for its own host. It cannot read anything from Watchly.
- **Rate limits.** The ingest endpoint is limited per token, not per address,
  so many hosts behind one NAT don't share a limit.
- **The Docker socket.** Mounting it gives full control of Docker, and `:ro`
  does not change that. The agent only ever sends `GET` requests. To have that
  *enforced*, run the agent behind a socket proxy that lets only those through;
  the agent's README has a compose file for it.
- **What is sent.** Never environment variables, commands, mounts, logs or
  arbitrary labels. Only compose project/service labels and `watchly.*` labels
  are sent.
- **Country blocking.** If `COUNTRY_ALLOW` / `COUNTRY_DENY` is set, it applies
  to the agents' pushes too. Allow the countries your servers are in.

## API

The endpoints are listed in [`apis.md`](apis.md#docker).
