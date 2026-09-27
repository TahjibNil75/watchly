import { useState } from 'react'
import { ErrorBanner, UserChecklist } from './components.jsx'
import { parseEmails, parsePhoneNumbers } from './format.js'

/**
 * Create mode when `initial` is absent (adds the member picker and extra
 * addresses); edit mode otherwise, where members and addresses are managed on
 * the project page instead. Every project needs at least one alert channel —
 * members or extra emails for email, a Slack bot token plus channel, a
 * Telegram bot token plus chat, and/or a WhatsApp sender plus numbers — and the
 * API explains what's missing if the form is submitted without one.
 */
export default function ProjectForm({ initial, users = [], onSubmit, onCancel, submitLabel }) {
  const creating = !initial
  const [form, setForm] = useState({
    name: initial?.name ?? '',
    description: initial?.description ?? '',
    extra_emails: '',
    is_active: initial?.is_active ?? true,
    slack_bot_token: '',
    slack_channel_id: initial?.slack_channel_id ?? '',
    slack_enabled: initial?.slack_enabled ?? true,
    telegram_bot_token: '',
    telegram_chat_id: initial?.telegram_chat_id ?? '',
    telegram_enabled: initial?.telegram_enabled ?? true,
    whatsapp_access_token: '',
    whatsapp_phone_number_id: initial?.whatsapp_phone_number_id ?? '',
    whatsapp_recipients: (initial?.whatsapp_recipients ?? []).join(', '),
    whatsapp_enabled: initial?.whatsapp_enabled ?? true,
  })
  const [memberIds, setMemberIds] = useState([])
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const set = (key) => (e) =>
    setForm({ ...form, [key]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })

  function buildPayload() {
    const token = form.slack_bot_token.trim()
    const channel = form.slack_channel_id.trim()
    const tgToken = form.telegram_bot_token.trim()
    const tgChat = form.telegram_chat_id.trim()
    const waToken = form.whatsapp_access_token.trim()
    const waNumberId = form.whatsapp_phone_number_id.trim()
    const waNumbers = parsePhoneNumbers(form.whatsapp_recipients)
    const payload = {
      name: form.name.trim(),
      description: form.description.trim() || null,
    }
    if (creating) {
      payload.extra_emails = parseEmails(form.extra_emails)
      payload.member_ids = memberIds
      if (token || channel) {
        payload.slack_bot_token = token || null
        payload.slack_channel_id = channel || null
      }
      if (tgToken || tgChat) {
        payload.telegram_bot_token = tgToken || null
        payload.telegram_chat_id = tgChat || null
      }
      if (waToken || waNumberId || waNumbers.length) {
        payload.whatsapp_access_token = waToken || null
        payload.whatsapp_phone_number_id = waNumberId || null
        payload.whatsapp_recipients = waNumbers
      }
      return payload
    }
    payload.is_active = form.is_active
    // The stored tokens are never sent back to us, so a blank field means "keep it".
    if (token) payload.slack_bot_token = token
    // Clearing the channel turns Slack off; the API drops the token with it.
    if (channel !== (initial.slack_channel_id ?? '')) payload.slack_channel_id = channel || null
    if (initial.slack_token_hint) payload.slack_enabled = form.slack_enabled
    // Telegram works the same way: clearing the chat drops the token with it.
    if (tgToken) payload.telegram_bot_token = tgToken
    if (tgChat !== (initial.telegram_chat_id ?? '')) payload.telegram_chat_id = tgChat || null
    if (initial.telegram_token_hint) payload.telegram_enabled = form.telegram_enabled
    // WhatsApp too: clearing the number id or the numbers drops the token with them.
    if (waToken) payload.whatsapp_access_token = waToken
    if (waNumberId !== (initial.whatsapp_phone_number_id ?? '')) {
      payload.whatsapp_phone_number_id = waNumberId || null
    }
    if (waNumbers.join(',') !== (initial.whatsapp_recipients ?? []).join(',')) {
      payload.whatsapp_recipients = waNumbers
    }
    if (initial.whatsapp_token_hint) payload.whatsapp_enabled = form.whatsapp_enabled
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

      {creating && (
        <fieldset className="fieldset">
          <legend>Members and email alerts</legend>
          <div className="field">
            <span>
              Members can see this project and its websites, and are alerted about every site
            </span>
            <UserChecklist users={users} selected={memberIds} onChange={setMemberIds} />
          </div>
          <label className="field">
            <span>Extra addresses (client contacts, shared inboxes)</span>
            <textarea
              rows={2}
              value={form.extra_emails}
              onChange={set('extra_emails')}
              placeholder="oncall@example.com"
            />
          </label>
          <p className="muted small">Both can be added later from the project page.</p>
        </fieldset>
      )}

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

      <fieldset className="fieldset">
        <legend>Telegram alerts (optional)</legend>
        <div className="row-2">
          <label className="field">
            <span>Bot token</span>
            <input
              type="password"
              value={form.telegram_bot_token}
              onChange={set('telegram_bot_token')}
              placeholder={
                initial?.telegram_token_hint
                  ? `Stored: ${initial.telegram_token_hint}`
                  : '123456789:AA…'
              }
              autoComplete="off"
            />
          </label>
          <label className="field">
            <span>Chat id</span>
            <input
              value={form.telegram_chat_id}
              onChange={set('telegram_chat_id')}
              placeholder="-1001234567890 or @channel"
              maxLength={64}
            />
          </label>
        </div>
        <p className="muted small">
          Create a bot with @BotFather and add it to the group, or to the channel as an admin
          that can post.
        </p>
        {!creating && initial.telegram_token_hint && (
          <>
            <label className="check">
              <input
                type="checkbox"
                checked={form.telegram_enabled}
                onChange={set('telegram_enabled')}
              />
              <span>Send Telegram alerts (untick to mute without losing the settings)</span>
            </label>
            <p className="muted small">
              Leave the token blank to keep the stored one. Clear the chat id to remove Telegram.
            </p>
          </>
        )}
      </fieldset>

      <fieldset className="fieldset">
        <legend>WhatsApp alerts (optional)</legend>
        <div className="row-2">
          <label className="field">
            <span>Access token</span>
            <input
              type="password"
              value={form.whatsapp_access_token}
              onChange={set('whatsapp_access_token')}
              placeholder={
                initial?.whatsapp_token_hint ? `Stored: ${initial.whatsapp_token_hint}` : 'EAA…'
              }
              autoComplete="off"
            />
          </label>
          <label className="field">
            <span>Phone number ID</span>
            <input
              value={form.whatsapp_phone_number_id}
              onChange={set('whatsapp_phone_number_id')}
              placeholder="106540352242922"
              inputMode="numeric"
              maxLength={32}
            />
          </label>
        </div>
        <label className="field">
          <span>Numbers to alert, with country code</span>
          <input
            value={form.whatsapp_recipients}
            onChange={set('whatsapp_recipients')}
            placeholder="+8801712345678, +447700900123"
          />
        </label>
        <p className="muted small">
          From Meta&apos;s WhatsApp Cloud API: a system user&apos;s access token, and the Phone
          number ID (not the number) of the business number that sends.
        </p>
        {!creating && initial.whatsapp_token_hint && (
          <>
            <label className="check">
              <input
                type="checkbox"
                checked={form.whatsapp_enabled}
                onChange={set('whatsapp_enabled')}
              />
              <span>Send WhatsApp alerts (untick to mute without losing the settings)</span>
            </label>
            <p className="muted small">
              Leave the token blank to keep the stored one. Clear the numbers to remove WhatsApp.
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
