import { useState } from 'react'
import { api } from './api.js'
import {
  CHECK_TYPES,
  MAIL_PROVIDERS,
  MAIL_TLS,
  RECORD_TYPES,
  SES_REGIONS,
  mailProvider,
  recordType,
  sesHostIn,
  sesHostRegion,
} from './checkTypes.js'
import { BusyRing, ErrorBanner } from './components.jsx'
import PeoplePicker from './PeoplePicker.jsx'
import { ENVIRONMENTS } from './environments.js'
import { duration, parseEmails, parsePhoneNumbers } from './format.js'
import { useApi } from './useApi.js'
import Select from './Select.jsx'

const NAME_MAX_WORDS = 25

const countWords = (text) => text.trim().split(/\s+/).filter(Boolean).length

const METHODS = ['GET', 'HEAD', 'POST', 'OPTIONS']
const INTERVALS = [30, 60, 120, 300, 600, 900, 1800, 3600, 21600, 86400]
// Each type's default timeout: a ping waits for echo replies, not pages, a
// resolver may have to recurse on a cache miss, and an SMTP server may pause
// before its greeting on purpose.
const TIMEOUTS = { http: 10, ping: 2, dns: 5, mail: 15 }

const BLANK = {
  project_id: '',
  environment: '',
  check_type: 'http',
  name: '',
  url: '',
  method: 'GET',
  expected_status: 200,
  timeout_seconds: TIMEOUTS.http,
  check_interval_seconds: 300,
  max_down_alerts: 4,
  slow_threshold_ms: '',
  retries_on_failure: 1,
  must_contain: '',
  must_not_contain: '',
  // Rows of { name, value, stored, hint }: `stored` is the name the header was
  // saved under, whose value the API keeps when `value` is left blank.
  request_headers: [],
  ping_count: 5,
  packet_loss_threshold_percent: '',
  dns_record_type: 'A',
  // One per line.
  dns_expected_values: '',
  mail_provider: 'resend',
  mail_tls: 'starttls',
  mail_username: '',
  // Secrets are never sent back to us; blank means "keep it" when editing.
  mail_password: '',
  mail_api_key: '',
  // Whose credentials read SES's quota: 'own' (Watchly's), 'key' (this
  // check's own access key) or 'account:<id>' (an infrastructure AWS account).
  mail_quota_source: 'own',
  mail_aws_access_key_id: '',
  mail_aws_secret_access_key: '',
  mail_region: '',
  alert_emails: '',
  inherit_project_recipients: true,
  slack_channel_id: '',
  slack_bot_token: '',
  telegram_chat_id: '',
  telegram_bot_token: '',
  whatsapp_recipients: '',
}

function fromSite(site) {
  return {
    ...BLANK,
    ...site,
    alert_emails: site.alert_emails.join(', '),
    slack_channel_id: site.slack_channel_id ?? '',
    // The stored tokens are never sent back to us; blank means "keep it".
    slack_bot_token: '',
    telegram_chat_id: site.telegram_chat_id ?? '',
    telegram_bot_token: '',
    whatsapp_recipients: site.whatsapp_recipients.join(', '),
    slow_threshold_ms: site.slow_threshold_ms ?? '',
    must_contain: site.must_contain ?? '',
    must_not_contain: site.must_not_contain ?? '',
    // The stored values are never sent back to us either.
    request_headers: (site.request_headers ?? []).map((h) => ({
      name: h.name,
      value: '',
      stored: h.name,
      hint: h.value_hint,
    })),
    packet_loss_threshold_percent: site.packet_loss_threshold_percent ?? '',
    dns_record_type: site.dns_record_type ?? 'A',
    dns_expected_values: (site.dns_expected_values ?? []).join('\n'),
    environment: site.environment ?? '',
    mail_provider: site.mail_provider ?? 'smtp',
    mail_tls: site.mail_tls ?? 'starttls',
    mail_username: site.mail_username ?? '',
    mail_password: '',
    mail_api_key: '',
    mail_quota_source: site.mail_aws_account_id
      ? `account:${site.mail_aws_account_id}`
      : site.mail_aws_access_key_id
        ? 'key'
        : 'own',
    mail_aws_access_key_id: site.mail_aws_access_key_id ?? '',
    mail_aws_secret_access_key: '',
    mail_region: site.mail_region ?? '',
  }
}

