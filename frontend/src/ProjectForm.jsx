import { useState } from 'react'
import { ErrorBanner, UserChecklist } from './components.jsx'
import { parseEmails } from './format.js'

/**
 * Create mode when `initial` is absent (adds the member picker); edit mode
 * otherwise. Every project needs at least one alert channel — members or
 * extra emails for email, and/or a Slack bot token plus channel — and the API
 * explains what's missing if the form is submitted without one.
 */
export default function ProjectForm({ initial, users = [], onSubmit, onCancel, submitLabel }) {
  const creating = !initial
  const [form, setForm] = useState({
    name: initial?.name ?? '',
    description: initial?.description ?? '',
    extra_emails: (initial?.extra_emails ?? []).join(', '),
    is_active: initial?.is_active ?? true,
    slack_bot_token: '',
    slack_channel_id: initial?.slack_channel_id ?? '',
    slack_enabled: initial?.slack_enabled ?? true,
  })
  const [memberIds, setMemberIds] = useState([])
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const set = (key) => (e) =>
    setForm({ ...form, [key]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })

  function buildPayload() {
    const token = form.slack_bot_token.trim()
    const channel = form.slack_channel_id.trim()
    const payload = {
      name: form.name.trim(),
      description: form.description.trim() || null,
      extra_emails: parseEmails(form.extra_emails),
    }
    if (creating) {
      payload.member_ids = memberIds
      if (token || channel) {
        payload.slack_bot_token = token || null
        payload.slack_channel_id = channel || null
      }
      return payload
    }
    payload.is_active = form.is_active
    // The stored token is never sent back to us, so a blank field means "keep it".
    if (token) payload.slack_bot_token = token
    // Clearing the channel turns Slack off; the API drops the token with it.
    if (channel !== (initial.slack_channel_id ?? '')) payload.slack_channel_id = channel || null
    if (initial.slack_token_hint) payload.slack_enabled = form.slack_enabled
    return payload
  }

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await onSubmit(buildPayload())
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="form" onSubmit={submit}>
      <ErrorBanner error={error} />
      <label className="field">
        <span>Name</span>
        <input
          value={form.name}
          onChange={set('name')}
          placeholder="Client or product name"
          required
          maxLength={255}
        />
      </label>
      <label className="field">
        <span>
          Description <span className="muted">(optional)</span>
        </span>
        <input value={form.description} onChange={set('description')} />
      </label>

      <fieldset className="fieldset">
        <legend>Email alerts</legend>
        {creating && (
          <div className="field">
            <span>Responsible members, alerted about every site in the project</span>
            <UserChecklist users={users} selected={memberIds} onChange={setMemberIds} />
          </div>
        )}
        <label className="field">
          <span>Extra addresses (client contacts, shared inboxes)</span>
          <textarea
            rows={2}
            value={form.extra_emails}
            onChange={set('extra_emails')}
            placeholder="oncall@example.com"
          />
        </label>
      </fieldset>

      <fieldset className="fieldset">
        <legend>Slack alerts (optional)</legend>
        <div className="row-2">
          <label className="field">
            <span>Bot token</span>
            <input
              type="password"
              value={form.slack_bot_token}
              onChange={set('slack_bot_token')}
              placeholder={
                initial?.slack_token_hint ? `Stored: ${initial.slack_token_hint}` : 'xoxb-…'
              }
              autoComplete="off"
            />
          </label>
          <label className="field">
            <span>Channel id</span>
            <input
              value={form.slack_channel_id}
              onChange={set('slack_channel_id')}
              placeholder="C0123456789"
              maxLength={32}
            />
          </label>
        </div>
        {!creating && initial.slack_token_hint && (
          <>
            <label className="check">
              <input type="checkbox" checked={form.slack_enabled} onChange={set('slack_enabled')} />
              <span>Send Slack alerts (untick to mute without losing the settings)</span>
            </label>
            <p className="muted small">
              Leave the token blank to keep the stored one. Clear the channel id to remove Slack.
            </p>
          </>
        )}
      </fieldset>

      {!creating && (
        <label className="check">
          <input type="checkbox" checked={form.is_active} onChange={set('is_active')} />
          <span>Project is active</span>
        </label>
      )}

      <div className="form-actions">
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Saving…' : submitLabel ?? (creating ? 'Create project' : 'Save')}
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
