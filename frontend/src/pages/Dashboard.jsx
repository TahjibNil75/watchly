import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { CHECK_TYPES } from '../checkTypes.js'
import { ErrorBanner, Loading, PageHeader, WebsiteTable } from '../components.jsx'
import { canCreateProjects, canViewAllProjects } from '../roles.js'
import { useApi } from '../useApi.js'
import { useMediaQuery } from '../useMediaQuery.js'

const PAGE_SIZE = 50

// `count` reads the summary; `query` is what the list asks the server for. A
// site in maintenance counts there, not under its status.
const live = { is_enabled: true, in_maintenance: false }
const FILTERS = [
  { key: 'all', label: 'All websites', count: 'total', query: {} },
  { key: 'down', label: 'Down', count: 'down', query: { status: 'down', ...live } },
  { key: 'up', label: 'Up', count: 'up', query: { status: 'up', ...live } },
  { key: 'unknown', label: 'Pending', count: 'unknown', query: { status: 'unknown', ...live } },
  {
    key: 'maintenance',
    label: 'Maintenance',
    count: 'maintenance',
    query: { is_enabled: true, in_maintenance: true },
  },
  { key: 'paused', label: 'Paused', count: 'paused', query: { is_enabled: false } },
]

// 24x24 stroke icons for the cards, drawn like the sidebar's.
const STAT_ICONS = {
  all: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
    </>
  ),
  down: <path d="M12 3.5 2.5 20h19zM12 10v4M12 17v.01" />,
  up: <path d="M3 12h4l2.5-6 5 12 2.5-6h4" />,
  unknown: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  maintenance: (
    <>
      <path d="M10 3.5h4l3.6 13.5H6.4zM8.1 10.5h7.8" />
      <rect x="3.5" y="17" width="17" height="3.5" rx="1" />
    </>
  ),
  paused: <path d="M9 5v14M15 5v14" />,
}

// The total's bar, in this order.
const SPLIT = ['up', 'down', 'unknown', 'maintenance', 'paused']

function statNote(key, n, counts) {
  const share = (x) => `${Math.round((x / counts.total) * 100)}%`
  switch (key) {
    case 'all':
      return counts.total ? `${counts.up} of ${counts.total} up` : 'Nothing watched yet'
    case 'down':
      return n ? 'Needs attention' : 'All clear'
    case 'up':
      return counts.total ? `${share(n)} of sites` : 'None yet'
    case 'unknown':
      return 'Not checked yet'
    case 'maintenance':
      return 'Checks on hold'
    default:
      return 'Checks off'
  }
}

// What the last refresh changed: each count's step from the poll before it.
// A new project, type or search starts over rather than passing its
// difference off as a change. `round` counts the refreshes, so each one can
// replay its animation.
function useRefreshChanges(counts, scope) {
  const [seen, setSeen] = useState({ counts: null, scope: null, deltas: {}, round: 0 })
  if (counts && counts !== seen.counts) {
    const same = seen.counts && seen.scope === scope
    setSeen({
      counts,
      scope,
      deltas: same
        ? Object.fromEntries(FILTERS.map((f) => [f.key, counts[f.count] - seen.counts[f.count]]))
        : {},
      round: seen.round + 1,
    })
  }
  return seen
}

// Runs a number from `from` to `to`; mount it afresh for each change.
function CountUp({ from, to }) {
  const [shown, setShown] = useState(from)
  useEffect(() => {
    const start = performance.now()
    let frame = requestAnimationFrame(function step(now) {
      const t = Math.min((now - start) / 700, 1)
      setShown(Math.round(from + (to - from) * (1 - (1 - t) ** 3)))
      if (t < 1) frame = requestAnimationFrame(step)
    })
    return () => cancelAnimationFrame(frame)
  }, [from, to])
  return shown
}

// One status card, which filters the table below, in its state's colour
// whatever the count. Down gets louder while anything is down. A refresh that
// changes the count runs the number to its new value, rings the card once and
// leaves a +1 or −1 until the next refresh.
function StatCard({ filter, counts, change, round, still, active, onPick }) {
  const { key, label } = filter
  const n = counts?.[filter.count]
  const loaded = n !== undefined
  const alarm = key === 'down' && n > 0
  const better = key === 'down' ? change < 0 : key === 'up' ? change > 0 : null
  const className = [
    'stat',
    `stat-${key}`,
    active && 'is-active',
    alarm && 'is-alarm',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <button type="button" className={className} onClick={onPick} aria-pressed={active}>
      <span className="stat-label">
        {alarm && <span className="stat-ping" />}
        {label}
      </span>
      <span className="stat-main">
        <span className="stat-value">
          {!loaded ? '–' : change && !still ? <CountUp key={round} from={n - change} to={n} /> : n}
        </span>
        {change !== 0 && (
          <span
            className={`stat-delta ${better === true ? 'is-good' : better === false ? 'is-bad' : ''}`}
            title={`${Math.abs(change)} ${change > 0 ? 'more' : 'fewer'} since the last refresh`}
          >
            {change > 0 ? `+${change}` : `−${-change}`}
          </span>
        )}
        <svg
          className="stat-icon"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.9"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          {STAT_ICONS[key]}
        </svg>
      </span>
      <span className="stat-note">{loaded ? statNote(key, n, counts) : '\u00a0'}</span>
      {key === 'all' && counts?.total > 0 && (
        <span className="stat-bar" aria-hidden="true">
          {SPLIT.map(
            (s) => counts[s] > 0 && <i key={s} className={`stat-bar-${s}`} style={{ flexGrow: counts[s] }} />,
          )}
        </span>
      )}
      {change !== 0 && !still && <span key={round} className="stat-flash" aria-hidden="true" />}
    </button>
  )
}

