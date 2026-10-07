import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { ConditionBadge, EventFeed, MetricChart, Usage } from '../Docker.jsx'
import { PROBLEM_LABELS, memory, rate } from '../docker.js'
import { dateTime, since, timeAgo } from '../format.js'
import { useApi } from '../useApi.js'

const RANGES = [
  { key: '24h', label: '24 hours' },
  { key: '7d', label: '7 days' },
  { key: '30d', label: '30 days' },
  { key: '90d', label: '90 days' },
]

function Fact({ label, children }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{children ?? '—'}</strong>
    </div>
  )
}

function Charts({ container }) {
  const [range, setRange] = useState('24h')
  const stats = useApi(() => api.containerStats(container.id, range), [container.id, range], { pollMs: 60000 })
  const s = stats.data
  // cpu is 100 per core; the chart shows the share of the whole host.
  const cpus = s?.cpus || 1
  const points = (s?.points ?? []).map((p) => ({
    ...p,
    cpu_avg: p.cpu_avg == null ? null : p.cpu_avg / cpus,
    cpu_max: p.cpu_max == null ? null : p.cpu_max / cpus,
  }))
  const daily = s && s.bucket_seconds >= 86400
  return (
    <section className="card">
      <h2>Resource use</h2>
      <div className="tabs" role="tablist" aria-label="Range">
        {RANGES.map((r) => (
          <button
            key={r.key}
            type="button"
            role="tab"
            aria-selected={range === r.key}
            className={range === r.key ? 'tab active' : 'tab'}
            onClick={() => setRange(r.key)}
          >
            {r.label}
          </button>
        ))}
      </div>
      <ErrorBanner error={stats.error} />
      {!s ? (
        stats.loading && <Loading />
      ) : !points.length ? (
        <p className="muted small">
          No samples in this range yet. The agent sends one with each heartbeat while the container runs.
        </p>
      ) : (
        <div className={s.range === range ? 'docker-charts' : 'docker-charts is-stale'}>
          <div>
            <h3>CPU</h3>
            <p className="muted small">Share of the host&apos;s {s.cpus ?? '?'} cores</p>
            <MetricChart
              points={points}
              daily={daily}
              max={100}
              label="CPU use over time"
              format={(v) => `${v < 10 && v > 0 ? v.toFixed(1) : Math.round(v)}%`}
              series={[
                { key: 'cpu_avg', label: 'average', className: 'series-1' },
                { key: 'cpu_max', label: 'peak', className: 'series-2' },
              ]}
            />
          </div>
          <div>
            <h3>Memory</h3>
            <p className="muted small">{s.mem_limit_bytes ? `Limit ${memory(s.mem_limit_bytes)}` : 'Without page cache'}</p>
            <MetricChart
              points={points}
              daily={daily}
              label="Memory use over time"
              format={memory}
              binary
              series={[
                { key: 'mem_avg', label: 'average', className: 'series-1' },
                { key: 'mem_max', label: 'peak', className: 'series-2' },
              ]}
            />
          </div>
          <div>
            <h3>Network</h3>
            <p className="muted small">Average over each {daily ? 'day' : s.bucket_seconds >= 3600 ? 'hour' : '5 minutes'}</p>
            <MetricChart
              points={points}
              daily={daily}
              label="Network traffic over time"
              format={rate}
              binary
              series={[
                { key: 'net_rx_avg', label: 'received', className: 'series-1' },
                { key: 'net_tx_avg', label: 'sent', className: 'series-2' },
              ]}
            />
          </div>
        </div>
      )}
    </section>
  )
}

