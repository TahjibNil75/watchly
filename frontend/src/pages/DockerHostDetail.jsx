import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { ConfirmDialog, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { ConnectAgent, ContainerTable, EventFeed, HostStatusBadge } from '../Docker.jsx'
import { memory } from '../docker.js'
import { dateTime, duration, since, timeAgo } from '../format.js'
import { useApi } from '../useApi.js'

function Fact({ label, children }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{children ?? '—'}</strong>
    </div>
  )
}

function SettingsForm({ host, onSaved, onCancel }) {
  const [form, setForm] = useState({
    name: host.name,
    interval_seconds: host.interval_seconds,
    ignore_patterns: host.ignore_patterns.join(', '),
  })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value })
  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onSaved(
        await api.updateDockerHost(host.id, {
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
    <form className="card" onSubmit={submit}>
      <h2>Settings</h2>
      <label className="field">
        <span>Name</span>
        <input value={form.name} onChange={set('name')} maxLength={255} required />
      </label>
      <label className="field">
        <span>Heartbeat every (seconds)</span>
        <input type="number" min={10} max={300} value={form.interval_seconds} onChange={set('interval_seconds')} required />
      </label>
      <label className="field">
        <span>Containers to skip</span>
        <input value={form.ignore_patterns} onChange={set('ignore_patterns')} placeholder="buildkit_*, *-tmp" />
        <span className="muted small">The agent picks both up with its next report; no restart needed.</span>
      </label>
      <ErrorBanner error={error} />
      <div className="form-actions">
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Saving…' : 'Save'}
        </button>
        <button type="button" className="btn btn-danger-solid" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  )
}

export default function DockerHostDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [token, setToken] = useState(null)
  const [confirm, setConfirm] = useState(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [showRemoved, setShowRemoved] = useState(false)

  const host = useApi(() => api.getDockerHost(id), [id], { pollMs: 15000 })
  const containers = useApi(
    () => api.listContainers({ host_id: id, include_removed: showRemoved || undefined }),
    [id, showRemoved],
    { pollMs: 15000 },
  )
  const events = useApi(() => api.dockerEvents({ host_id: id, limit: 40 }), [id], { pollMs: 30000 })

  if (host.loading) return <Loading />
  if (!host.data) {
    return (
      <>
        <ErrorBanner error={host.error} />
        <Link to="/docker">← Back to Docker</Link>
      </>
    )
  }
  const h = host.data

  async function act(action) {
    setBusy(true)
    setActionError(null)
    try {
      if (action === 'rotate') {
        const rotated = await api.rotateDockerToken(h.id)
        setToken(rotated.token)
        host.setData({ ...h, token_hint: rotated.token_hint })
      } else {
        await api.deleteDockerHost(h.id)
        navigate('/docker')
        return
      }
      setConfirm(null)
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <p className="crumbs">
        <Link to="/docker">Docker</Link> / <Link to={`/projects/${h.project.id}`}>{h.project.name}</Link>
      </p>
      <PageHeader
        title={
          <>
            {h.name} <HostStatusBadge status={h.status} />
          </>
        }
        subtitle={h.description}
      >
        {h.can_manage && (
          <>
            <button type="button" className="btn btn-warn" onClick={() => setEditing(!editing)}>
              {editing ? 'Close settings' : 'Settings'}
            </button>
            <button type="button" className="btn" onClick={() => setConfirm('rotate')} disabled={busy}>
              New token
            </button>
            <button type="button" className="btn btn-danger-solid" onClick={() => setConfirm('delete')} disabled={busy}>
              Delete
            </button>
          </>
        )}
      </PageHeader>

      <ErrorBanner error={actionError ?? host.error} />
      {confirm === 'rotate' && (
        <ConfirmDialog
          title={`New agent token for ${h.name}?`}
          word="rotate"
          confirmLabel="Make new token"
          busy={busy}
          error={actionError}
          onConfirm={() => act('rotate')}
          onCancel={() => setConfirm(null)}
        >
          The current token stops working at once: the agent on the host reports nothing until it runs with the new one.
        </ConfirmDialog>
      )}
      {confirm === 'delete' && (
        <ConfirmDialog
          title={`Delete ${h.name}?`}
          busy={busy}
          error={actionError}
          onConfirm={() => act('delete')}
          onCancel={() => setConfirm(null)}
        >
          Deletes the host, its {h.counts.total} containers and their history, and its agent&apos;s token stops working.
          The agent itself keeps running on the host until you remove it. This can&apos;t be undone.
        </ConfirmDialog>
      )}

      {token && (
        <section className="card">
          <h2>New agent token</h2>
          <ConnectAgent token={token} />
        </section>
      )}
      {h.status === 'pending' && !token && (
        <div className="banner banner-action">
          Its agent hasn&apos;t reported yet. Run it on the host with the token you were given
          {h.can_manage ? ', or make a new token if that one is lost' : ''}.
        </div>
      )}
      {h.status === 'offline' && (
        <div className="banner banner-error" role="alert">
          Its agent stopped reporting {h.down_since ? `${since(h.down_since)} ago` : ''}: last heard from{' '}
          {dateTime(h.last_seen_at)}. Its containers&apos; states below are as they were then.
        </div>
      )}
      {h.status === 'docker_down' && (
        <div className="banner banner-error" role="alert">
          The agent reports, but cannot reach Docker on the host: {h.docker_error}
        </div>
      )}

      {editing && (
        <SettingsForm
          host={h}
          onCancel={() => setEditing(false)}
          onSaved={(saved) => {
            host.setData(saved)
            setEditing(false)
          }}
        />
      )}

      <section className="card">
        <div className="kv kv-inline">
          <Fact label="Hostname">{h.hostname}</Fact>
          <Fact label="OS">{h.os}</Fact>
          <Fact label="Docker">{h.docker_version}</Fact>
          <Fact label="CPUs">{h.cpus}</Fact>
          <Fact label="Memory">{h.mem_total_bytes ? memory(h.mem_total_bytes) : null}</Fact>
          <Fact label="Last report">{h.last_seen_at ? timeAgo(h.last_seen_at) : 'never'}</Fact>
          <Fact label="Heartbeat">every {duration(h.interval_seconds)}</Fact>
          <Fact label="Agent">
            {h.agent_version ? `${h.agent_version}${h.agent_mem_bytes ? ` · ${memory(h.agent_mem_bytes)}` : ''}` : null}
          </Fact>
          <Fact label="Token">
            <code>{h.token_hint}</code>
          </Fact>
        </div>
        {(h.ignore_patterns.length > 0 || h.dropped_events > 0) && (
          <p className="muted small">
            {h.ignore_patterns.length > 0 && <>Skips containers named {h.ignore_patterns.join(', ')}. </>}
            {h.dropped_events > 0 &&
              `The agent had to drop ${h.dropped_events} Docker events while Watchly was out of reach.`}
          </p>
        )}
      </section>

      <section className="section">
        <div className="section-head-row">
          <h2>Containers</h2>
          <label className="muted small">
            <input type="checkbox" checked={showRemoved} onChange={(e) => setShowRemoved(e.target.checked)} /> Show
            removed
          </label>
        </div>
        <ErrorBanner error={containers.error} />
        {containers.loading ? (
          <Loading />
        ) : (
          <ContainerTable
            containers={containers.data?.items ?? []}
            hosts={{ [h.id]: h }}
            showHost={false}
            emptyLabel={h.status === 'pending' ? 'Nothing reported yet.' : 'No containers on this host.'}
          />
        )}
      </section>

      <section className="section">
        <h2>Activity</h2>
        <p className="muted small">Docker&apos;s events as the agent reported them, and the alerts Watchly sent.</p>
        <EventFeed events={events.data?.items ?? []} />
      </section>
    </>
  )
}
