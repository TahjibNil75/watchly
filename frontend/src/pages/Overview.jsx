import { Link, useOutletContext } from 'react-router-dom'
import { api } from '../api.js'
import { ErrorBanner, PageHeader } from '../components.jsx'
import { CONDITIONS, matchesFilter, useDockerEnabled } from '../docker.js'
import { DockerIcon } from '../Docker.jsx'
import { timeAgo } from '../format.js'
import { stateLabel, useInfraEnabled } from '../infra.js'
import { useApi } from '../useApi.js'

const POLL_MS = 30000

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

// Each monitor's total split into the states its page knows, good ones first
// and the worst last: [bar class, what to call it, how many].
const websiteSplit = (w) => [
  ['up', 'up', w.up],
  ['maintenance', 'in maintenance', w.maintenance],
  ['paused', 'paused', w.paused],
  ['unknown', 'not checked', w.unknown],
  ['down', 'down', w.down],
]
const infraSplit = (r) => [
  ['healthy', 'healthy', r.healthy],
  ['maintenance', 'in maintenance', r.maintenance],
  ['paused', 'paused', r.paused],
  ['unknown', 'not checked', r.unknown],
  ['missing', 'missing', r.missing],
  ['degraded', 'degraded', r.degraded],
  ['down', 'down', r.down],
]
// `running` counts the unhealthy ones too, so they come out of it.
const dockerSplit = (d) => [
  ['healthy', 'running', d.running - d.unhealthy],
  ['paused', 'stopped', d.stopped],
  ['degraded', 'unhealthy', d.unhealthy],
  ['down', 'down', d.down],
]

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
      source: 'websites',
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
      source: 'infra',
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
      source: 'docker',
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

const SHOWN = 5

// One monitor's lane: how many it has, how they split, and the ones that
// need a look, so a problem sits under the monitor it belongs to.
function Lane({ to, icon, label, counts, split, empty, rows, loading }) {
  const total = counts?.total
  const parts = counts && total > 0 ? split(counts).filter(([, , n]) => n > 0) : []
  const shown = rows.slice(0, SHOWN)
  return (
    <section className="card lane" aria-label={label}>
      <div className="lane-main">
        <Link to={to} className="lane-name">
          <span className="lane-total">{total ?? '–'}</span>
          <span>
            <strong>{label}</strong>
            <span className="lane-kind muted small">{icon}</span>
          </span>
        </Link>
        <div className="lane-bar">
          {!counts ? (
            <p className="muted small lane-note">Loading…</p>
          ) : total === 0 ? (
            <p className="muted small lane-note">{empty}</p>
          ) : (
            <>
              <span className="stat-bar" role="img" aria-label={parts.map(([, name, n]) => `${n} ${name}`).join(', ')}>
                {parts.map(([cls, , n]) => (
                  <i key={cls} className={`stat-bar-${cls}`} style={{ flexGrow: n }} />
                ))}
              </span>
              <ul className="lane-legend small">
                {parts.map(([cls, name, n]) => (
                  <li key={cls}>
                    <i className={`stat-bar-${cls}`} aria-hidden="true" />
                    {n} {name}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
        <Link to={to} className="lane-open small">
          Open
        </Link>
      </div>
      {total > 0 &&
        (rows.length > 0 ? (
          <>
            <ul className="overview-list lane-issues">
              {shown.map((row) => (
                <li key={row.key}>
                  <Link to={row.to} className="overview-row">
                    <span className={`badge badge-${row.badge}`}>{row.label}</span>
                    <span className="overview-row-main">
                      <strong>{row.name}</strong>
                      {row.detail && <span className="muted small">{row.detail}</span>}
                    </span>
                    <span className="muted small nowrap">{row.since ? timeAgo(row.since) : ''}</span>
                  </Link>
                </li>
              ))}
            </ul>
            {rows.length > SHOWN && (
              <p className="muted small overview-more lane-more">
                And {rows.length - SHOWN} more. <Link to={to}>Open {label}</Link> to see them all.
              </p>
            )}
          </>
        ) : (
          !loading && <p className="muted small lane-ok">Nothing needs attention.</p>
        ))}
    </section>
  )
}

export default function Overview() {
  const counts = useOutletContext()
  const infra = useInfraEnabled()
  const docker = useDockerEnabled()
  const attention = useAttention({ infra, docker })
  const rowsOf = (source) => attention.rows.filter((row) => row.source === source)

  return (
    <>
      <PageHeader title="Overview" subtitle="Everything Watchly watches, and what needs a look." />

      <ErrorBanner error={attention.error} />

      <div className="overview-lanes">
        <Lane
          to="/websites"
          icon={<WebsiteIcon />}
          label="Websites"
          counts={counts.websites}
          split={websiteSplit}
          empty="Nothing watched yet"
          rows={rowsOf('websites')}
          loading={attention.loading}
        />
        {infra && (
          <Lane
            to="/infra"
            icon={<InfraIcon />}
            label="Infrastructure"
            counts={counts.infra}
            split={infraSplit}
            empty="No resources yet"
            rows={rowsOf('infra')}
            loading={attention.loading}
          />
        )}
        {docker && (
          <Lane
            to="/docker"
            icon={<DockerIcon />}
            label="Docker"
            counts={counts.docker}
            split={dockerSplit}
            empty={counts.docker?.hosts ? 'No containers reported' : 'No hosts yet'}
            rows={rowsOf('docker')}
            loading={attention.loading}
          />
        )}
      </div>
    </>
  )
}
