import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { AwsAccountsCard } from '../AwsAccounts.jsx'
import DeploymentsSection from '../Deployments.jsx'
import {
  ConfirmDialog,
  ErrorBanner,
  Loading,
  PageHeader,
  PersonList,
  UserChecklist,
  WebsiteTable,
} from '../components.jsx'
import { ContainerTable, HostStatusBadge } from '../Docker.jsx'
import { saveFile } from '../download.js'
import { dateTime, parseEmails, previousMonthUtc } from '../format.js'
import { ResourceTable } from '../Infra.jsx'
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
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  const project = useApi(() => api.getProject(id), [id])
  const infra = project.data?.monitors === 'infrastructure'
  const docker = project.data?.monitors === 'docker'
  const websites = project.data && !infra && !docker
  const sites = useApi(
    () => (websites ? api.listWebsites({ project_id: id }) : null),
    [id, websites],
    { pollMs: 30000 },
  )
  const hosts = useApi(() => (docker ? api.listDockerHosts({ project_id: id }) : null), [id, docker], {
    pollMs: 30000,
  })
  const containers = useApi(() => (docker ? api.listContainers({ project_id: id }) : null), [id, docker], {
    pollMs: 30000,
  })
  const resources = useApi(
    () => (infra ? api.listResources({ project_id: id, sort: 'name', limit: 200 }) : null),
    [id, infra],
    { pollMs: 30000 },
  )
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

  const downloadReport = () =>
    run(async () => {
      const month = previousMonthUtc()
      saveFile(`watchly-${p.name.replace(/\W+/g, '-')}-report-${month}.csv`, await api.projectReportCsv(p.id, month))
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
            {infra && <span className="chip">infrastructure</span>}
            {docker && <span className="chip">docker</span>}
            {!p.is_active && <span className="badge badge-paused">inactive</span>}
          </>
        }
        subtitle={p.description}
      >
        {canManage && (
          <>
            {infra ? (
              <Link to={`/infra/new?project=${p.id}`} className="btn btn-primary">
                Add resources
              </Link>
            ) : docker ? (
              <Link to={`/docker/new?project=${p.id}`} className="btn btn-primary">
                Add Docker host
              </Link>
            ) : (
              <Link to={`/websites/new?project=${p.id}`} className="btn btn-primary">
                Add website
              </Link>
            )}
            <button type="button" className="btn btn-warn" onClick={() => setEditing(!editing)}>
              {editing ? 'Close editor' : 'Edit'}
            </button>
            <button
              type="button"
              className="btn btn-danger-solid"
              onClick={() => setConfirmingDelete(true)}
              disabled={busy}
            >
              Delete
            </button>
          </>
        )}
      </PageHeader>

      <ErrorBanner error={actionError} />
      {confirmingDelete && (
        <ConfirmDialog
          title={`Delete project ${p.name}?`}
          busy={busy}
          error={actionError}
          onConfirm={remove}
          onCancel={() => setConfirmingDelete(false)}
        >
          Deletes this project.
          {(sites.data?.total ?? 0) > 0 &&
            ` This also stops monitoring its ${sites.data.total} website(s) and deletes their history.`}
          {infra &&
            ` This also deletes its AWS accounts and stops monitoring its ${resources.data?.total ?? 0} resource(s), deleting their history.`}
          {docker &&
            ` This also deletes its ${hosts.data?.items.length ?? 0} Docker host(s) and their containers' history; their agents' tokens stop working.`}{' '}
          This can&apos;t be undone.
        </ConfirmDialog>
      )}
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

      {docker ? (
        <>
          <section className="section">
            <h2>Docker hosts</h2>
            <ErrorBanner error={hosts.error} />
            {hosts.loading ? (
              <Loading />
            ) : (hosts.data?.items ?? []).length === 0 ? (
              <p className="muted small">
                No hosts yet.{' '}
                {canManage && (
                  <>
                    <Link to={`/docker/new?project=${p.id}`}>Add the first one</Link>: Watchly gives it a token for the
                    agent you run there.
                  </>
                )}
              </p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Status</th>
                      <th>Host</th>
                      <th>Containers</th>
                      <th>Last report</th>
                    </tr>
                  </thead>
                  <tbody>
                    {hosts.data.items.map((h) => (
                      <tr key={h.id}>
                        <td>
                          <HostStatusBadge status={h.status} />
                        </td>
                        <td>
                          <Link to={`/docker/hosts/${h.id}`} className="strong-link">
                            {h.name}
                          </Link>
                          {h.hostname && <div className="muted small">{h.hostname}</div>}
                        </td>
                        <td className="nowrap">
                          {h.counts.total}
                          {h.counts.down > 0 && <span className="text-down"> · {h.counts.down} down</span>}
                          {h.counts.unhealthy > 0 && <span className="text-pending"> · {h.counts.unhealthy} unhealthy</span>}
                        </td>
                        <td className="nowrap">{h.last_seen_at ? dateTime(h.last_seen_at) : 'never'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          {(hosts.data?.items ?? []).length > 0 && (
            <section className="section">
              <h2>Containers</h2>
              <ErrorBanner error={containers.error} />
              {containers.loading ? (
                <Loading />
              ) : (
                <ContainerTable
                  containers={containers.data?.items ?? []}
                  hosts={Object.fromEntries((hosts.data?.items ?? []).map((h) => [h.id, h]))}
                  emptyLabel="No containers reported yet."
                />
              )}
            </section>
          )}
        </>
      ) : infra ? (
        <>
          <section className="section">
            <h2>Resources</h2>
            <ErrorBanner error={resources.error} />
            {resources.loading ? (
              <Loading />
            ) : (
              <ResourceTable
                resources={resources.data?.items ?? []}
                emptyLabel={
                  canManage ? (
                    <>
                      No resources in this project yet.{' '}
                      <Link to={`/infra/new?project=${p.id}`}>Add the first ones</Link> from its AWS accounts.
                    </>
                  ) : (
                    'No resources in this project yet.'
                  )
                }
              />
            )}
          </section>
          <DeploymentsSection projectId={p.id} canManage={canManage} />
          {canManage && <AwsAccountsCard projectId={p.id} />}
        </>
      ) : (
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
      )}

      <div className="grid-2">
        <section className="card">
          <h2>Members</h2>
          <p className="muted small">
            Members can see this project and all its {infra ? 'resources' : docker ? 'Docker hosts' : 'websites'}, and
            are emailed about every {infra || docker ? 'one' : 'site'}.
            Only admins and DevOps see projects they aren&apos;t in.
          </p>
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
            <div>
              <dt>Telegram</dt>
              <dd>
                {p.telegram_configured
                  ? `${p.telegram_chat_id} (${p.telegram_token_hint})`
                  : p.telegram_token_hint
                    ? 'muted'
                    : 'not set up'}
              </dd>
            </div>
            <div>
              <dt>WhatsApp</dt>
              <dd>
                {p.whatsapp_configured
                  ? `${p.whatsapp_recipients.join(', ')} (${p.whatsapp_token_hint})`
                  : p.whatsapp_token_hint
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
          {websites && (
            <div className="section-actions">
              <button type="button" className="btn btn-sm" onClick={downloadReport} disabled={busy}>
                Download last month&apos;s report (CSV)
              </button>
              {canManage && (
                <button type="button" className="btn btn-sm" onClick={sendReport} disabled={busy}>
                  Send last month&apos;s report now
                </button>
              )}
            </div>
          )}
        </div>
        <NotificationSettings
          projectId={p.id}
          canEdit={canManage}
          channels={p.alert_channels}
          monitors={p.monitors}
        />
      </section>
    </>
  )
}