export default function Dashboard() {
  const { user } = useAuth()
  const [filter, setFilter] = useState('all')
  const [projectId, setProjectId] = useState('')
  const [checkType, setCheckType] = useState('')
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(0)

  // Search once typing pauses, not on every keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(search.trim())
      setPage(0)
    }, 300)
    return () => clearTimeout(timer)
  }, [search])

  const active = FILTERS.find((f) => f.key === filter)
  const scope = { project_id: projectId, check_type: checkType, q }
  // The counters cover every state within the project, type and search,
  // whichever one the table below is showing.
  const summary = useApi(() => api.websiteSummary(scope), [projectId, checkType, q], {
    pollMs: 30000,
  })
  const sites = useApi(
    () =>
      api.listWebsites({
        ...scope,
        ...active.query,
        sort: 'status',
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    [filter, projectId, checkType, q, page],
    { pollMs: 30000 },
  )
  const projects = useApi(() => api.listProjects(), [])
  const changes = useRefreshChanges(summary.data, JSON.stringify(scope))
  const still = useMediaQuery('(prefers-reduced-motion: reduce)')

  const total = sites.data?.total ?? 0
  const lastPage = Math.max(Math.ceil(total / PAGE_SIZE) - 1, 0)
  // Sites deleted or recovered since can leave the page past the end.
  if (sites.data && page > lastPage) setPage(lastPage)

  const pick = (setter) => (value) => {
    setter(value)
    setPage(0)
  }

  const projectItems = projects.data?.items ?? []
  const projectNames = Object.fromEntries(projectItems.map((p) => [p.id, p.name]))

  const creator = canCreateProjects(user)
  const nothingYet = summary.data?.total === 0 && !q && !projectId && !checkType
  const emptyLabel = !nothingYet ? (
    'No websites match this search and filter.'
  ) : canViewAllProjects(user) ? (
    <>
      Nothing is being monitored yet. <Link to="/projects">Create a project</Link>, then add a
      website to it.
    </>
  ) : creator ? (
    <>
      You don't have any websites yet. <Link to="/projects">Create a project</Link>, or ask an
      admin to add you to one.
    </>
  ) : (
    "You don't have any websites yet. Ask an admin to add you to a project."
  )

  return (
    <>
      <PageHeader title="Websites">
        {creator && (
          <Link to="/websites/new" className="btn btn-primary">
            Add website
          </Link>
        )}
      </PageHeader>

      <ErrorBanner error={sites.error ?? summary.error} />

      <div className="stats-wrap">
        <div className="stats">
          {FILTERS.map((f) => (
            <StatCard
              key={f.key}
              filter={f}
              counts={summary.data}
              change={changes.deltas[f.key] ?? 0}
              round={changes.round}
              still={still}
              active={filter === f.key}
              onPick={() => pick(setFilter)(f.key)}
            />
          ))}
        </div>
      </div>

      <div className="toolbar">
        <label className="inline-field">
          <span>Search</span>
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Name, URL or host"
            maxLength={200}
          />
        </label>
        <label className="inline-field">
          <span>Type</span>
          <select value={checkType} onChange={(e) => pick(setCheckType)(e.target.value)}>
            <option value="">All types</option>
            {CHECK_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
        {projectItems.length > 1 && (
          <label className="inline-field">
            <span>Project</span>
            <select value={projectId} onChange={(e) => pick(setProjectId)(e.target.value)}>
              <option value="">All projects</option>
              {projectItems.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {sites.loading ? (
        <Loading />
      ) : (
        <WebsiteTable
          sites={sites.data?.items ?? []}
          projectNames={projectNames}
          emptyLabel={emptyLabel}
        />
      )}

      {total > PAGE_SIZE && (
        <div className="pager">
          <span className="muted small">
            {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total}
          </span>
          <button type="button" className="btn btn-sm" onClick={() => setPage(page - 1)} disabled={page === 0}>
            Previous
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setPage(page + 1)}
            disabled={page >= lastPage}
          >
            Next
          </button>
        </div>
      )}
    </>
  )
}
