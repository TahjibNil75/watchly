import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { Breadcrumbs, Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { ConditionBadge, EventFeed, MetricChart, RestartBadge, Sparkline } from '../Docker.jsx'
import { PROBLEM_LABELS, cpuShare, memory, memoryLimit, pct, rate } from '../docker.js'
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

// The limit worth drawing, once the host has said how much memory it has: a
// container without a limit reports the host's total, which isn't one.
function useMemoryLimit(host, statsLimit) {
  const ready = Boolean(host.data || host.error)
  return ready ? memoryLimit(statsLimit, host.data?.mem_total_bytes) : null
}

// The last day at a glance: where CPU and memory are now, and the shape of the
// day behind them. Memory is drawn against its limit.
function Vitals({ container, host }) {
  const stats = useApi(() => api.containerStats(container.id, '24h'), [container.id], { pollMs: 60000 })
  const s = stats.data
  const limit = useMemoryLimit(host, s?.mem_limit_bytes)
  const cpus = s?.cpus || host.data?.cpus || 1
  const points = s?.points ?? []
  const cpuValues = points.map((p) => (p.cpu_avg == null ? null : p.cpu_avg / cpus))
  const memValues = points.map((p) => p.mem_avg)
  const m = container.metrics
  const live = m && ['running', 'unhealthy'].includes(container.condition)
  const cpuNow = live ? cpuShare(m.cpu_pct, cpus) : null
  const memPeak = Math.max(0, ...points.map((p) => p.mem_max ?? 0))
  const cpuPeak = Math.max(0, ...cpuValues.map((v) => v ?? 0))

  const none = !points.length && (
    <span className="muted small">{stats.loading ? 'Loading…' : 'No samples in the last day.'}</span>
  )
  return (
    <div className="docker-vitals">
      <div className="docker-vital">
        <span>CPU, share of the host</span>
        <strong>{cpuNow == null ? '—' : pct(cpuNow, cpuNow < 10 ? 1 : 0)}</strong>
        {none || (
          <Sparkline
            values={cpuValues}
            floor={5}
            label={`CPU over the last 24 hours, average, peaking at ${pct(cpuPeak, cpuPeak < 10 ? 1 : 0)} of the host`}
          />
        )}
        <span>Last 24 hours</span>
      </div>
      <div className="docker-vital">
        <span>Memory{limit ? ` of ${memory(limit)}` : ''}</span>
        <strong>
          {live ? memory(m.mem_used_bytes) : '—'}
          {live && limit && <span className="muted"> · {pct((m.mem_used_bytes / limit) * 100)}</span>}
        </strong>
        {none || (
          <Sparkline
            values={memValues}
            limit={limit ?? undefined}
            floor={1}
            label={`Memory over the last 24 hours, average, peaking at ${memory(memPeak)}${limit ? ` against a ${memory(limit)} limit` : ''}`}
          />
        )}
        <span>{limit ? 'Last 24 hours · dashed line is the limit' : 'Last 24 hours · no limit set'}</span>
      </div>
    </div>
  )
}

function Charts({ container, host }) {
  const [range, setRange] = useState('24h')
  const stats = useApi(() => api.containerStats(container.id, range), [container.id, range], { pollMs: 60000 })
  const s = stats.data
  const limit = useMemoryLimit(host, s?.mem_limit_bytes)
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
            <p className="muted small">{limit ? `Limit ${memory(limit)}` : 'Without page cache'}</p>
            <MetricChart
              points={points}
              daily={daily}
              label="Memory use over time"
              format={memory}
              binary
              limit={limit ? { value: limit, label: `limit ${memory(limit)}` } : undefined}
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
      <Breadcrumbs
        items={[
          ...(host.data?.project
            ? [
                { label: 'Projects', to: '/projects' },
                { label: host.data.project.name, to: `/projects/${host.data.project.id}` },
              ]
            : [{ label: 'Docker', to: '/docker' }]),
          { label: c.host.name, to: `/docker/hosts/${c.host.id}` },
          { label: c.name },
        ]}
      />
      <PageHeader
        title={
          <>
            {c.name} <ConditionBadge condition={c.condition} />
            <RestartBadge count={c.restart_count} looping={Boolean(c.problems?.restart_loop)} />
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
        {c.condition !== 'removed' && <Vitals container={c} host={host} />}
        <div className="kv kv-inline">
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

      {c.condition === 'removed' ? null : <Charts container={c} host={host} />}

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
