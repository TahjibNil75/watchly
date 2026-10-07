import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { ContainerTable, EventFeed, HostStatusBadge } from '../Docker.jsx'
import { FILTERS, matchesFilter, memory, useDockerEnabled } from '../docker.js'
import { timeAgo } from '../format.js'
import { canCreateProjects } from '../roles.js'
import { useApi } from '../useApi.js'

function StateCard({ filter, counts, active, onPick }) {
  const n = filter.key === 'all' ? counts?.total : counts?.[filter.key]
  const alarm = filter.key === 'down' && n > 0
  const classes = ['stat', filter.className, active && 'is-active', alarm && 'is-alarm']
  return (
    <button type="button" className={classes.filter(Boolean).join(' ')} onClick={onPick} aria-pressed={active}>
      <span className="stat-label">
        {alarm && <span className="stat-ping" />}
        {filter.label}
      </span>
      <span className="stat-main">
        <span className="stat-value">{n ?? '–'}</span>
      </span>
      <span className="stat-note">
        {filter.key === 'all'
          ? counts?.total
            ? `${counts.running} of ${counts.total} running`
            : 'Nothing reported yet'
          : filter.note}
      </span>
    </button>
  )
}

function HostCard({ host }) {
  const c = host.counts
  const tone = host.status !== 'online' ? (host.status === 'pending' ? 'unknown' : 'down') : c.down ? 'down' : c.unhealthy ? 'degraded' : 'healthy'
  return (
    <Link to={`/docker/hosts/${host.id}`} className={`card vpc-card tone-${tone}`}>
      <div className="vpc-card-head">
        <strong>{host.name}</strong>
        <HostStatusBadge status={host.status} />
      </div>
      <div className="muted small">
        {host.status === 'pending'
          ? 'Waiting for its agent to report'
          : [host.hostname, host.docker_version && `Docker ${host.docker_version}`, host.cpus && `${host.cpus} CPUs`, host.mem_total_bytes && memory(host.mem_total_bytes)]
              .filter(Boolean)
              .join(' · ')}
      </div>
      <div className="vpc-card-counts small">
        <span>{c.total} containers</span>
        {c.down > 0 && <span className="text-down">{c.down} down</span>}
        {c.unhealthy > 0 && <span className="text-pending">{c.unhealthy} unhealthy</span>}
        {host.last_seen_at && <span className="muted">seen {timeAgo(host.last_seen_at)}</span>}
      </div>
      {c.total > 0 && (
        <span className="stat-bar" aria-hidden="true">
          {[
            ['healthy', c.running - c.unhealthy],
            ['degraded', c.unhealthy],
            ['down', c.down],
            ['paused', c.stopped],
          ].map(([cls, value]) => value > 0 && <i key={cls} className={`stat-bar-${cls}`} style={{ flexGrow: value }} />)}
        </span>
      )}
    </Link>
  )
}

export default function Docker() {
  const { user } = useAuth()
  const enabled = useDockerEnabled()
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')

  useEffect(() => {
    const timer = setTimeout(() => setQ(search.trim()), 300)
    return () => clearTimeout(timer)
  }, [search])

  const hosts = useApi(() => (enabled ? api.listDockerHosts() : null), [enabled], { pollMs: 15000 })
  const containers = useApi(() => (enabled ? api.listContainers({ q }) : null), [enabled, q], { pollMs: 15000 })
  const events = useApi(() => (enabled ? api.dockerEvents({ source: 'watchly', limit: 12 }) : null), [enabled], {
    pollMs: 30000,
  })

  if (enabled === null) return <Loading />
  if (!enabled) {
    return (
      <>
        <PageHeader title="Docker" />
        <Empty>
          Docker monitoring is off. Set <code>DOCKER_ENABLED=true</code> on the API to watch containers through the
          Watchly Docker agent.
        </Empty>
      </>
    )
  }

  const creator = canCreateProjects(user)
  const hostList = hosts.data?.items ?? []
  const byId = Object.fromEntries(hostList.map((h) => [h.id, h]))
  const items = containers.data?.items ?? []
  const shown = items.filter((c) => matchesFilter(c, filter))
  const nothingYet = hosts.data && hostList.length === 0

  return (
    <>
      <PageHeader
        title="Docker"
        subtitle="Containers on your Docker hosts, as their Watchly agents report them: state, health, restarts and resource use."
      >
        {creator && (
          <Link to="/docker/new" className="btn btn-primary">
            Add host
          </Link>
        )}
      </PageHeader>

      <ErrorBanner error={hosts.error ?? containers.error} />

      {nothingYet ? (
        <Empty>
          No Docker hosts yet.{' '}
          {creator ? (
            <>
              <Link to="/docker/new">Add one</Link>: Watchly gives it a token, and an agent you run on the host reports
              its containers.
            </>
          ) : (
            'Ask a project manager or admin to add one to a project of yours.'
          )}
        </Empty>
      ) : (
        <>
          <div className="stats-wrap">
            <div className="stats stats-docker">
              {FILTERS.map((f) => (
                <StateCard
                  key={f.key}
                  filter={f}
                  counts={containers.data?.counts}
                  active={filter === f.key}
                  onPick={() => setFilter(f.key)}
                />
              ))}
            </div>
          </div>

          {hostList.length > 0 && (
            <div className="vpc-cards">
              {hostList.map((host) => (
                <HostCard key={host.id} host={host} />
              ))}
            </div>
          )}

          <div className="infra-bar is-docker">
            <div className="infra-bar-row">
              <label className="infra-search">
                <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
                  <circle cx="9" cy="9" r="5.5" />
                  <path d="m13.5 13.5 4 4" />
                </svg>
                <input
                  type="search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search by name, image or compose project"
                  aria-label="Search containers"
                  maxLength={200}
                />
              </label>
            </div>
          </div>

          {containers.loading ? (
            <Loading />
          ) : (
            <ContainerTable
              containers={shown}
              hosts={byId}
              emptyLabel={
                items.length ? 'No containers match this search and filter.' : 'No containers reported yet.'
              }
            />
          )}

          <section className="section section-spaced">
            <h2>Recent alerts</h2>
            <EventFeed events={events.data?.items ?? []} showHost emptyLabel="No alerts yet." />
          </section>
        </>
      )}
    </>
  )
}
