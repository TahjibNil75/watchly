import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import {
  ErrorBanner,
  Loading,
  PageHeader,
  PersonList,
  UserChecklist,
  WebsiteTable,
} from '../components.jsx'
import { dateTime, parseEmails } from '../format.js'
import NotificationSettings from '../NotificationSettings.jsx'
import ProjectForm from '../ProjectForm.jsx'
import { canManageProject } from '../roles.js'
import { useApi } from '../useApi.js'

export default function ProjectDetail() {
  const { id } = useParams()
  const { user } = useAuth()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [adding, setAdding] = useState([])
  const [newEmails, setNewEmails] = useState('')
  const [actionError, setActionError] = useState(null)
  const [reportResult, setReportResult] = useState(null)
  const [busy, setBusy] = useState(false)

  const project = useApi(() => api.getProject(id), [id])
  const sites = useApi(() => api.listWebsites({ project_id: id }), [id], { pollMs: 30000 })
  const canManage = canManageProject(user, project.data)
  const users = useApi(() => (canManage ? api.listUsers({ is_active: true }) : null), [canManage])

  if (project.loading) return <Loading />
  if (!project.data) {
    return (
      <>
        <ErrorBanner error={project.error} />
        <Link to="/projects">← Back to projects</Link>
      </>
    )
  }

  const p = project.data

  async function run(action) {
    setBusy(true)
    setActionError(null)
    try {
      await action()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(false)
    }
  }

  const remove = () => {
    const n = sites.data?.total ?? 0
    const warning = n ? ` This also stops monitoring its ${n} website(s) and deletes their history.` : ''
    if (!window.confirm(`Delete project "${p.name}"?${warning}`)) return
    run(async () => {
      await api.deleteProject(p.id)
      navigate('/projects')
    })
  }

  const removeMember = (member) =>
    run(async () => project.setData(await api.removeMember(p.id, member.id)))

  const addMembers = () =>
    run(async () => {
      project.setData(await api.addMembers(p.id, adding))
      setAdding([])
    })

  // The API replaces extra_emails wholesale, so both of these send the full list.
  const addEmails = (event) => {
    event.preventDefault()
    const seen = new Set(p.extra_emails.map((e) => e.toLowerCase()))
    const fresh = parseEmails(newEmails).filter((e) => {
      const key = e.toLowerCase()
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    if (!fresh.length) {
      setNewEmails('')
      return
    }
    run(async () => {
      project.setData(await api.updateProject(p.id, { extra_emails: [...p.extra_emails, ...fresh] }))
      setNewEmails('')
    })
  }

  const removeEmail = (email) =>
    run(async () =>
      project.setData(
        await api.updateProject(p.id, { extra_emails: p.extra_emails.filter((e) => e !== email) }),
      ),
    )

  const sendReport = () =>
    run(async () => {
      setReportResult(null)
      setReportResult(await api.sendReport(p.id))
    })

  const memberIds = new Set(p.members.map((m) => m.id))
  const candidates = (users.data?.items ?? []).filter((u) => !memberIds.has(u.id))
  const owner =
    p.owner_id === user.id ? 'you' : users.data?.items.find((u) => u.id === p.owner_id)?.username

  return (
    <>
      <p className="crumbs">
        <Link to="/projects">Projects</Link>
      </p>
      <PageHeader
        title={
          <>
            {p.name}{' '}
            {!p.is_active && <span className="badge badge-paused">inactive</span>}
          </>
        }
        subtitle={p.description}
      >
        {canManage && (
          <>
            <Link to={`/websites/new?project=${p.id}`} className="btn btn-primary">
              Add website
            </Link>
            <button type="button" className="btn" onClick={() => setEditing(!editing)}>
              {editing ? 'Close editor' : 'Edit'}
            </button>
            <button type="button" className="btn btn-danger" onClick={remove} disabled={busy}>
              Delete
            </button>
          </>
        )}
      </PageHeader>

      <ErrorBanner error={actionError} />
      {reportResult &&
        (reportResult.delivered_by.length ? (
          <div className="banner banner-info">
            The {reportResult.month} report ({reportResult.sites} website
            {reportResult.sites === 1 ? '' : 's'}) was sent by {reportResult.delivered_by.join(' and ')}.
          </div>
        ) : (
          <div className="banner banner-error" role="alert">
            The {reportResult.month} report was built but not delivered. Monthly reports may be
            switched off for this project, or email/Slack is not set up — see the API log.
          </div>
        ))}

      {editing && (
        <section className="card">
          <h2>Edit project</h2>
          <ProjectForm
            initial={p}
            onCancel={() => setEditing(false)}
            onSubmit={async (payload) => {
              project.setData(await api.updateProject(p.id, payload))
              setEditing(false)
            }}
          />
        </section>
      )}

      <section className="section">
        <h2>Websites</h2>
        <ErrorBanner error={sites.error} />
        {sites.loading ? (
          <Loading />
        ) : (
          <WebsiteTable
            sites={sites.data?.items ?? []}
            emptyLabel={
              canManage ? (
                <>
                  No websites in this project yet.{' '}
                  <Link to={`/websites/new?project=${p.id}`}>Add the first one</Link>.
                </>
              ) : (
                'No websites in this project yet.'
              )
            }
          />
        )}
      </section>

      <div className="grid-2">
        <section className="card">
          <h2>Members</h2>
          <p className="muted small">Members are emailed about every site in this project.</p>
          <PersonList
            people={p.members}
            onRemove={canManage ? removeMember : null}
            emptyLabel="No members yet."
          />
          {canManage && candidates.length > 0 && (
            <details className="advanced">
              <summary>Add members</summary>
              <UserChecklist users={candidates} selected={adding} onChange={setAdding} />
              <button
                type="button"
                className="btn btn-sm"
                onClick={addMembers}
                disabled={busy || !adding.length}
              >
                Add {adding.length || ''} selected
              </button>
            </details>
          )}

          <h3>Extra addresses</h3>
          <p className="muted small">
            Also emailed about every site, without needing an account — a client contact or a
            shared inbox.
          </p>
          {p.extra_emails.length ? (
            <ul className="people">
              {p.extra_emails.map((email) => (
                <li key={email}>
                  <span>{email}</span>
                  {canManage && (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => removeEmail(email)}
                      disabled={busy}
                    >
                      Remove
                    </button>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">No extra addresses.</p>
          )}
          {canManage && (
            <form className="add-row" onSubmit={addEmails}>
              <input
                value={newEmails}
                onChange={(e) => setNewEmails(e.target.value)}
                placeholder="name@example.com"
                aria-label="Email addresses to add, separated by commas"
              />
              <button className="btn btn-sm" disabled={busy || !newEmails.trim()}>
                Add
              </button>
            </form>
          )}
        </section>

        <section className="card">
          <h2>Alerting</h2>
          <dl className="kv">
            <div>
              <dt>Channels</dt>
              <dd>
                {p.alert_channels.map((c) => (
                  <span key={c} className="chip">
                    {c}
                  </span>
                ))}
              </dd>
            </div>
            <div>
              <dt>Slack</dt>
              <dd>
                {p.slack_configured
                  ? `${p.slack_channel_id} (${p.slack_token_hint})`
                  : p.slack_token_hint
                    ? 'muted'
                    : 'not set up'}
              </dd>
            </div>
            {owner && (
              <div>
                <dt>Owner</dt>
                <dd>{owner}</dd>
              </div>
            )}
            <div>
              <dt>Created</dt>
              <dd>{dateTime(p.created_at)}</dd>
            </div>
          </dl>
        </section>
      </div>

      <section className="section">
        <div className="section-head">
          <div>
            <h2>Notifications</h2>
            <p className="muted small">
              Choose which messages this project sends, on which channel, and how they read.
              Anything left alone follows the global settings.
            </p>
          </div>
          {canManage && (
            <button type="button" className="btn btn-sm" onClick={sendReport} disabled={busy}>
              Send last month&apos;s report now
            </button>
          )}
        </div>
        <NotificationSettings projectId={p.id} canEdit={canManage} channels={p.alert_channels} />
      </section>
    </>
  )
}
