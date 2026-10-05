import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import ChannelLogo from '../ChannelLogo.jsx'
import { CHANNELS } from '../channels.js'
import { Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { initials } from '../format.js'
import ProjectForm from '../ProjectForm.jsx'
import { canCreateProjects, canViewAllProjects } from '../roles.js'
import { useApi } from '../useApi.js'

// A project's bar, in the dashboard's order.
const SPLIT = ['up', 'down', 'unknown', 'maintenance', 'paused']
const MAX_FACES = 4

// An infrastructure resource's state, in the bar's terms.
const RESOURCE_SPLIT = {
  healthy: 'up',
  degraded: 'up',
  down: 'down',
  unknown: 'unknown',
  missing: 'unknown',
  maintenance: 'maintenance',
  paused: 'paused',
}

// The two kinds of project, in the order their groups appear on the page.
const KINDS = [
  {
    monitors: 'websites',
    title: 'Websites',
    nouns: 'sites',
    icon: (
      <>
        <rect x="3" y="4" width="18" height="16" rx="2" />
        <path d="M3 9h18M6.5 6.5h.01M9.5 6.5h.01" />
      </>
    ),
  },
  {
    monitors: 'infrastructure',
    title: 'Infrastructure',
    nouns: 'resources',
    icon: (
      <>
        <rect x="3.5" y="4" width="17" height="6.5" rx="1.5" />
        <rect x="3.5" y="13.5" width="17" height="6.5" rx="1.5" />
        <path d="M7.5 7.25h.01M7.5 16.75h.01M11 7.25h5.5M11 16.75h5.5" />
      </>
    ),
  },
]
// Past this many projects the page offers a search.
const SEARCH_FROM = 6

// Which part of the bar a site belongs to. As on the dashboard, a site in
// maintenance counts there, not under its status.
function siteState(site) {
  if (!site.is_enabled) return 'paused'
  if (site.maintenance) return 'maintenance'
  return site.status
}

// The card's colour and its one-line verdict on the project's sites.
function health(project, c, nouns) {
  const up = c.up ?? 0
  if (!project.is_active) return { tone: 'paused', note: 'Inactive' }
  if (!c.total) return { tone: 'muted', note: `No ${nouns} yet` }
  if (c.down) return { tone: 'down', note: `${c.down} down` }
  if (up === c.total) return { tone: 'up', note: 'All up' }
  return { tone: up ? 'up' : 'muted', note: `${up} of ${c.total} up` }
}

// One project: its initials in its state's colour, what its sites are doing,
// who's on it and where its alerts go. The whole card opens the project.
function ProjectCard({ project, counts }) {
  const c = counts ?? { total: 0 }
  const infra = project.monitors === 'infrastructure'
  const [noun, nouns] = infra ? ['resource', 'resources'] : ['website', 'websites']
  const { tone, note } = health(project, c, nouns)
  const faces = project.members.slice(0, MAX_FACES)
  const channels = CHANNELS.filter((ch) => project.alert_channels.includes(ch.id))
  const via = channels.length ? `Alerts via ${channels.map((ch) => ch.name).join(', ')}` : ''
  const memberNames = project.members.map((m) => m.full_name || m.username).join(', ')

  return (
    <Link to={`/projects/${project.id}`} className={`project-card tone-${tone}`}>
      <span className="project-card-head">
        <span className={`project-mark${tone === 'down' ? ' is-alarm' : ''}`} aria-hidden="true">
          {initials(project.name)}
        </span>
        <span className="project-card-title">
          <span className="project-card-name">{project.name}</span>
          {project.description && <span className="project-card-desc">{project.description}</span>}
        </span>
        {!project.is_active && <span className="badge badge-paused">inactive</span>}
      </span>

      <span className="project-card-health">
        <span className="project-card-note">
          {c.down > 0 && project.is_active && <span className="stat-ping" />}
          {note}
        </span>
        <span className="muted small">
          {c.total} {c.total === 1 ? noun : nouns}
        </span>
      </span>
      <span className="project-bar" aria-hidden="true">
        {SPLIT.map(
          (s) => c[s] > 0 && <i key={s} className={`stat-bar-${s}`} style={{ flexGrow: c[s] }} />,
        )}
      </span>

      <span className="project-card-foot">
        <span className="project-people" title={memberNames || undefined}>
          {faces.length > 0 && (
            <span className="project-faces" aria-hidden="true">
              {faces.map((m) => (
                <span key={m.id} className="project-face">
                  {initials(m.full_name || m.username)}
                </span>
              ))}
              {project.members.length > MAX_FACES && (
                <span className="project-face is-more">+{project.members.length - MAX_FACES}</span>
              )}
            </span>
          )}
          <span className="muted small nowrap">
            {project.members.length} {project.members.length === 1 ? 'member' : 'members'}
          </span>
        </span>
        {via && (
          <span className="project-channels" title={via}>
            {channels.map((ch) => (
              <ChannelLogo key={ch.id} channel={ch.id} />
            ))}
            <span className="visually-hidden">{via}</span>
          </span>
        )}
      </span>
    </Link>
  )
}

// One kind's projects under a band that says how they are doing: its sites or
// resources up, down and in maintenance, summed over the group's projects.
// The band folds the group away.
function ProjectGroup({ kind, projects, counts, closed, onToggle }) {
  const sum = (state) => projects.reduce((n, p) => n + (counts[p.id]?.[state] ?? 0), 0)
  const watched = projects.reduce((n, p) => n + (counts[p.id]?.total ?? 0), 0)
  const id = `project-group-${kind.monitors}`
  return (
    <section className={`project-group is-${kind.monitors}${closed ? ' is-closed' : ''}`}>
      <h2 className="project-group-title">
        <button type="button" className="project-group-head" aria-expanded={!closed} aria-controls={id} onClick={onToggle}>
          <span className="project-group-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24">{kind.icon}</svg>
          </span>
          <span className="project-group-name">
            {kind.title}
            <span className="muted small">
              {projects.length} {projects.length === 1 ? 'project' : 'projects'} · {watched} {kind.nouns}
            </span>
          </span>
          <span className="project-group-sum">
            <span>
              <strong>{sum('up')}</strong>Up
            </span>
            <span className={sum('down') ? 'is-down' : undefined}>
              <strong>{sum('down')}</strong>Down
            </span>
            <span>
              <strong>{sum('maintenance')}</strong>Maintenance
            </span>
          </span>
          <svg className="project-group-chevron" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      </h2>
      <div id={id} className="project-grid" hidden={closed}>
        {projects.map((p) => (
          <ProjectCard key={p.id} project={p} counts={counts[p.id]} />
        ))}
      </div>
    </section>
  )
}

export default function Projects() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const creator = canCreateProjects(user)
  const [creating, setCreating] = useState(false)
  const [query, setQuery] = useState('')
  const [closedKinds, setClosedKinds] = useState([])

  const projects = useApi(() => api.listProjects(), [])
  const sites = useApi(() => api.listWebsites(), [], { pollMs: 30000 })
  // Answers 404 while infrastructure monitoring is off: no resources then.
  const resources = useApi(() => api.listResources({ limit: 200 }).catch(() => null), [], { pollMs: 30000 })
  const users = useApi(() => (creating ? api.listUsers({ is_active: true }) : null), [creating])

  const counts = {}
  for (const site of sites.data?.items ?? []) {
    const c = (counts[site.project_id] ??= { total: 0 })
    const state = siteState(site)
    c.total++
    c[state] = (c[state] ?? 0) + 1
  }
  for (const resource of resources.data?.items ?? []) {
    const c = (counts[resource.project.id] ??= { total: 0 })
    const state = RESOURCE_SPLIT[resource.state] ?? 'unknown'
    c.total++
    c[state] = (c[state] ?? 0) + 1
  }
  const items = projects.data?.items ?? []
  const needle = query.trim().toLowerCase()
  const shown = needle
    ? items.filter((p) => `${p.name} ${p.description ?? ''}`.toLowerCase().includes(needle))
    : items

  return (
    <>
      <PageHeader title={creating ? 'New project' : 'Projects'} subtitle="A project groups one client's or product's websites, or its AWS infrastructure, and decides who hears about outages.">
        {!creating && items.length >= SEARCH_FROM && (
          <input
            type="search"
            className="project-search"
            placeholder="Search projects"
            aria-label="Search projects"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        )}
        {creator && !creating && (
          <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
            New project
          </button>
        )}
      </PageHeader>

      {creating ? (
        users.loading ? (
          <Loading />
        ) : (
          <ProjectForm
            users={users.data?.items ?? []}
            onCancel={() => setCreating(false)}
            onSubmit={async (payload) => {
              const project = await api.createProject(payload)
              navigate(`/projects/${project.id}`)
            }}
          />
        )
      ) : (
        <>
          <ErrorBanner error={projects.error} />
          {projects.loading ? (
            <Loading />
          ) : items.length === 0 ? (
            <Empty>
              <svg
                className="empty-icon"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
              </svg>
              {canViewAllProjects(user)
                ? 'No projects yet. Create one to start monitoring websites.'
                : creator
                  ? "You don't own or belong to any project yet. Create one, or ask an admin to add you to one."
                  : "You aren't a member of any project yet. Ask an admin to add you to one."}
            </Empty>
          ) : (
            shown.length === 0 ? (
              <Empty>No project matches &ldquo;{query.trim()}&rdquo;.</Empty>
            ) : (
              KINDS.map((kind) => {
                const group = shown.filter((p) => (p.monitors ?? 'websites') === kind.monitors)
                if (!group.length) return null
                const closed = closedKinds.includes(kind.monitors)
                return (
                  <ProjectGroup
                    key={kind.monitors}
                    kind={kind}
                    projects={group}
                    counts={counts}
                    closed={closed}
                    onToggle={() =>
                      setClosedKinds(closed ? closedKinds.filter((k) => k !== kind.monitors) : [...closedKinds, kind.monitors])
                    }
                  />
                )
              })
            )
          )}
        </>
      )}
    </>
  )
}
