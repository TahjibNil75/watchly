import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import {
  Empty,
  EnvironmentBadge,
  ErrorBanner,
  Loading,
  PageHeader,
  PersonList,
  StatusBadge,
  UserChecklist,
} from '../components.jsx'
import { environmentLabel } from '../environments.js'
import { dateTime, duration, since, timeAgo } from '../format.js'
import { canManageProject } from '../roles.js'
import SiteHistory from '../SiteHistory.jsx'
import { useApi } from '../useApi.js'
import WebsiteForm from '../WebsiteForm.jsx'

const CHECKS_SHOWN = 60

// A failed connection has no response, so its recorded time (often 0) means nothing.
const responseTime = (c) => (c.status_code != null ? c.response_time_ms : null)

const STEPS = [
  ['dns_ms', 'DNS'],
  ['connect_ms', 'connect'],
  ['tls_ms', 'TLS'],
  ['first_byte_ms', 'first byte'],
]

// Where a check's time went, e.g. "DNS 12 ms · connect 40 ms · …"; empty when untimed.
const timeSplit = (c) =>
  STEPS.filter(([key]) => c[key] != null)
    .map(([key, label]) => `${label} ${c[key]} ms`)
    .join(' · ')

function CheckStrip({ checks }) {
  // Oldest on the left, like a status page. Empty slots on the left keep bar
  // widths steady while history builds up.
  const ordered = [...checks].reverse()
  const slowest = Math.max(1, ...ordered.map((c) => responseTime(c) ?? 0))
  const padding = Math.max(0, CHECKS_SHOWN - ordered.length)
  return (
    <div className="strip" aria-label="Recent checks, oldest first">
      {Array.from({ length: padding }, (_, i) => (
        <span key={`empty-${i}`} className="strip-bar is-empty" />
      ))}
      {ordered.map((c) => (
        <span
          key={c.id}
          className={`strip-bar ${c.is_up ? 'is-up' : 'is-down'}`}
          style={{ height: `${c.is_up ? 25 + (75 * (responseTime(c) ?? 0)) / slowest : 100}%` }}
          title={`${dateTime(c.checked_at)} · ${c.is_up ? 'up' : 'down'}${
            responseTime(c) != null ? ` · ${responseTime(c)} ms` : ''
          }${c.error ? ` · ${c.error}` : ''}`}
        />
      ))}
    </div>
  )
}

const COLLAPSED_ROWS = 15