// "example.com" is a common thing to type; the API only accepts full URLs.
function withScheme(url) {
  const trimmed = url.trim()
  return /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`
}

// A pasted URL is a common slip for a ping or a DNS check; keep just its host.
function hostOf(text) {
  const trimmed = text.trim()
  if (!/^[a-z][a-z\d+.-]*:\/\//i.test(trimmed)) return trimmed
  try {
    return new URL(trimmed).hostname
  } catch {
    return trimmed
  }
}

// Blank means "use the server-wide default".
const orNull = (value) => (value === '' ? null : Number(value))

const MAX_REQUEST_HEADERS = 10

// A header row still under the name it was saved with: a blank value keeps
// the stored one.
const keepsStoredValue = (header) =>
  Boolean(header.stored) && header.stored.toLowerCase() === header.name.trim().toLowerCase()

// A textarea's non-blank lines.
const lines = (text) =>
  text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)

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

// The same for Telegram, given the project's own Telegram setup.
function telegramHint(project, site) {
  if (site?.telegram_token_hint) {
    return (
      'This site sends with its own bot. Leave the token blank to keep it; ' +
      "clear the chat id to remove this site's Telegram."
    )
  }
  if (!project) {
    return "Leave blank to use the project's Telegram, if it has one. A bot token needs a chat id."
  }
  if (project.telegram_chat_id) {
    const muted = project.telegram_enabled ? '' : ' (currently muted)'
    return (
      `Leave blank to send to ${project.name}'s chat ${project.telegram_chat_id}${muted}. ` +
      "Add a chat id to send elsewhere with the project's bot, or a bot token too to use another bot."
    )
  }
  return null
}

// WhatsApp has no bot of the site's own: its numbers are always sent from the
// project's business number.
function whatsappHint(project) {
  if (!project) {
    return "Leave blank to use the project's WhatsApp numbers, if it has any."
  }
  if (project.whatsapp_phone_number_id) {
    const muted = project.whatsapp_enabled ? '' : ' (currently muted)'
    return (
      `Leave blank to alert ${project.name}'s numbers${muted}. ` +
      "Add numbers to alert them instead, still from the project's business number."
    )
  }
  return (
    `${project.name} has no WhatsApp set up. Add it to the project first: ` +
    "a site's numbers are sent from the project's business number."
  )
}

// A mail check's part of the payload. Secrets left blank are left out, which
// keeps the stored ones; the quota fields of other providers are dropped.
function mailPayload(form) {
  // Without TLS there is no login: the API refuses one.
  const login = form.mail_tls !== 'none'
  const payload = {
    mail_provider: form.mail_provider,
    mail_tls: form.mail_tls,
    mail_username: (login && form.mail_username.trim()) || null,
  }
  if (login && form.mail_password) payload.mail_password = form.mail_password
  if (form.mail_provider === 'resend' && form.mail_api_key.trim()) {
    payload.mail_api_key = form.mail_api_key.trim()
  }
  if (form.mail_provider === 'ses') {
    const source = form.mail_quota_source
    payload.mail_region = form.mail_region.trim() || null
    payload.mail_aws_account_id = source.startsWith('account:') ? Number(source.slice(8)) : null
    payload.mail_aws_access_key_id = source === 'key' ? form.mail_aws_access_key_id.trim() : null
    if (source === 'key' && form.mail_aws_secret_access_key.trim()) {
      payload.mail_aws_secret_access_key = form.mail_aws_secret_access_key.trim()
    }
  }
  return payload
}

