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

// Which part of the bar a site belongs to. As on the dashboard, a site in
// maintenance counts there, not under its status.
function siteState(site) {
  if (!site.is_enabled) return 'paused'
  if (site.maintenance) return 'maintenance'
  return site.status
}

// The card's colour and its one-line verdict on the project's sites.
function health(project, c) {
  const up = c.up ?? 0
  if (!project.is_active) return { tone: 'paused', note: 'Inactive' }
  if (!c.total) return { tone: 'muted', note: 'No websites yet' }
  if (c.down) return { tone: 'down', note: `${c.down} down` }
  if (up === c.total) return { tone: 'up', note: 'All up' }
  return { tone: up ? 'up' : 'muted', note: `${up} of ${c.total} up` }
}

// One project: its initials in its state's colour, what its sites are doing,
// who's on it and where its alerts go. The whole card opens the project.
function ProjectCard({ project, counts }) {
  const c = counts ?? { total: 0 }
  const { tone, note } = health(project, c)
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
          {c.total} {c.total === 1 ? 'website' : 'websites'}
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

export default function Projects() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const creator = canCreateProjects(user)
  const [creating, setCreating] = useState(false)

  const projects = useApi(() => api.listProjects(), [])
  const sites = useApi(() => api.listWebsites(), [], { pollMs: 30000 })
  const users = useApi(() => (creating ? api.listUsers({ is_active: true }) : null), [creating])

  const counts = {}
  for (const site of sites.data?.items ?? []) {
    const c = (counts[site.project_id] ??= { total: 0 })
    const state = siteState(site)
    c.total++
    c[state] = (c[state] ?? 0) + 1
  }
  const items = projects.data?.items ?? []

  return (
    <>
      <PageHeader title="Projects" subtitle="A project groups one client's or product's websites and decides who hears about outages.">
        {creator && !creating && (
          <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
            New project
          </button>
        )}
      </PageHeader>

      {creating && (
        <section className="card">
          <h2>New project</h2>
          {users.loading ? (
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
          )}
        </section>
      )}

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
        <div className="project-grid">
          {items.map((p) => (
            <ProjectCard key={p.id} project={p} counts={counts[p.id]} />
          ))}
        </div>
      )}
    </>
  )
}
