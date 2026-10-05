import { useEffect, useRef, useState } from 'react'
import { AccountFields } from './AwsAccounts.jsx'
import { ErrorBanner, Loading, UserChecklist } from './components.jsx'
import { parseEmails, parsePhoneNumbers } from './format.js'
import { BLANK_ACCOUNT, newAccountPayload, useInfraEnabled } from './infra.js'

const NAME_MAX_WORDS = 10
const DESCRIPTION_MIN_WORDS = 5
const DESCRIPTION_MAX_WORDS = 150
const POPUP_MS = 6000

const countWords = (text) => text.trim().split(/\s+/).filter(Boolean).length

// 24x24 stroke icons for the "What it monitors" choice.
const MODE_ICONS = {
  websites: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M3 9h18M6.5 6.5h.01M9.5 6.5h.01" />
    </>
  ),
  infrastructure: (
    <>
      <rect x="3.5" y="4" width="17" height="6.5" rx="1.5" />
      <rect x="3.5" y="13.5" width="17" height="6.5" rx="1.5" />
      <path d="M7.5 7.25h.01M7.5 16.75h.01M11 7.25h5.5M11 16.75h5.5" />
    </>
  ),
}

const MODES = [
  {
    value: 'websites',
    label: 'Websites',
    covers: 'URLs, hosts and DNS records.',
    tags: ['HTTP checks', 'SSL and domain expiry', 'Ping and TCP', 'DNS records'],
  },
  {
    value: 'infrastructure',
    label: 'Infrastructure (AWS)',
    covers: "EC2 servers, load balancers and Auto Scaling groups, read from the project's own AWS accounts.",
    tags: ['EC2', 'Load balancers', 'Auto Scaling', 'Databases'],
  },
]

const STEP_INFO = {
  type: ['Type', 'What it monitors'],
  details: ['Details', 'Name and description'],
  accounts: ['AWS accounts', 'At least one'],
  people: ['People', 'Members and email'],
  chat: ['Chat alerts', 'Optional'],
  review: ['Review', 'Check and create'],
}