export default function DockerContainerDetail() {
  const { id } = useParams()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const container = useApi(() => api.getContainer(id), [id], { pollMs: 15000 })
  const host = useApi(
    () => (container.data ? api.getDockerHost(container.data.host.id) : null),
    [container.data?.host.id],
  )
  const events = useApi(() => api.dockerEvents({ container_id: id, limit: 40 }), [id], { pollMs: 30000 })

  if (container.loading) return <Loading />
  if (!container.data) {
    return (
      <>
        <ErrorBanner error={container.error} />
        <Link to="/docker">← Back to Docker</Link>
      </>
    )
  }
  const c = container.data
  const problems = Object.entries(c.problems ?? {})

  async function toggleMute() {
    setBusy(true)
    setError(null)
    try {
      container.setData(await api.updateContainer(c.id, { muted: !c.muted }))
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <p className="crumbs">
        <Link to="/docker">Docker</Link> / <Link to={`/docker/hosts/${c.host.id}`}>{c.host.name}</Link>
      </p>
      <PageHeader
        title={
          <>
            {c.name} <ConditionBadge condition={c.condition} />
            {c.muted && <span className="badge badge-paused">muted</span>}
          </>
        }
        subtitle={c.image}
      >
        {c.can_manage && (
          <button type="button" className="btn" onClick={toggleMute} disabled={busy}>
            {c.muted ? 'Unmute alerts' : 'Mute alerts'}
          </button>
        )}
      </PageHeader>
      <ErrorBanner error={error ?? container.error} />

      {c.condition === 'unknown' && (
        <div className="banner banner-action">
          Its host isn&apos;t reporting right now, so this is how it was {timeAgo(c.last_seen_at)}.
        </div>
      )}
      {c.condition === 'removed' && (
        <div className="banner banner-info">
          Removed from its host {timeAgo(c.removed_at)}. It is forgotten a few days after removal.
        </div>
      )}
      {c.down_since && (
        <div className="banner banner-error" role="alert">
          Down for {since(c.down_since)}
          {c.exit_code != null && `, last exit code ${c.exit_code}`}
          {c.oom_killed && ', killed for lack of memory'}.
        </div>
      )}
      {problems.length > 0 && (
        <div className="banner banner-action">
          {problems.map(([key, p]) => (
            <div key={key}>
              <strong>{PROBLEM_LABELS[key] ?? key}</strong>: {p.detail} (since {timeAgo(p.since)})
            </div>
          ))}
        </div>
      )}

      <section className="card">
        <div className="kv kv-inline">
          <Fact label="Uses">
            <Usage container={c} cpus={host.data?.cpus} />
          </Fact>
          <Fact label="Docker state">{c.status_text ?? c.state}</Fact>
          <Fact label="Health">{c.health === 'none' ? 'no healthcheck' : c.health}</Fact>
          <Fact label="Restarts">{c.restart_count}</Fact>
          <Fact label="Last exit">
            {c.exit_code == null ? null : `code ${c.exit_code}${c.oom_killed ? ' (OOM)' : ''}`}
          </Fact>
          <Fact label="Started">{c.started_at ? dateTime(c.started_at) : null}</Fact>
          <Fact label="Finished">{c.finished_at ? dateTime(c.finished_at) : null}</Fact>
          {c.compose_project && (
            <Fact label="Compose">
              {c.compose_project} / {c.compose_service ?? '—'}
            </Fact>
          )}
          <Fact label="Container id">
            <code title={c.docker_id}>{c.docker_id.slice(0, 12)}</code>
          </Fact>
          <Fact label="Watched since">{dateTime(c.first_seen_at)}</Fact>
        </div>
        {c.muted && (
          <p className="muted small">Muted: its state is still recorded, but nothing about it alerts anyone.</p>
        )}
      </section>

      {c.condition === 'removed' ? null : <Charts container={c} />}

      <section className="section">
        <h2>Activity</h2>
        {events.data ? (
          <EventFeed events={events.data.items} emptyLabel="Nothing has happened to it since Watchly started watching." />
        ) : events.loading ? (
          <Loading />
        ) : (
          <Empty>
            <ErrorBanner error={events.error} />
          </Empty>
        )}
      </section>
    </>
  )
}
