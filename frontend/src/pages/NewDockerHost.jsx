import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { ConnectAgent, HostStatusBadge } from '../Docker.jsx'
import { useDockerEnabled } from '../docker.js'
import { canCreateProjects, canManageProject } from '../roles.js'
import { useApi } from '../useApi.js'

// Once the host exists: its token and how to run the agent, and its status,
// polled until the agent's first push turns it online.
function Waiting({ created }) {
  const host = useApi(() => api.getDockerHost(created.host.id), [created.host.id], { pollMs: 3000 })
  const h = host.data ?? created.host
  const online = h.status !== 'pending'
  return (
    <section className="card">
      <div className="section-head-row">
        <h2>Connect the agent to {h.name}</h2>
        <HostStatusBadge status={h.status} />
      </div>
      <ConnectAgent token={created.token} />
      <div className={`banner ${online ? 'banner-info' : 'banner-action'}`}>
        {online ? (
          <>
            The agent reported: {h.counts.total} containers on {h.hostname ?? h.name}.{' '}
            <Link to={`/docker/hosts/${h.id}`}>Open the host</Link>
          </>
        ) : (
          'Waiting for the agent’s first report… this page updates by itself.'
        )}
      </div>
    </section>
  )
}

export default function NewDockerHost() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const enabled = useDockerEnabled()
  const allowed = canCreateProjects(user)
  const projects = useApi(() => (allowed && enabled ? api.listProjects({ monitors: 'docker' }) : null), [allowed, enabled])
  const [form, setForm] = useState({ project_id: '', name: '', interval_seconds: 30, ignore_patterns: '' })
  const [created, setCreated] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  if (!allowed) return <Empty>Only admins, DevOps and project managers can add Docker hosts.</Empty>
  if (enabled === null || projects.loading) return <Loading />
  if (!enabled) return <Empty>Docker monitoring is off. Set <code>DOCKER_ENABLED=true</code> on the API.</Empty>

  const manageable = (projects.data?.items ?? []).filter((p) => canManageProject(user, p))
  const requested = Number(params.get('project'))
  const projectId = form.project_id || (manageable.some((p) => p.id === requested) ? requested : manageable[0]?.id)
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value })

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      setCreated(
        await api.createDockerHost({
          project_id: Number(projectId),
          name: form.name.trim(),
          interval_seconds: Number(form.interval_seconds),
          ignore_patterns: form.ignore_patterns
            .split(/[\s,]+/)
            .map((p) => p.trim())
            .filter(Boolean),
        }),
      )
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Add Docker host"
        subtitle="A machine running Docker. Watchly gives it a token; a small agent you run there reports its containers every few seconds, over outbound HTTPS only."
      />
      <ErrorBanner error={projects.error} />
      {created ? (
        <Waiting created={created} />
      ) : manageable.length === 0 ? (
        <Empty>
          Docker hosts live under a Docker project, and you don&apos;t manage one yet.{' '}
          <Link to="/projects">Create a project</Link> that monitors Docker first.
        </Empty>
      ) : (
        <form className="card" onSubmit={submit}>
          <label className="field">
            <span>Project</span>
            <select value={projectId} onChange={set('project_id')} required>
              {manageable.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Name</span>
            <input
              value={form.name}
              onChange={set('name')}
              placeholder="prod-docker-1"
              maxLength={255}
              required
              autoFocus
            />
          </label>
          <label className="field">
            <span>Heartbeat every (seconds)</span>
            <input type="number" min={10} max={300} value={form.interval_seconds} onChange={set('interval_seconds')} required />
            <span className="muted small">
              How often the agent reports resource use. Docker events (a crash, an OOM kill) are sent within seconds
              regardless. 30 is a good default.
            </span>
          </label>
          <label className="field">
            <span>Containers to skip (optional)</span>
            <input value={form.ignore_patterns} onChange={set('ignore_patterns')} placeholder="buildkit_*, *-tmp" />
            <span className="muted small">
              Name patterns, comma-separated. A container labelled <code>watchly.ignore=true</code> is skipped too.
            </span>
          </label>
          <ErrorBanner error={error} />
          <div className="form-actions">
            <button className="btn btn-primary" disabled={busy}>
              {busy ? 'Creating…' : 'Create host and get its token'}
            </button>
            <button type="button" className="btn btn-danger-solid" onClick={() => navigate(-1)}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </>
  )
}
