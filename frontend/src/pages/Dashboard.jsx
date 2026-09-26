import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { CHECK_TYPES } from '../checkTypes.js'
import { ErrorBanner, Loading, PageHeader, WebsiteTable } from '../components.jsx'
import { canCreateProjects, canViewAllProjects } from '../roles.js'
import { useApi } from '../useApi.js'

const PAGE_SIZE = 50

// `count` reads the summary; `query` is what the list asks the server for.
const FILTERS = [
  { key: 'all', label: 'All', count: 'total', query: {} },
  { key: 'down', label: 'Down', count: 'down', query: { status: 'down', is_enabled: true } },
  { key: 'up', label: 'Up', count: 'up', query: { status: 'up', is_enabled: true } },
  { key: 'unknown', label: 'Pending', count: 'unknown', query: { status: 'unknown', is_enabled: true } },
  { key: 'paused', label: 'Paused', count: 'paused', query: { is_enabled: false } },
]

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
      <PageHeader
        title="Websites"
        subtitle="Live status of every site, pinged host and DNS record you can see. Refreshes every 30 seconds."
      >
        {creator && (
          <Link to="/websites/new" className="btn btn-primary">
            Add website
          </Link>
        )}
      </PageHeader>

      <ErrorBanner error={sites.error ?? summary.error} />

      <div className="stats">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            className={`stat stat-${f.key} ${filter === f.key ? 'is-active' : ''}`}
            onClick={() => pick(setFilter)(f.key)}
          >
            <span className="stat-value">{summary.data ? summary.data[f.count] : '–'}</span>
            <span className="stat-label">{f.label}</span>
          </button>
        ))}
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
