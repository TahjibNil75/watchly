import { Link, useOutletContext } from 'react-router-dom'
import { api } from '../api.js'
import { ErrorBanner, PageHeader } from '../components.jsx'
import { CONDITIONS, matchesFilter, useDockerEnabled } from '../docker.js'
import { DockerIcon } from '../Docker.jsx'
import { timeAgo } from '../format.js'
import { stateLabel, useInfraEnabled } from '../infra.js'
import { useApi } from '../useApi.js'

const POLL_MS = 30000

// One monitor's card: the count that matters, how the rest is doing, and a
// link to its page. A monitor with problems takes the down tone.
function Tile({ to, icon, label, value, note, problems, empty }) {
  const tone = empty ? 'stat-paused' : problems ? 'stat-down' : 'stat-up'
  return (
    <Link to={to} className={`stat overview-tile ${tone}`}>
      <span className="stat-label">
        {icon}
        {label}
      </span>
      <span className="stat-main">
        <span className="stat-value">{value ?? '–'}</span>
      </span>
      <span className="stat-note">{note}</span>
    </Link>
  )
}

function SvgIcon({ children }) {
  return (
    <svg
      className="icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

const WebsiteIcon = () => (
  <SvgIcon>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M3 9h18M6.5 6.5h.01M9.5 6.5h.01" />
  </SvgIcon>
)
const InfraIcon = () => (
  <SvgIcon>
    <rect x="3.5" y="4" width="17" height="6.5" rx="1.5" />
    <rect x="3.5" y="13.5" width="17" height="6.5" rx="1.5" />
    <path d="M7.5 7.25h.01M7.5 16.75h.01M11 7.25h5.5M11 16.75h5.5" />
  </SvgIcon>
)
const ProjectIcon = () => (
  <SvgIcon>
    <path d="M20 17a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3.9a2 2 0 0 1-1.69-.9l-.81-1.2a2 2 0 0 0-1.67-.9H8a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2zM2 8v11a2 2 0 0 0 2 2h14" />
  </SvgIcon>
)

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`

// "1 down · 4 degraded", or what to say when there is nothing to report.
const breakdown = (parts, fallback) => {
  const shown = parts.filter(([n]) => n > 0).map(([n, word]) => `${n} ${word}`)
  return shown.length ? shown.join(' · ') : fallback
}

function websiteTile(w) {
  if (!w) return { value: null, note: 'Loading…' }
  if (!w.total) return { value: 0, note: 'Nothing watched yet', empty: true }
  return { value: w.total, note: breakdown([[w.down, 'down']], 'All up'), problems: w.problems }
}

function infraTile(r) {
  if (!r) return { value: null, note: 'Loading…' }
  if (!r.total) return { value: 0, note: 'No resources yet', empty: true }
  return {
    value: r.total,
    note: breakdown(
      [
        [r.down, 'down'],
        [r.degraded, 'degraded'],
      ],
      'All healthy',
    ),
    problems: r.problems,
  }
}

function dockerTile(d) {
  if (!d) return { value: null, note: 'Loading…' }
  if (!d.total) return { value: 0, note: d.hosts ? 'No containers reported' : 'No hosts yet', empty: true }
  return {
    value: d.total,
    note: breakdown(
      [
        [d.down, 'down'],
        [d.unhealthy, 'unhealthy'],
      ],
      'All running',
    ),
    problems: d.problems,
  }
}

// Everything that is down or failing, from every monitor, worst first.
function useAttention({ infra, docker }) {
  const sites = useApi(
    () => api.listWebsites({ status: 'down', is_enabled: true, in_maintenance: false, limit: 20 }),
    [],
    { pollMs: POLL_MS },
  )
  const resources = useApi(() => (infra ? api.infraOverview({}) : null), [infra], { pollMs: POLL_MS })
  const containers = useApi(() => (docker ? api.listContainers() : null), [docker], { pollMs: POLL_MS })

  const rows = []
  for (const site of sites.data?.items ?? []) {
    rows.push({
      key: `site-${site.id}`,
      to: `/websites/${site.id}`,
      source: 'Website',
      name: site.name,
      detail: site.url,
      badge: 'down',
      label: 'down',
      since: site.down_since,
    })
  }
  for (const item of resources.data?.attention ?? []) {
    rows.push({
      key: `infra-${item.resource.id}`,
      to: `/infra/resources/${item.resource.id}`,
      source: 'Infra',
      name: item.resource.name,
      detail: item.summary,
      badge: item.state,
      label: stateLabel(item.state).toLowerCase(),
      since: item.since,
    })
  }
  for (const c of containers.data?.items ?? []) {
    if (!matchesFilter(c, 'down') && !matchesFilter(c, 'unhealthy')) continue
    const condition = CONDITIONS[c.condition] ?? CONDITIONS.unknown
    rows.push({
      key: `docker-${c.id}`,
      to: `/docker/containers/${c.id}`,
      source: 'Docker',
      name: c.name,
      detail: c.host?.name,
      badge: condition.badge,
      label: condition.label,
      since: c.down_since,
    })
  }
  // Down before degraded, then the longest-standing first.
  const weight = (row) => (row.badge === 'down' ? 0 : 1)
  rows.sort((a, b) => weight(a) - weight(b) || new Date(a.since ?? 0) - new Date(b.since ?? 0))

  return {
    rows,
    loading: sites.loading || resources.loading || containers.loading,
    error: sites.error ?? resources.error ?? containers.error,
  }
}

const SHOWN = 10

export default function Overview() {
  const counts = useOutletContext()
  const infra = useInfraEnabled()
  const docker = useDockerEnabled()
  const projects = useApi(() => api.listProjects(), [])
  const attention = useAttention({ infra, docker })

  const sites = websiteTile(counts.websites)
  const resources = infraTile(counts.infra)
  const containers = dockerTile(counts.docker)
  const projectCount = projects.data?.items?.length

  const rows = attention.rows
  const shown = rows.slice(0, SHOWN)

  return (
    <>
      <PageHeader title="Overview" subtitle="Everything Watchly watches, and what needs a look." />

      <div className="overview-tiles">
        <Tile
          to="/websites"
          icon={<WebsiteIcon />}
          label="Websites"
          value={sites.value}
          note={sites.note}
          problems={sites.problems}
          empty={sites.empty}
        />
        {infra && (
          <Tile
            to="/infra"
            icon={<InfraIcon />}
            label="Infrastructure"
            value={resources.value}
            note={resources.note}
            problems={resources.problems}
            empty={resources.empty}
          />
        )}
        {docker && (
          <Tile
            to="/docker"
            icon={<DockerIcon />}
            label="Docker"
            value={containers.value}
            note={containers.note}
            problems={containers.problems}
            empty={containers.empty}
          />
        )}
        <Tile
          to="/projects"
          icon={<ProjectIcon />}
          label="Projects"
          value={projectCount}
          note={projectCount == null ? 'Loading…' : plural(projectCount, 'project', 'projects')}
          empty={projectCount === 0}
        />
      </div>

      <ErrorBanner error={attention.error ?? projects.error} />

      <section className="card overview-attention" aria-labelledby="needs-attention">
        <h2 id="needs-attention" className="overview-attention-title">
          Needs attention
          {rows.length > 0 && <span className="badge badge-down">{rows.length}</span>}
        </h2>
        {attention.loading && !rows.length ? (
          <p className="muted loading">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="muted overview-clear">All clear. Nothing is down or failing.</p>
        ) : (
          <ul className="overview-list">
            {shown.map((row) => (
              <li key={row.key}>
                <Link to={row.to} className="overview-row">
                  <span className={`badge badge-${row.badge}`}>{row.label}</span>
                  <span className="overview-row-main">
                    <strong>{row.name}</strong>
                    {row.detail && <span className="muted small">{row.detail}</span>}
                  </span>
                  <span className="chip">{row.source}</span>
                  <span className="muted small nowrap">{row.since ? timeAgo(row.since) : ''}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
        {rows.length > SHOWN && (
          <p className="muted small overview-more">
            And {rows.length - SHOWN} more. Open a monitor above to see them all.
          </p>
        )}
      </section>
    </>
  )
}
