import { useState } from 'react'
import { ErrorBanner, UserChecklist } from './components.jsx'
import { ENVIRONMENTS } from './environments.js'
import { duration, parseEmails } from './format.js'

const METHODS = ['GET', 'HEAD', 'POST', 'OPTIONS']
const INTERVALS = [30, 60, 120, 300, 600, 900, 1800, 3600, 21600, 86400]

const BLANK = {
  project_id: '',
  environment: '',
  name: '',
  url: '',
  method: 'GET',
  expected_status: 200,
  timeout_seconds: 10,
  check_interval_seconds: 300,
  max_down_alerts: 4,
  slow_threshold_ms: '',
  alert_emails: '',
  inherit_project_recipients: true,
  slack_channel_id: '',
  slack_bot_token: '',
}

function fromSite(site) {
  return {
    ...BLANK,
    ...site,
    alert_emails: site.alert_emails.join(', '),
    slack_channel_id: site.slack_channel_id ?? '',
    // The stored token is never sent back to us; blank means "keep it".
    slack_bot_token: '',
    slow_threshold_ms: site.slow_threshold_ms ?? '',
    environment: site.environment ?? '',
  }
}

// "example.com" is a common thing to type; the API only accepts full URLs.
function withScheme(url) {
  const trimmed = url.trim()
  return /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`
}

// What the Slack fields will do, given the project's own Slack setup.
function slackHint(project, site) {
  if (site?.slack_token_hint) {
    return (
      'This site posts with its own bot. Leave the token blank to keep it; ' +
      "clear the channel id to remove this site's Slack."
    )
  }
  if (!project) {
    return "Leave blank to use the project's Slack, if it has one. A bot token needs a channel id."
  }
  if (project.slack_channel_id) {
    const muted = project.slack_enabled ? '' : ' (currently muted)'
    return (
      `Leave blank to post to ${project.name}'s channel ${project.slack_channel_id}${muted}. ` +
      "Add a channel id to post elsewhere with the project's bot, or a bot token too to use another bot."
    )
  }
  return null
}

/**
 * Create mode when `projects` is passed (adds the project picker and
 * recipients); edit mode when `initial` is a website, with `project` its
 * project when known.
 */
