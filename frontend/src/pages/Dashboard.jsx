import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { ErrorBanner, Loading, PageHeader, WebsiteTable } from '../components.jsx'
import { canCreateProjects, canViewAllProjects } from '../roles.js'
import { useApi } from '../useApi.js'

const FILTERS = [
  { key: 'all', label: 'All', match: () => true },
  { key: 'down', label: 'Down', match: (s) => s.is_enabled && s.status === 'down' },
  { key: 'up', label: 'Up', match: (s) => s.is_enabled && s.status === 'up' },
  { key: 'unknown', label: 'Pending', match: (s) => s.is_enabled && s.status === 'unknown' },
  { key: 'paused', label: 'Paused', match: (s) => !s.is_enabled },
]

export default function Dashboard() {
  const { user } = useAuth()
  const [filter, setFilter] = useState('all')
  const [projectId, setProjectId] = useState('')

  // One unfiltered request feeds both the counters and the table, so the
  // counters always describe everything, whatever filter is picked below.
  const sites = useApi(() => api.listWebsites(), [], { pollMs: 30000 })
  const projects = useApi(() => api.listProjects(), [])

  const projectItems = projects.data?.items ?? []
  const projectNames = Object.fromEntries(projectItems.map((p) => [p.id, p.name]))
  const all = sites.data?.items ?? []
  const inProject = projectId ? all.filter((s) => s.project_id === Number(projectId)) : all
  const active = FILTERS.find((f) => f.key === filter)
  const shown = inProject.filter(active.match)
  // Down sites first, then by name.
  shown.sort((a, b) => (b.status === 'down') - (a.status === 'down') || a.name.localeCompare(b.name))

  const creator = canCreateProjects(user)
  const emptyLabel = all.length ? (
    'No websites match this filter.'
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
      <PageHeader title="Websites" subtitle="Live status of every site you can see. Refreshes every 30 seconds.">
        {creator && (
          <Link to="/websites/new" className="btn btn-primary">
            Add website
          </Link>
        )}
      </PageHeader>

      <ErrorBanner error={sites.error} />

      <div className="stats">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            className={`stat stat-${f.key} ${filter === f.key ? 'is-active' : ''}`}
            onClick={() => setFilter(f.key)}
          >
            <span className="stat-value">
              {sites.data ? inProject.filter(f.match).length : '–'}
            </span>
            <span className="stat-label">{f.label}</span>
          </button>
        ))}
      </div>

      {projectItems.length > 1 && (
        <div className="toolbar">
          <label className="inline-field">
            <span>Project</span>
            <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">All projects</option>
              {projectItems.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}

      {sites.loading ? (
        <Loading />
      ) : (
        <WebsiteTable sites={shown} projectNames={projectNames} emptyLabel={emptyLabel} />
      )}

      {sites.data && sites.data.total > all.length && (
        <p className="muted small">
          Showing the first {all.length} of {sites.data.total} websites.
        </p>
      )}
    </>
  )
}