// Who runs the SMTP server, how to log in to it, and how to read its quota.
function MailFields({ form, set, initial, pickProvider, pickRegion, accounts, ses, resend }) {
  const login = form.mail_tls !== 'none'
  // An account picked before, which the user may no longer be able to list.
  const linked = form.mail_quota_source.startsWith('account:')
    ? Number(form.mail_quota_source.slice(8))
    : null
  const listed = accounts.some((a) => a.id === linked)
  // What the host names, which the quota is read in when no region is chosen.
  const hostRegion = sesHostRegion(form.url)
  const knownRegion = SES_REGIONS.some(([value]) => value === form.mail_region)
  const keyStored = Boolean(initial?.mail_aws_secret_hint) && Boolean(initial?.mail_aws_access_key_id)
  return (
    <>
      <div className="row-2">
        <label className="field">
          <span>Provider</span>
          <Select value={form.mail_provider} onChange={pickProvider}>
            {MAIL_PROVIDERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </Select>
          <span className="muted small">
            {ses || resend
              ? 'Watchly logs in over SMTP and reads the sending quota from the provider’s API. No mail is sent.'
              : 'Watchly logs in over SMTP and hangs up. No mail is sent.'}
          </span>
        </label>
        <label className="field">
          <span>Encryption</span>
          <Select value={form.mail_tls} onChange={set('mail_tls')}>
            {MAIL_TLS.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </Select>
          {!login && (
            <span className="muted small">
              Without TLS Watchly only greets the server: it never sends a password unencrypted.
            </span>
          )}
        </label>
      </div>

      {login && (
        <div className="row-2">
          <label className="field">
            <span>SMTP user name</span>
            <input
              value={form.mail_username}
              onChange={set('mail_username')}
              placeholder={ses ? 'AKIA… (SMTP credentials)' : 'Blank to only greet the server'}
              maxLength={255}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <label className="field">
            <span>SMTP password</span>
            <input
              type="password"
              value={form.mail_password}
              onChange={set('mail_password')}
              placeholder={
                initial?.mail_password_set
                  ? 'Stored: leave blank to keep it'
                  : resend
                    ? 're_… (an API key)'
                    : ''
              }
              maxLength={1024}
              autoComplete="new-password"
              required={Boolean(form.mail_username.trim()) && !initial?.mail_password_set}
            />
            <span className="muted small">
              {ses
                ? 'SES’s SMTP credentials, not an IAM access key: create them under SES → SMTP settings.'
                : 'Stored encrypted and never shown again.'}
            </span>
          </label>
        </div>
      )}

      {resend && (
        <label className="field">
          <span>API key for the quota (optional)</span>
          <input
            type="password"
            value={form.mail_api_key}
            onChange={set('mail_api_key')}
            placeholder={
              initial?.mail_api_key_hint
                ? `Stored: ${initial.mail_api_key_hint}`
                : 'Blank: use the SMTP password'
            }
            maxLength={1024}
            autoComplete="new-password"
          />
          <span className="muted small">
            Reading the quota needs a full-access key. If the SMTP password is a sending-only key,
            give a full-access one here.
          </span>
        </label>
      )}

      {ses && (
        <div className="row-2">
          <label className="field">
            <span>Read the quota with</span>
            <Select value={form.mail_quota_source} onChange={set('mail_quota_source')}>
              <option value="own">Watchly’s own AWS credentials</option>
              {accounts.map((a) => (
                <option key={a.id} value={`account:${a.id}`}>
                  AWS account: {a.name}
                </option>
              ))}
              {linked && !listed && <option value={form.mail_quota_source}>AWS account #{linked}</option>}
              <option value="key">An access key of this check’s own</option>
            </Select>
            <span className="muted small">Needs the ses:GetAccount permission.</span>
          </label>
          <label className="field">
            <span>SES region</span>
            <Select
              value={form.mail_region}
              onChange={pickRegion}
              // A host that does not name a region leaves nothing to take it from.
              required={!hostRegion}
            >
              <option value="">
                {hostRegion ? `Same as the host (${hostRegion})` : 'Select a region…'}
              </option>
              {form.mail_region && !knownRegion && (
                <option value={form.mail_region}>{form.mail_region}</option>
              )}
              {SES_REGIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label} · {value}
                </option>
              ))}
            </Select>
            <span className="muted small">Where the quota is read; keep it the same as the SMTP host's.</span>
          </label>
        </div>
      )}

      {ses && form.mail_quota_source === 'key' && (
        <div className="row-2">
          <label className="field">
            <span>Access key id</span>
            <input
              value={form.mail_aws_access_key_id}
              onChange={set('mail_aws_access_key_id')}
              placeholder="AKIA…"
              maxLength={128}
              autoComplete="off"
              spellCheck={false}
              required
            />
          </label>
          <label className="field">
            <span>Secret access key</span>
            <input
              type="password"
              value={form.mail_aws_secret_access_key}
              onChange={set('mail_aws_secret_access_key')}
              placeholder={keyStored ? `Stored: ${initial.mail_aws_secret_hint}` : ''}
              maxLength={1024}
              autoComplete="new-password"
              required={
                !keyStored ||
                form.mail_aws_access_key_id.trim() !== (initial?.mail_aws_access_key_id ?? '')
              }
            />
          </label>
        </div>
      )}
    </>
  )
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

  const setHeader = (index, key) => (e) =>
    setForm({
      ...form,
      request_headers: form.request_headers.map((h, i) =>
        i === index ? { ...h, [key]: e.target.value } : h,
      ),
    })
  const addHeader = () =>
    setForm({
      ...form,
      request_headers: [...form.request_headers, { name: '', value: '', stored: null, hint: null }],
    })
  const removeHeader = (index) =>
    setForm({ ...form, request_headers: form.request_headers.filter((_, i) => i !== index) })

  const ping = form.check_type === 'ping'
  const dns = form.check_type === 'dns'
  const mail = form.check_type === 'mail'
  const http = !ping && !dns && !mail
  const ses = mail && form.mail_provider === 'ses'
  const resend = mail && form.mail_provider === 'resend'

  // SES's quota can be read with an infrastructure project's AWS account.
  const awsAccounts = useApi(
    () => (ses ? api.listAwsAccounts().catch(() => null) : null),
    [ses],
  )

  // A provider's SMTP host and user, filled in while the fields are blank or
  // still hold another provider's.
  const presetFor = (provider, current) => {
    const from = mailProvider(current.mail_provider)
    const to = mailProvider(provider)
    const url =
      !current.url.trim() || current.url.trim() === from.host(current.mail_region)
        ? to.host(current.mail_region)
        : current.url
    const username =
      !current.mail_username || current.mail_username === from.username
        ? (to.username ?? '')
        : current.mail_username
    return { mail_provider: provider, url, mail_username: username }
  }

  // Switching type swaps in its default timeout, unless one was typed in.
  const pickType = (type) => {
    const next = {
      ...form,
      check_type: type,
      timeout_seconds:
        Number(form.timeout_seconds) === TIMEOUTS[form.check_type]
          ? TIMEOUTS[type]
          : form.timeout_seconds,
    }
    if (type === 'mail' && !form.url.trim()) {
      // A new mail check starts on its provider's server.
      const provider = mailProvider(form.mail_provider)
      next.url = provider.host(form.mail_region)
      next.mail_username = form.mail_username || (provider.username ?? '')
    }
    setForm(next)
  }

  const pickProvider = (e) => setForm({ ...form, ...presetFor(e.target.value, form) })

  // Choosing an SES region moves an SES host to it, so the two never disagree.
  const pickRegion = (e) => {
    const region = e.target.value
    const url = (region && sesHostIn(form.url, region)) || form.url
    setForm({ ...form, mail_region: region, url })
  }

  // Port 465 means implicit TLS and 587 STARTTLS, as the API assumes too.
  const setMailUrl = (e) => {
    const url = e.target.value
    let tls = form.mail_tls
    if (/:465\s*$/.test(url)) tls = 'implicit'
    else if (/:587\s*$/.test(url) && tls === 'implicit') tls = 'starttls'
    setForm({ ...form, url, mail_tls: tls })
  }

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
  const tgHint = telegramHint(project, initial)
  const tgChat = form.telegram_chat_id.trim()
  const tgToken = form.telegram_bot_token.trim()
  const needsTgToken =
    Boolean(tgChat) &&
    Boolean(project) &&
    !project.telegram_chat_id &&
    !initial?.telegram_token_hint
  // Nothing to send a site's numbers from; still allow clearing ones already saved.
  const noWaSender =
    Boolean(project) && !project.whatsapp_phone_number_id && !form.whatsapp_recipients.trim()

  const intervals = INTERVALS.includes(Number(form.check_interval_seconds))
    ? INTERVALS
    : [...INTERVALS, Number(form.check_interval_seconds)].sort((a, b) => a - b)

  async function submit(event) {
    event.preventDefault()
    const payload = {
      name: form.name.trim(),
      environment: form.environment || null,
      url: http ? withScheme(form.url) : mail ? form.url.trim() : hostOf(form.url),
      method: form.method,
      expected_status: Number(form.expected_status),
      timeout_seconds: Number(form.timeout_seconds),
      check_interval_seconds: Number(form.check_interval_seconds),
      max_down_alerts: Number(form.max_down_alerts),
      slow_threshold_ms: orNull(form.slow_threshold_ms),
      retries_on_failure: Number(form.retries_on_failure),
      // Blank removes the rule; a ping or a DNS check has no body to search.
      must_contain: (http && form.must_contain) || null,
      must_not_contain: (http && form.must_not_contain) || null,
      ping_count: Number(form.ping_count),
      packet_loss_threshold_percent: orNull(form.packet_loss_threshold_percent),
      alert_emails: parseEmails(form.alert_emails),
      inherit_project_recipients: form.inherit_project_recipients,
      // Clearing the channel removes the site's own Slack, token included.
      slack_channel_id: slackChannel || null,
      // Likewise for Telegram.
      telegram_chat_id: tgChat || null,
      // Empty goes back to the project's numbers.
      whatsapp_recipients: parsePhoneNumbers(form.whatsapp_recipients),
    }
    if (dns) {
      payload.dns_record_type = form.dns_record_type
      payload.dns_expected_values = lines(form.dns_expected_values)
    }
    if (http) {
      payload.request_headers = form.request_headers
        .filter((h) => h.name.trim())
        .map((h) => ({
          name: h.name.trim(),
          value: h.value === '' && keepsStoredValue(h) ? null : h.value,
        }))
    }
    if (mail) Object.assign(payload, mailPayload(form))
    if (slackToken) payload.slack_bot_token = slackToken
    if (tgToken) payload.telegram_bot_token = tgToken
    if (creating) {
      payload.check_type = form.check_type
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
    <form className="form" onSubmit={submit} autoComplete="off">
      <ErrorBanner error={error} />

      {/* Fixed once created: each keeps a different history. */}
      {creating && (
        <div className="choices" role="radiogroup" aria-label="What to check">
          {CHECK_TYPES.map((t) => (
            <label key={t.value} className="choice">
              <input
                type="radio"
                name="check_type"
                value={t.value}
                checked={form.check_type === t.value}
                onChange={() => pickType(t.value)}
              />
              <span>
                <strong>{t.label}</strong>
                <span className="muted small">{t.description}</span>
              </span>
            </label>
          ))}
        </div>
      )}

      <div className="row-2">
        {creating && (
          <label className="field">
            <span>Project</span>
            <Select value={form.project_id} onChange={set('project_id')} required>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          </label>
        )}
        <label className="field">
          <span>Environment</span>
          {/* Required for new sites; sites that predate the field can stay unset. */}
          <Select value={form.environment} onChange={set('environment')} required={creating}>
            <option value="">{creating ? 'Select environment…' : 'Not set'}</option>
            {ENVIRONMENTS.map((env) => (
              <option key={env.value} value={env.value}>
                {env.label}
              </option>
            ))}
          </Select>
        </label>
      </div>

      <div className="row-2">
        <label className="field">
          <span>Name</span>
          <input
            value={form.name}
            onChange={(e) => {
              // Native validation blocks the submit and points at this field.
              e.target.setCustomValidity(
                countWords(e.target.value) > NAME_MAX_WORDS
                  ? `The name can be at most ${NAME_MAX_WORDS} words.`
                  : '',
              )
              set('name')(e)
            }}
            placeholder={
              ping
                ? 'Core router'
                : dns
                  ? 'Mail servers (MX)'
                  : mail
                    ? 'Transactional mail'
                    : 'Marketing site'
            }
            required
            maxLength={255}
          />
        </label>
        <label className="field">
          <span>
            {ping ? 'Host or IP address' : dns ? 'Domain' : mail ? 'SMTP server' : 'URL'}
          </span>
          <input
            value={form.url}
            onChange={mail ? setMailUrl : set('url')}
            placeholder={
              ping
                ? '203.0.113.10 or server.example.com'
                : dns
                  ? 'example.com or _dmarc.example.com'
                  : mail
                    ? 'smtp.example.com:587'
                    : 'https://example.com/'
            }
            required
          />
        </label>
      </div>

      {mail && (
        <MailFields
          form={form}
          set={set}
          initial={initial}
          pickProvider={pickProvider}
          pickRegion={pickRegion}
          accounts={awsAccounts.data?.items ?? []}
          ses={ses}
          resend={resend}
        />
      )}

      {dns && (
        <div className="row-2">
          <label className="field">
            <span>Record type</span>
            <Select value={form.dns_record_type} onChange={set('dns_record_type')}>
              {RECORD_TYPES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </Select>
            <span className="muted small">
              Looked up at several public resolvers at once, so a change seen by only some of them
              shows.
              {initial && form.dns_record_type !== initial.dns_record_type &&
                ' Changing it forgets the records learned so far.'}
            </span>
          </label>
          <label className="field">
            <span>Expected values (optional)</span>
            <textarea
              rows={3}
              value={form.dns_expected_values}
              onChange={set('dns_expected_values')}
              placeholder={`One per line, e.g. ${recordType(form.dns_record_type).example}`}
            />
            <span className="muted small">
              Any other answer from any resolver counts as down. Leave blank to learn the records
              and be alerted when they change.
            </span>
          </label>
        </div>
      )}

      <div className="row-3">
        <label className="field">
          <span>Check every</span>
          <Select value={form.check_interval_seconds} onChange={set('check_interval_seconds')}>
            {intervals.map((s) => (
              <option key={s} value={s}>
                {duration(s)}
              </option>
            ))}
          </Select>
        </label>
        <label className="field">
          <span>
            {ping
              ? 'Reply timeout (seconds)'
              : dns
                ? 'Resolver timeout (seconds)'
                : 'Timeout (seconds)'}
          </span>
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

      {ping && (
        <details className="advanced">
          <summary>Ping options</summary>
          <div className="row-3">
            <label className="field">
              <span>Pings per check</span>
              <input
                type="number"
                min={1}
                max={20}
                value={form.ping_count}
                onChange={set('ping_count')}
                required
              />
              <span className="muted small">
                Sent half a second apart. More pings measure packet loss more finely.
              </span>
            </label>
            <label className="field">
              <span>Packet loss alert at (%)</span>
              <input
                type="number"
                min={1}
                max={99}
                value={form.packet_loss_threshold_percent}
                onChange={set('packet_loss_threshold_percent')}
                placeholder="Server default"
              />
              <span className="muted small">
                Alert when the host answers but keeps losing at least this share of pings. Losing
                them all counts as down.
              </span>
            </label>
            <label className="field">
              <span>Latency alert above (ms)</span>
              <input
                type="number"
                min={1}
                max={120000}
                value={form.slow_threshold_ms}
                onChange={set('slow_threshold_ms')}
                placeholder="Server default"
              />
              <span className="muted small">
                Alert when the average round trip stays above this.
              </span>
            </label>
          </div>
          <div className="row-3">
            <label className="field">
              <span>Retries before failing</span>
              <input
                type="number"
                min={0}
                max={3}
                value={form.retries_on_failure}
                onChange={set('retries_on_failure')}
                required
              />
              <span className="muted small">
                When no ping is answered, try again a few seconds later before alerting. 0 alerts
                at once.
              </span>
            </label>
          </div>
        </details>
      )}

      {dns && (
        <details className="advanced">
          <summary>DNS options</summary>
          <div className="row-3">
            <label className="field">
              <span>Slow answer alert above (ms)</span>
              <input
                type="number"
                min={1}
                max={120000}
                value={form.slow_threshold_ms}
                onChange={set('slow_threshold_ms')}
                placeholder="Server default"
              />
              <span className="muted small">
                Alert when the resolvers&apos; average answer time stays above this.
              </span>
            </label>
            <label className="field">
              <span>Retries before failing</span>
              <input
                type="number"
                min={0}
                max={3}
                value={form.retries_on_failure}
                onChange={set('retries_on_failure')}
                required
              />
              <span className="muted small">
                When the record fails or is wrong, look again a few seconds later before
                alerting. 0 alerts at once.
              </span>
            </label>
          </div>
        </details>
      )}

      {mail && (
        <details className="advanced">
          <summary>Mail options</summary>
          <div className="row-3">
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
                Alert when connecting, the greeting and the login together stay slower than this.
              </span>
            </label>
            <label className="field">
              <span>Retries before failing</span>
              <input
                type="number"
                min={0}
                max={3}
                value={form.retries_on_failure}
                onChange={set('retries_on_failure')}
                required
              />
              <span className="muted small">
                Try again a few seconds later before alerting. A refused login is never retried,
                so a wrong password does not lock the account. 0 alerts at once.
              </span>
            </label>
          </div>
        </details>
      )}

      {http && (
        <details className="advanced">
          <summary>Request options</summary>
          <div className="row-3">
            <label className="field">
              <span>Method</span>
              <Select value={form.method} onChange={set('method')}>
                {METHODS.map((m) => (
                  <option key={m}>{m}</option>
                ))}
              </Select>
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
          <div className="row-3">
            <label className="field">
              <span>Retries before failing</span>
              <input
                type="number"
                min={0}
                max={3}
                value={form.retries_on_failure}
                onChange={set('retries_on_failure')}
                required
              />
              <span className="muted small">
                A failed check is repeated a few seconds later, so a one-off blip stays quiet. 0
                alerts on the first failure.
              </span>
            </label>
            <label className="field">
              <span>Response contains (optional)</span>
              <input
                value={form.must_contain}
                onChange={set('must_contain')}
                maxLength={255}
                placeholder="e.g. Add to cart"
              />
            </label>
            <label className="field">
              <span>Response does not contain (optional)</span>
              <input
                value={form.must_not_contain}
                onChange={set('must_not_contain')}
                maxLength={255}
                placeholder="e.g. Service unavailable"
              />
              <span className="muted small">
                Leave blank to skip. When set, the page text is searched on every check
                (case-sensitive, first 1 MB, GET or POST only).
              </span>
            </label>
          </div>
          <div className="field">
            <span>Request headers</span>
            {form.request_headers.map((h, i) => (
              <div key={i} className="header-row">
                <input
                  value={h.name}
                  onChange={setHeader(i, 'name')}
                  placeholder="Authorization"
                  aria-label="Header name"
                  maxLength={100}
                  required={Boolean(h.value)}
                  autoComplete="off"
                  spellCheck={false}
                />
                <input
                  value={h.value}
                  onChange={setHeader(i, 'value')}
                  placeholder={
                    keepsStoredValue(h) ? `Stored: ${h.hint ?? 'empty'}` : 'Bearer …'
                  }
                  aria-label="Header value"
                  maxLength={1024}
                  required={Boolean(h.name.trim()) && !keepsStoredValue(h)}
                  autoComplete="off"
                  spellCheck={false}
                />
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => removeHeader(i)}
                  aria-label={`Remove the ${h.name || 'new'} header`}
                >
                  Remove
                </button>
              </div>
            ))}
            {form.request_headers.length < MAX_REQUEST_HEADERS && (
              <button type="button" className="btn btn-sm header-add" onClick={addHeader}>
                Add header
              </button>
            )}
            <span className="muted small">
              Sent with every check, e.g. a token for a page behind a login, or your own User-Agent.
              Values are stored encrypted and never shown again; leave a stored one blank to keep
              it. They follow redirects, so only add secrets for a URL you trust to stay on its
              own host.
            </span>
          </div>
        </details>
      )}

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
            <PeoplePicker users={users} selected={recipientIds} onChange={setRecipientIds} />
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
              autoComplete="new-password"
              required={needsToken}
            />
          </label>
        </div>
      </fieldset>

      <fieldset className="fieldset">
        <legend>Telegram alerts (optional)</legend>
        {tgHint && <p className="muted small">{tgHint}</p>}
        <div className="row-2">
          <label className="field">
            <span>Chat id</span>
            <input
              value={form.telegram_chat_id}
              onChange={set('telegram_chat_id')}
              placeholder={project?.telegram_chat_id ?? '-1001234567890 or @channel'}
              maxLength={64}
              required={Boolean(tgToken)}
            />
          </label>
          <label className="field">
            <span>Bot token</span>
            <input
              type="password"
              value={form.telegram_bot_token}
              onChange={set('telegram_bot_token')}
              placeholder={
                initial?.telegram_token_hint
                  ? `Stored: ${initial.telegram_token_hint}`
                  : project?.telegram_chat_id
                    ? "Project's bot"
                    : '123456789:AA…'
              }
              autoComplete="new-password"
              required={needsTgToken}
            />
          </label>
        </div>
      </fieldset>

      <fieldset className="fieldset">
        <legend>WhatsApp alerts (optional)</legend>
        <p className="muted small">{whatsappHint(project)}</p>
        <label className="field">
          <span>Numbers to alert, with country code</span>
          <input
            value={form.whatsapp_recipients}
            onChange={set('whatsapp_recipients')}
            placeholder={project?.whatsapp_recipients?.join(', ') || '+8801712345678'}
            disabled={noWaSender}
          />
        </label>
      </fieldset>

      <div className="form-actions">
        <button className="btn btn-primary" disabled={busy}>
          <BusyRing busy={busy} failed={!!error} />
          {busy ? 'Saving…' : submitLabel}
        </button>
        {onCancel && (
          <button type="button" className="btn btn-danger-solid" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </form>
  )
}
