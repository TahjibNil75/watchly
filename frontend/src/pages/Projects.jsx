import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import ProjectForm from '../ProjectForm.jsx'
import { canCreateProjects, canViewAllProjects } from '../roles.js'
import { useApi } from '../useApi.js'

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
    const c = (counts[site.project_id] ??= { total: 0, down: 0 })
    c.total++
    if (site.is_enabled && site.status === 'down') c.down++
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
          {canViewAllProjects(user)
            ? 'No projects yet. Create one to start monitoring websites.'
            : creator
              ? "You don't own or belong to any project yet. Create one, or ask an admin to add you to one."
              : "You aren't a member of any project yet. Ask an admin to add you to one."}
        </Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Project</th>
                <th>Websites</th>
                <th>Members</th>
                <th>Alerts via</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => {
                const c = counts[p.id] ?? { total: 0, down: 0 }
                return (
                  <tr key={p.id}>
                    <td>
                      <Link to={`/projects/${p.id}`} className="strong-link">
                        {p.name}
                      </Link>
                      {p.description && <div className="muted small">{p.description}</div>}
                    </td>
                    <td className="nowrap">
                      {c.total}
                      {c.down > 0 && <span className="text-down"> · {c.down} down</span>}
                    </td>
                    <td>{p.members.length}</td>
                    <td>
                      {p.alert_channels.map((ch) => (
                        <span key={ch} className="chip">
                          {ch}
                        </span>
                      ))}
                    </td>
                    <td>
                      {p.is_active ? (
                        <span className="badge badge-up">active</span>
                      ) : (
                        <span className="badge badge-paused">inactive</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