/**
 * Create mode when `initial` is absent: a wizard that takes one step at a time
 * (type, details, AWS accounts, people, chat alerts, review), with the member
 * picker and extra addresses. Edit mode otherwise, one form, where members and
 * addresses are managed on the project page instead. Every project needs at
 * least one alert channel — members or extra emails for email, a Slack bot
 * token plus channel, a Telegram bot token plus chat, and/or a WhatsApp sender
 * plus numbers — and the API explains what's missing if the form is submitted
 * without one.
 *
 * A new project monitors either websites or infrastructure, for good. An
 * infrastructure project starts with at least one AWS account; more can be
 * added here or later on the project page.
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
  const infraEnabled = useInfraEnabled()
  const [monitors, setMonitors] = useState('websites')
  const nextKey = useRef(1)
  const [accounts, setAccounts] = useState([{ ...BLANK_ACCOUNT, key: 0 }])
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  // The word limits are not shown on the form; breaking one pops this up.
  const [popup, setPopup] = useState(null)
  // The wizard's place: the step, and how far the user has got, by position.
  // Changing what is monitored changes the steps, so it sends them back to the type.
  const [stepId, setStepId] = useState(null)
  const [furthest, setFurthest] = useState(0)
  const formRef = useRef(null)

  useEffect(() => {
    if (!popup) return undefined
    const timer = setTimeout(() => setPopup(null), POPUP_MS)
    return () => clearTimeout(timer)
  }, [popup])

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
      payload.monitors = monitors
      if (monitors === 'infrastructure') payload.aws_accounts = accounts.map(newAccountPayload)
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

  const infra = monitors === 'infrastructure'
  const stepIds = [
    ...(infraEnabled ? ['type'] : []),
    'details',
    ...(infra ? ['accounts'] : []),
    'people',
    'chat',
    'review',
  ]
  const step = stepId ?? stepIds[0]
  const stepIndex = stepIds.indexOf(step)
  const lastStep = step === 'review'

  // What is wrong with the name or description, if anything: shown as a popup.
  function detailsProblem() {
    if (!form.name.trim()) return { title: 'Check the name', text: 'Give the project a name.' }
    if (creating && countWords(form.name) > NAME_MAX_WORDS) {
      return { title: 'Check the name', text: `The name can be at most ${NAME_MAX_WORDS} words.` }
    }
    const words = countWords(form.description)
    if (words > 0 && words < DESCRIPTION_MIN_WORDS) {
      return {
        title: 'Check the description',
        text: `The description is too short. Add a few more words (${DESCRIPTION_MIN_WORDS} or more).`,
      }
    }
    if (words > DESCRIPTION_MAX_WORDS) {
      return {
        title: 'Check the description',
        text: `The description is too long. Shorten it to ${DESCRIPTION_MAX_WORDS} words or fewer (it has ${words}).`,
      }
    }
    return null
  }

  function goTo(id) {
    setStepId(id)
    setPopup(null)
    setError(null)
  }

  // Only the current step is on screen, so the browser's own checks (the
  // required AWS account fields) cover just that step.
  function next() {
    if (!formRef.current.reportValidity()) return
    if (step === 'details') {
      const problem = detailsProblem()
      if (problem) return setPopup(problem)
    }
    const following = stepIndex + 1
    setFurthest(Math.max(furthest, following))
    goTo(stepIds[following])
  }

  function chooseMonitors(value) {
    setMonitors(value)
    setFurthest(0)
  }

  async function submit(event) {
    event.preventDefault()
    // Enter in a field moves the wizard on; only the last step creates.
    if (creating && !lastStep) return next()
    const problem = detailsProblem()
    if (problem) {
      if (creating) setStepId('details')
      return setPopup(problem)
    }
    setPopup(null)
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

  const nameField = (
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
  )
  const descriptionField = (
    <label className="field">
      <span>
        Description <span className="muted">(optional)</span>
      </span>
      <input value={form.description} onChange={set('description')} />
    </label>
  )

  const chatFields = (
    <>
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
              autoComplete="new-password"
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
              autoComplete="new-password"
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
              autoComplete="new-password"
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
    </>
  )

  const popupToast = popup && (
    <section className="toasts" aria-live="assertive" aria-label="Form error">
      <div className="toast tone-down" role="alert">
        <span className="toast-dot" aria-hidden="true" />
        <div className="toast-body">
          <strong>{popup.title}</strong>
          <span className="muted">{popup.text}</span>
        </div>
        <button type="button" className="toast-close" aria-label="Dismiss" onClick={() => setPopup(null)}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </div>
    </section>
  )

  if (!creating) {
    return (
      <form className="form" onSubmit={submit} autoComplete="off">
        <ErrorBanner error={error} />
        {nameField}
        {descriptionField}
        {chatFields}

        <label className="check">
          <input type="checkbox" checked={form.is_active} onChange={set('is_active')} />
          <span>Project is active</span>
        </label>

        <div className="form-actions">
          <button className="btn btn-primary" disabled={busy}>
            {busy ? 'Saving…' : submitLabel ?? 'Save'}
          </button>
          {onCancel && (
            <button type="button" className="btn btn-danger-solid" onClick={onCancel}>
              Cancel
            </button>
          )}
        </div>
        {popupToast}
      </form>
    )
  }

  // Whether the infrastructure setting has been read yet: the type step depends on it.
  if (infraEnabled === null) return <Loading />

  const memberNames = users.filter((u) => memberIds.includes(u.id)).map((u) => u.full_name || u.username)
  const extraEmails = parseEmails(form.extra_emails)
  // The channels this form would set up, as the API will count them.
  const alertChannels = [
    ...(memberIds.length || extraEmails.length ? ['Email'] : []),
    ...(form.slack_bot_token.trim() && form.slack_channel_id.trim() ? ['Slack'] : []),
    ...(form.telegram_bot_token.trim() && form.telegram_chat_id.trim() ? ['Telegram'] : []),
    ...(form.whatsapp_access_token.trim() &&
    form.whatsapp_phone_number_id.trim() &&
    parsePhoneNumbers(form.whatsapp_recipients).length
      ? ['WhatsApp']
      : []),
  ]

  const panes = {
    type: (
      <>
        <div>
          <h2>What will this project monitor?</h2>
          <p className="muted small wizard-lock">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <rect x="5" y="11" width="14" height="9" rx="2" />
              <path d="M8 11V8a4 4 0 0 1 8 0v3" />
            </svg>
            This can&apos;t be changed once the project exists.
          </p>
        </div>
        <div className="wizard-modes" role="radiogroup" aria-label="What the project monitors">
          {MODES.map((mode) => (
            <button
              key={mode.value}
              type="button"
              role="radio"
              aria-checked={monitors === mode.value}
              className={`wizard-mode is-${mode.value}`}
              onClick={() => chooseMonitors(mode.value)}
            >
              <span className="wizard-mode-icon">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  {MODE_ICONS[mode.value]}
                </svg>
              </span>
              <strong>{mode.label}</strong>
              <span className="muted small">{mode.covers}</span>
              <span className="wizard-tags">
                {mode.tags.map((tag) => (
                  <span key={tag}>{tag}</span>
                ))}
              </span>
            </button>
          ))}
        </div>
      </>
    ),
    details: (
      <>
        <h2>Project details</h2>
        {nameField}
        {descriptionField}
      </>
    ),
    accounts: (
      <>
        <h2>AWS accounts</h2>
        <p className="muted small">
          At least one. Each is tried with AWS before the project is created; its secret is stored encrypted and
          never shown again. Resources are added from these accounts, on the Infrastructure page.
        </p>
        {accounts.map((account, index) => (
          <div key={account.key} className="account-entry">
            {accounts.length > 1 && (
              <div className="account-entry-head">
                <strong>Account {index + 1}</strong>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => setAccounts(accounts.filter((a) => a.key !== account.key))}
                >
                  Remove
                </button>
              </div>
            )}
            <AccountFields
              value={account}
              onChange={(updated) => setAccounts(accounts.map((a) => (a.key === account.key ? updated : a)))}
            />
          </div>
        ))}
        <button
          type="button"
          className="btn btn-sm wizard-add"
          onClick={() => setAccounts([...accounts, { ...BLANK_ACCOUNT, key: nextKey.current++ }])}
        >
          Add another AWS account
        </button>
      </>
    ),
    people: (
      <>
        <h2>Members and email alerts</h2>
        <div className="field">
          <span>
            Members can see this project and its {infra ? 'resources' : 'websites'}, and are alerted about every{' '}
            {infra ? 'one' : 'site'}
          </span>
          <UserChecklist users={users} selected={memberIds} onChange={setMemberIds} />
        </div>
        <label className="field">
          <span>Extra addresses (client contacts, shared inboxes)</span>
          <textarea rows={2} value={form.extra_emails} onChange={set('extra_emails')} placeholder="oncall@example.com" />
        </label>
        <p className="muted small">Both can be added later from the project page.</p>
      </>
    ),
    chat: (
      <>
        <h2>
          Chat alerts <span className="muted small wizard-optional">(all optional)</span>
        </h2>
        {chatFields}
      </>
    ),
    review: (
      <>
        <h2>Review</h2>
        <dl className="wizard-review">
          <dt>Monitors</dt>
          <dd>{MODES.find((m) => m.value === monitors).label}</dd>
          <dt>Name</dt>
          <dd>{form.name.trim() || '—'}</dd>
          {form.description.trim() && (
            <>
              <dt>Description</dt>
              <dd>{form.description.trim()}</dd>
            </>
          )}
          {infra && (
            <>
              <dt>AWS accounts</dt>
              <dd>{accounts.map((a) => a.name.trim() || 'Unnamed').join(', ')}</dd>
            </>
          )}
          <dt>Members</dt>
          <dd>{memberNames.length ? memberNames.join(', ') : 'None'}</dd>
          <dt>Extra addresses</dt>
          <dd>{extraEmails.length ? extraEmails.join(', ') : 'None'}</dd>
          <dt>Alerts go to</dt>
          <dd>{alertChannels.length ? alertChannels.join(', ') : 'Nowhere yet'}</dd>
        </dl>
        {!alertChannels.length && (
          <p className="text-down small">
            Every project needs at least one alert channel: a member, an extra address, or a chat channel. Go back
            to People or Chat alerts to add one.
          </p>
        )}
        <p className="muted small">Nothing is created until you press the button.</p>
      </>
    ),
  }

  return (
    <form ref={formRef} className="wizard" data-monitors={monitors} onSubmit={submit} autoComplete="off">
      <nav className="card wizard-rail" aria-label="Steps">
        {stepIds.map((id, index) => (
          <button
            key={id}
            type="button"
            className={`wizard-step${index < stepIndex ? ' is-done' : ''}${id === step ? ' is-current' : ''}`}
            aria-current={id === step ? 'step' : undefined}
            disabled={index > furthest}
            onClick={() => goTo(id)}
          >
            <span className="wizard-dot" aria-hidden="true">
              {index < stepIndex ? (
                <svg viewBox="0 0 24 24">
                  <path d="M5 12.5l4.5 4.5L19 7.5" />
                </svg>
              ) : (
                index + 1
              )}
            </span>
            <span>
              <strong>{STEP_INFO[id][0]}</strong>
              <small>{STEP_INFO[id][1]}</small>
            </span>
          </button>
        ))}
      </nav>

      <section className="card wizard-card">
        <ErrorBanner error={error} />
        <div className="wizard-pane" key={step}>
          {panes[step]}
        </div>
        <div className="wizard-nav">
          {stepIndex > 0 ? (
            <button type="button" className="btn" onClick={() => goTo(stepIds[stepIndex - 1])}>
              Back
            </button>
          ) : (
            <span />
          )}
          <span className="form-actions">
            {onCancel && (
              <button type="button" className="btn btn-danger-solid" onClick={onCancel}>
                Cancel
              </button>
            )}
            <button key={lastStep ? 'create' : 'next'} className="btn btn-primary" disabled={busy}>
              {lastStep ? (busy ? (infra ? 'Asking AWS…' : 'Saving…') : submitLabel ?? 'Create project') : 'Next'}
            </button>
          </span>
        </div>
      </section>
      {popupToast}
    </form>
  )
}