export default function WebsiteForm({
  initial,
  projects,
  project: siteProject,
  users = [],
  defaultProjectId,
  onSubmit,
  onCancel,
  submitLabel = 'Save',
}) {
  const creating = Boolean(projects)
  const [form, setForm] = useState(() =>
    initial
      ? fromSite(initial)
      : { ...BLANK, project_id: defaultProjectId ?? projects?.[0]?.id ?? '' },
  )
  const [recipientIds, setRecipientIds] = useState([])
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const set = (key) => (e) =>
    setForm({ ...form, [key]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })

  const project = creating
    ? projects.find((p) => String(p.id) === String(form.project_id))
    : siteProject
  const hint = slackHint(project, initial)
  const slackChannel = form.slack_channel_id.trim()
  const slackToken = form.slack_bot_token.trim()
  // A channel needs a bot from somewhere; don't insist while the project is unknown.
  const needsToken =
    Boolean(slackChannel) &&
    Boolean(project) &&
    !project.slack_channel_id &&
    !initial?.slack_token_hint

  const intervals = INTERVALS.includes(Number(form.check_interval_seconds))
    ? INTERVALS
    : [...INTERVALS, Number(form.check_interval_seconds)].sort((a, b) => a - b)

  async function submit(event) {
    event.preventDefault()
    const payload = {
      name: form.name.trim(),
      environment: form.environment || null,
      url: withScheme(form.url),
      method: form.method,
      expected_status: Number(form.expected_status),
      timeout_seconds: Number(form.timeout_seconds),
      check_interval_seconds: Number(form.check_interval_seconds),
      max_down_alerts: Number(form.max_down_alerts),
      // Blank means "use the server-wide threshold".
      slow_threshold_ms: form.slow_threshold_ms === '' ? null : Number(form.slow_threshold_ms),
      alert_emails: parseEmails(form.alert_emails),
      inherit_project_recipients: form.inherit_project_recipients,
      // Clearing the channel removes the site's own Slack, token included.
      slack_channel_id: slackChannel || null,
    }
    if (slackToken) payload.slack_bot_token = slackToken
    if (creating) {
      payload.project_id = Number(form.project_id)
      payload.recipient_ids = recipientIds
    }
    setBusy(true)
    setError(null)
    try {
      await onSubmit(payload)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="form" onSubmit={submit}>
      <ErrorBanner error={error} />

      <div className="row-2">
        {creating && (
          <label className="field">
            <span>Project</span>
            <select value={form.project_id} onChange={set('project_id')} required>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="field">
          <span>Environment</span>
          {/* Required for new sites; sites that predate the field can stay unset. */}
          <select value={form.environment} onChange={set('environment')} required={creating}>
            <option value="">{creating ? 'Select environment…' : 'Not set'}</option>
            {ENVIRONMENTS.map((env) => (
              <option key={env.value} value={env.value}>
                {env.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="row-2">
        <label className="field">
          <span>Name</span>
          <input
            value={form.name}
            onChange={set('name')}
            placeholder="Marketing site"
            required
            maxLength={255}
          />
        </label>
        <label className="field">
          <span>URL</span>
          <input
            value={form.url}
            onChange={set('url')}
            placeholder="https://example.com/"
            required
          />
        </label>
      </div>

      <div className="row-3">
        <label className="field">
          <span>Check every</span>
          <select value={form.check_interval_seconds} onChange={set('check_interval_seconds')}>
            {intervals.map((s) => (
              <option key={s} value={s}>
                {duration(s)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Timeout (seconds)</span>
          <input
            type="number"
            min={1}
            max={120}
            value={form.timeout_seconds}
            onChange={set('timeout_seconds')}
            required
          />
        </label>
        <label className="field">
          <span>Alerts per outage</span>
          <input
            type="number"
            min={1}
            max={50}
            value={form.max_down_alerts}
            onChange={set('max_down_alerts')}
            required
          />
        </label>
      </div>

      <details className="advanced">
        <summary>Request options</summary>
        <div className="row-3">
          <label className="field">
            <span>Method</span>
            <select value={form.method} onChange={set('method')}>
              {METHODS.map((m) => (
                <option key={m}>{m}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Expected status</span>
            <input
              type="number"
              min={100}
              max={599}
              value={form.expected_status}
              onChange={set('expected_status')}
              required
            />
          </label>
          <label className="field">
            <span>Slow after (ms)</span>
            <input
              type="number"
              min={1}
              max={120000}
              value={form.slow_threshold_ms}
              onChange={set('slow_threshold_ms')}
              placeholder="Server default"
            />
            <span className="muted small">
              Alert when successful responses stay slower than this.
            </span>
          </label>
        </div>
      </details>

      <fieldset className="fieldset">
        <legend>Email alerts</legend>
        <label className="check">
          <input
            type="checkbox"
            checked={form.inherit_project_recipients}
            onChange={set('inherit_project_recipients')}
          />
          <span>Alert the project's members and extra emails</span>
        </label>
        {creating && users.length > 0 && (
          <div className="field">
            <span>Also give these users access to this site and alert them about it</span>
            <UserChecklist users={users} selected={recipientIds} onChange={setRecipientIds} />
          </div>
        )}
        <label className="field">
          <span>Extra email addresses for this site</span>
          <textarea
            rows={2}
            value={form.alert_emails}
            onChange={set('alert_emails')}
            placeholder="client@example.com, oncall@example.com"
          />
        </label>
      </fieldset>

      <fieldset className="fieldset">
        <legend>Slack alerts (optional)</legend>
        {hint && <p className="muted small">{hint}</p>}
        <div className="row-2">
          <label className="field">
            <span>Channel id</span>
            <input
              value={form.slack_channel_id}
              onChange={set('slack_channel_id')}
              placeholder={project?.slack_channel_id ?? 'C0123456789'}
              maxLength={32}
              required={Boolean(slackToken)}
            />
          </label>
          <label className="field">
            <span>Bot token</span>
            <input
              type="password"
              value={form.slack_bot_token}
              onChange={set('slack_bot_token')}
              placeholder={
                initial?.slack_token_hint
                  ? `Stored: ${initial.slack_token_hint}`
                  : project?.slack_channel_id
                    ? "Project's bot"
                    : 'xoxb-…'
              }
              autoComplete="off"
              required={needsToken}
            />
          </label>
        </div>
      </fieldset>

      <div className="form-actions">
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Saving…' : submitLabel}
        </button>
        {onCancel && (
          <button type="button" className="btn btn-ghost" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </form>
  )
}