function Checks({ checks }) {
  const [expanded, setExpanded] = useState(false)
  if (!checks.length) return <Empty>No checks yet. The first one runs within a minute.</Empty>

  return (
    <>
      <CheckStrip checks={checks} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Result</th>
              <th>HTTP</th>
              <th>Response</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {(expanded ? checks : checks.slice(0, COLLAPSED_ROWS)).map((c) => (
              <tr key={c.id}>
                <td className="nowrap" title={dateTime(c.checked_at)}>
                  {timeAgo(c.checked_at)}
                </td>
                <td>
                  <StatusBadge status={c.is_up ? 'up' : 'down'} />
                </td>
                <td>{c.status_code ?? '—'}</td>
                <td className="nowrap" title={timeSplit(c) || undefined}>
                  {responseTime(c) != null ? `${responseTime(c)} ms` : '—'}
                </td>
                <td className="muted small">
                  {c.error ?? ''}
                  {c.final_url && <div className="truncate">→ {c.final_url}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {checks.length > COLLAPSED_ROWS && (
        <button
          type="button"
          className="btn btn-sm show-more"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? 'Show fewer' : `Show all ${checks.length} checks`}
        </button>
      )}
    </>
  )
}

// Whole days until the certificate ends; negative once it has.
const sslDaysLeft = (iso) => Math.floor((new Date(iso).getTime() - Date.now()) / 86_400_000)

export default function WebsiteDetail() {
  const { id } = useParams()
  const { user } = useAuth()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [adding, setAdding] = useState([])
  const [notice, setNotice] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [busy, setBusy] = useState(false)

  const site = useApi(() => api.getWebsite(id), [id], { pollMs: 30000 })
  const checks = useApi(() => api.listChecks(id, CHECKS_SHOWN), [id], { pollMs: 30000 })
  const projectId = site.data?.project_id
  // A site recipient may see the site without being able to see its project.
  const project = useApi(
    () => (projectId ? api.getProject(projectId).catch(() => null) : null),
    [projectId],
  )
  const canManage = canManageProject(user, project.data)
  const users = useApi(() => (canManage ? api.listUsers({ is_active: true }) : null), [canManage])

  if (site.loading) return <Loading />
  if (!site.data) {
    return (
      <>
        <ErrorBanner error={site.error} />
        <Link to="/">← Back to websites</Link>
      </>
    )
  }

  const s = site.data

  async function run(action) {
    setBusy(true)
    setActionError(null)
    setNotice(null)
    try {
      await action()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(false)
    }
  }

  const checkNow = () =>
    run(async () => {
      const result = await api.checkNow(s.id)
      site.setData(result.website)
      checks.reload()
      const c = result.check
      const detail = c.is_up
        ? `up · HTTP ${c.status_code} in ${c.response_time_ms} ms`
        : `down · ${c.error ?? `HTTP ${c.status_code}`}`
      setNotice(`Checked just now: ${detail}${result.alert_sent ? ` · "${result.alert_sent}" alert sent` : ''}`)
    })

  const toggleEnabled = () =>
    run(async () => site.setData(await api.updateWebsite(s.id, { is_enabled: !s.is_enabled })))

  const remove = () => {
    if (!window.confirm(`Stop monitoring "${s.name}" and delete its check history?`)) return
    run(async () => {
      await api.deleteWebsite(s.id)
      navigate(project.data ? `/projects/${project.data.id}` : '/')
    })
  }

  const removeRecipient = (person) =>
    run(async () => site.setData(await api.removeRecipient(s.id, person.id)))

  const addRecipients = () =>
    run(async () => {
      site.setData(await api.addRecipients(s.id, adding))
      setAdding([])
    })

  const recipientIds = new Set(s.recipients.map((r) => r.id))
  const candidates = (users.data?.items ?? []).filter((u) => !recipientIds.has(u.id))

  return (
    <>
      <p className="crumbs">
        <Link to="/">Websites</Link>
        {project.data && (
          <>
            {' / '}
            <Link to={`/projects/${project.data.id}`}>{project.data.name}</Link>
          </>
        )}
      </p>
      <PageHeader
        title={
          <>
            {s.name} <StatusBadge status={s.status} enabled={s.is_enabled} />
            <EnvironmentBadge environment={s.environment} />
          </>
        }
        subtitle={
          <a href={s.url} target="_blank" rel="noreferrer">
            {s.url}
          </a>
        }
      >
        {canManage && (
          <>
            <button type="button" className="btn btn-primary" onClick={checkNow} disabled={busy}>
              {busy ? 'Working…' : 'Check now'}
            </button>
            <button type="button" className="btn" onClick={toggleEnabled} disabled={busy}>
              {s.is_enabled ? 'Pause' : 'Resume'}
            </button>
            <button type="button" className="btn" onClick={() => setEditing(!editing)}>
              {editing ? 'Close editor' : 'Edit'}
            </button>
            <button type="button" className="btn btn-danger" onClick={remove} disabled={busy}>
              Delete
            </button>
          </>
        )}
      </PageHeader>

      <ErrorBanner error={actionError ?? site.error} />
      {notice && <div className="banner banner-info">{notice}</div>}
      {s.status === 'down' && s.is_enabled && s.down_since && (
        <div className="banner banner-error">
          Down for {since(s.down_since)} (since {dateTime(s.down_since)}) ·{' '}
          {s.consecutive_failures} failed {s.consecutive_failures === 1 ? 'check' : 'checks'} ·{' '}
          {s.down_alerts_sent} of {s.max_down_alerts} alerts sent
        </div>
      )}

      {editing && (
        <section className="card">
          <h2>Edit website</h2>
          <WebsiteForm
            initial={s}
            project={project.data}
            onCancel={() => setEditing(false)}
            onSubmit={async (payload) => {
              site.setData(await api.updateWebsite(s.id, payload))
              setEditing(false)
            }}
          />
        </section>
      )}

      <div className="grid-2">
        <section className="card">
          <h2>Configuration</h2>
          <dl className="kv">
            <div>
              <dt>Environment</dt>
              <dd>{environmentLabel(s.environment) ?? '—'}</dd>
            </div>
            <div>
              <dt>Last checked</dt>
              <dd title={dateTime(s.last_checked_at)}>{timeAgo(s.last_checked_at)}</dd>
            </div>
            <div>
              <dt>Interval</dt>
              <dd>every {duration(s.check_interval_seconds)}</dd>
            </div>
            <div>
              <dt>Request</dt>
              <dd>
                {s.method}, expects {s.expected_status}
              </dd>
            </div>
            <div>
              <dt>Timeout</dt>
              <dd>{s.timeout_seconds}s</dd>
            </div>
            <div>
              <dt>Alerts per outage</dt>
              <dd>{s.max_down_alerts}</dd>
            </div>
            <div>
              <dt>Retries</dt>
              <dd>{s.retries_on_failure}</dd>
            </div>
            {s.must_contain && (
              <div>
                <dt>Must contain</dt>
                <dd className="truncate">{s.must_contain}</dd>
              </div>
            )}
            {s.must_not_contain && (
              <div>
                <dt>Must not contain</dt>
                <dd className="truncate">{s.must_not_contain}</dd>
              </div>
            )}
            <div>
              <dt>Slow after</dt>
              <dd>{s.slow_threshold_ms ? `${s.slow_threshold_ms} ms` : 'server default'}</dd>
            </div>
            <div>
              <dt>SSL certificate</dt>
              <dd>
                {s.ssl_expires_at ? (
                  <span className={sslDaysLeft(s.ssl_expires_at) <= 7 ? 'text-down' : undefined}>
                    {sslDaysLeft(s.ssl_expires_at) < 0
                      ? `expired ${dateTime(s.ssl_expires_at)}`
                      : `expires ${dateTime(s.ssl_expires_at)} (${sslDaysLeft(s.ssl_expires_at)} days)`}
                  </span>
                ) : s.url.startsWith('https:') ? (
                  'not read yet'
                ) : (
                  '—'
                )}
              </dd>
            </div>
            <div>
              <dt>Monitoring since</dt>
              <dd>{dateTime(s.created_at)}</dd>
            </div>
          </dl>
        </section>

        <section className="card">
          <h2>Alerting</h2>
          <dl className="kv">
            <div>
              <dt>Channels</dt>
              <dd>
                {s.alert_channels.length
                  ? s.alert_channels.map((c) => (
                      <span key={c} className="chip">
                        {c}
                      </span>
                    ))
                  : '—'}
              </dd>
            </div>
            <div>
              <dt>Project recipients</dt>
              <dd>{s.inherit_project_recipients ? 'included' : 'not included'}</dd>
            </div>
            <div>
              <dt>Extra emails</dt>
              <dd>{s.alert_emails.length ? s.alert_emails.join(', ') : '—'}</dd>
            </div>
            {(s.slack_channel_id || s.alert_channels.includes('slack')) && (
              <div>
                <dt>Slack</dt>
                <dd>
                  {s.slack_channel_id ?? "project's channel"} ·{' '}
                  {s.slack_token_hint ? `own bot (${s.slack_token_hint})` : "project's bot"}
                </dd>
              </div>
            )}
          </dl>
          <h3>Site recipients</h3>
          <p className="muted small">
            Recipients can see this site and its checks, even if they aren&apos;t in its project.
          </p>
          <PersonList
            people={s.recipients}
            onRemove={canManage ? removeRecipient : null}
            emptyLabel="No users are alerted about this site specifically."
          />
          {canManage && candidates.length > 0 && (
            <details className="advanced">
              <summary>Add recipients</summary>
              <UserChecklist users={candidates} selected={adding} onChange={setAdding} />
              <button
                type="button"
                className="btn btn-sm"
                onClick={addRecipients}
                disabled={busy || !adding.length}
              >
                Add {adding.length || ''} selected
              </button>
            </details>
          )}
        </section>
      </div>

      <SiteHistory websiteId={s.id} />

      <section className="card">
        <h2>Recent checks</h2>
        <ErrorBanner error={checks.error} />
        {checks.loading ? <Loading /> : <Checks checks={checks.data ?? []} />}
      </section>
    </>
  )
}
