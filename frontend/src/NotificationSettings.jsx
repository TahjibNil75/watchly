import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { ErrorBanner, Loading } from './components.jsx'
import { useApi } from './useApi.js'

const FIELDS = ['email_enabled', 'slack_enabled', 'telegram_enabled', 'subject', 'body']

const pickDraft = (setting) => Object.fromEntries(FIELDS.map((f) => [f, setting[f]]))

// What to send on save. A field the user did not change keeps whatever this
// level already stores (usually "inherit"), so opening a card and pressing
// Save never turns inherited wording into a copy that stops following it.
function buildPayload(setting, draft) {
  const out = {}
  for (const field of FIELDS) {
    if (draft[field] === setting[field]) {
      out[field] = setting.overrides[field]
    } else {
      out[field] = typeof draft[field] === 'string' ? draft[field].trim() || null : draft[field]
    }
  }
  return out
}

const isDirty = (setting, draft) => FIELDS.some((f) => draft[f] !== setting[f])
const hasOverrides = (setting) => FIELDS.some((f) => setting.overrides[f] !== null)

const SOURCE_HINT = {
  project: 'Set for this project',
  global: 'From the global settings',
  default: 'Built-in wording',
}

// ---------------------------------------------------------------------------
// Slack preview: just enough of Block Kit and mrkdwn to draw our own messages.
// ---------------------------------------------------------------------------

const EMOJI = {
  ':rotating_light:': '🚨',
  ':warning:': '⚠️',
  ':white_check_mark:': '✅',
  ':lock:': '🔒',
  ':hourglass_flowing_sand:': '⏳',
  ':bar_chart:': '📊',
  ':large_green_circle:': '🟢',
  ':large_yellow_circle:': '🟡',
  ':red_circle:': '🔴',
  ':large_blue_circle:': '🔵',
}

function slackText(raw) {
  return raw
    .replace(/<!(channel|here|everyone)>/g, '@$1')
    .replace(/<@[A-Z0-9]+>/g, '@someone')
    .replace(/:[a-z_]+:/g, (code) => EMOJI[code] ?? code)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
}

function Mrkdwn({ text }) {
  return slackText(text)
    .split(/(\*[^*\n]+\*|@(?:channel|here|everyone|someone))/g)
    .map((part, i) => {
      if (/^@(channel|here|everyone|someone)$/.test(part)) {
        return (
          <span key={i} className="sp-mention">
            {part}
          </span>
        )
      }
      return part.length > 2 && part.startsWith('*') && part.endsWith('*') ? (
        <strong key={i}>{part.slice(1, -1)}</strong>
      ) : (
        part
      )
    })
}

function SlackBlock({ block }) {
  switch (block.type) {
    case 'header':
      return (
        <div className="sp-header">
          <Mrkdwn text={block.text.text} />
        </div>
      )
    case 'section':
      return (
        <div className="sp-section">
          {block.text && (
            <div className="sp-text">
              <Mrkdwn text={block.text.text} />
            </div>
          )}
          {block.fields && (
            <div className="sp-fields">
              {block.fields.map((f, i) => (
                <div key={i} className="sp-text">
                  <Mrkdwn text={f.text} />
                </div>
              ))}
            </div>
          )}
        </div>
      )
    case 'context':
      return (
        <div className="sp-context">
          {block.elements.map((e, i) => (
            <span key={i}>
              <Mrkdwn text={e.text} />
            </span>
          ))}
        </div>
      )
    case 'divider':
      return <hr className="sp-divider" />
    case 'actions':
      return (
        <div className="sp-actions">
          {block.elements.map((e, i) => (
            <a key={i} href={e.url} target="_blank" rel="noreferrer noopener" className="sp-button">
              {e.text.text}
            </a>
          ))}
        </div>
      )
    default:
      return null
  }
}

function SlackPreview({ blocks }) {
  return (
    <div className="slack-preview">
      <div className="sp-app">
        <span className="sp-avatar" aria-hidden="true">
          W
        </span>
        <strong>Watchly</strong>
        <span className="muted small">APP</span>
      </div>
      {blocks.map((block, i) => (
        <SlackBlock key={i} block={block} />
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Telegram preview: just enough of Telegram's HTML to draw our own messages —
// <b>, <i> and <a href>, with &-entities — as elements, never as raw HTML.
// ---------------------------------------------------------------------------

const TG_TAG = /(<\/?(?:b|i|a)(?: href="[^"]*")?>)/

function decodeEntities(text) {
  return text
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, '&')
}

function parseTelegram(html) {
  const root = { children: [] }
  const stack = [root]
  for (const part of html.split(TG_TAG)) {
    const open = part.match(/^<(b|i|a)(?: href="([^"]*)")?>$/)
    if (open) {
      const node = { tag: open[1], href: open[2] && decodeEntities(open[2]), children: [] }
      stack.at(-1).children.push(node)
      stack.push(node)
    } else if (/^<\/(b|i|a)>$/.test(part)) {
      if (stack.length > 1) stack.pop()
    } else if (part) {
      stack.at(-1).children.push(decodeEntities(part))
    }
  }
  return root.children
}

function TelegramNodes({ nodes }) {
  return nodes.map((node, i) => {
    if (typeof node === 'string') return node
    const inner = <TelegramNodes nodes={node.children} />
    if (node.tag === 'b') return <strong key={i}>{inner}</strong>
    if (node.tag === 'i') return <em key={i}>{inner}</em>
    return /^https?:\/\//i.test(node.href ?? '') ? (
      <a key={i} href={node.href} target="_blank" rel="noreferrer noopener">
        {inner}
      </a>
    ) : (
      <span key={i}>{inner}</span>
    )
  })
}

function TelegramPreview({ html }) {
  return (
    <div className="telegram-preview">
      <div className="tg-bubble">
        <div className="tg-name">Watchly</div>
        <div className="tg-text">
          <TelegramNodes nodes={parseTelegram(html)} />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Live preview, rendered by the API exactly as it would be sent.
// ---------------------------------------------------------------------------

const TAB_LABELS = { email: 'Email', slack: 'Slack', telegram: 'Telegram' }

function Preview({ kind, subject, body }) {
  const [tab, setTab] = useState('email')
  const [state, setState] = useState({ data: null, error: null })

  // Debounced, and only the latest response may land.
  useEffect(() => {
    let live = true
    const timer = setTimeout(async () => {
      try {
        const data = await api.previewNotification({
          kind,
          subject: subject.trim() || null,
          body: body.trim() || null,
        })
        if (live) setState({ data, error: null })
      } catch (error) {
        if (live) setState((prev) => ({ ...prev, error }))
      }
    }, 350)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [kind, subject, body])

  const { data, error } = state
  return (
    <div className="preview">
      <div className="tabs" role="tablist">
        {['email', 'slack', 'telegram'].map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? 'tab active' : 'tab'}
            onClick={() => setTab(name)}
          >
            {TAB_LABELS[name]}
          </button>
        ))}
        <span className="muted small tabs-note">Preview with sample data</span>
      </div>
      <ErrorBanner error={error} />
      {!data ? (
        !error && <Loading label="Rendering preview…" />
      ) : tab === 'email' ? (
        <>
          <p className="muted small">
            Subject: <strong>{data.subject}</strong>
          </p>
          <iframe
            title="Email preview"
            className="preview-frame"
            sandbox=""
            srcDoc={data.email_html}
          />
        </>
      ) : tab === 'slack' ? (
        <SlackPreview blocks={data.slack_blocks} />
      ) : (
        <TelegramPreview html={data.telegram_html} />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// One notification kind
// ---------------------------------------------------------------------------

function KindEditor({ setting, scope, canEdit, channels, onSave, onReset }) {
  const [draft, setDraft] = useState(() => pickDraft(setting))
  const [seen, setSeen] = useState(setting)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const subjectRef = useRef(null)
  const bodyRef = useRef(null)
  const lastFocused = useRef('body')

  // A saved or reset setting arrives as a new object: start the draft over.
  // (Adjusting state while rendering is React's way to reset it on a prop change.)
  if (seen !== setting) {
    setSeen(setting)
    setDraft(pickDraft(setting))
  }

  const dirty = isDirty(setting, draft)
  const customized = FIELDS.some((f) => setting.sources[f] === scope)
  const inherits = scope === 'project' && !customized && FIELDS.some((f) => setting.sources[f] === 'global')
  const hasChannel = (name) => !channels || channels.includes(name)

  async function run(action) {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const save = () => run(() => onSave(setting.kind, buildPayload(setting, draft)))
  const reset = () => {
    const to = scope === 'project' ? 'inherit the global wording again' : 'go back to the built-in wording'
    if (!window.confirm(`Reset “${setting.label}”? It will ${to}.`)) return
    run(() => onReset(setting.kind))
  }

  function insertPlaceholder(name) {
    const field = lastFocused.current
    const el = field === 'subject' ? subjectRef.current : bodyRef.current
    const token = `{{${name}}}`
    const text = draft[field]
    const start = el?.selectionStart ?? text.length
    const end = el?.selectionEnd ?? start
    setDraft({ ...draft, [field]: text.slice(0, start) + token + text.slice(end) })
    requestAnimationFrame(() => {
      el?.focus()
      el?.setSelectionRange(start + token.length, start + token.length)
    })
  }

  const toggle = (field, label, channel) => (
    <label className="check">
      <input
        type="checkbox"
        checked={draft[field]}
        disabled={!canEdit || busy}
        onChange={(e) => setDraft({ ...draft, [field]: e.target.checked })}
      />
      <span>
        {label}
        {!hasChannel(channel) && <span className="muted small"> (not set up)</span>}
      </span>
    </label>
  )

  return (
    <div className="notif">
      <div className="notif-head">
        <div>
          <h3>
            {setting.label}
            {customized && <span className="badge badge-unknown">customized</span>}
            {inherits && <span className="badge badge-paused">global wording</span>}
          </h3>
          <p className="muted small">{setting.description}</p>
        </div>
        <div className="notif-toggles">
          {toggle('email_enabled', 'Email', 'email')}
          {toggle('slack_enabled', 'Slack', 'slack')}
          {toggle('telegram_enabled', 'Telegram', 'telegram')}
        </div>
      </div>

      <details className="advanced" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
        <summary>{canEdit ? 'Customize wording and preview' : 'View wording and preview'}</summary>

        <p className="muted small">Sent to: {setting.audience}</p>

        <label className="field">
          <span>Subject</span>
          <input
            ref={subjectRef}
            value={draft.subject}
            readOnly={!canEdit}
            maxLength={255}
            placeholder={setting.inherited_subject}
            onFocus={() => (lastFocused.current = 'subject')}
            onChange={(e) => setDraft({ ...draft, subject: e.target.value })}
          />
          <span className="muted small">
            {SOURCE_HINT[setting.sources.subject]}
            {canEdit && '. Clear it to inherit again.'}
          </span>
        </label>

        <label className="field">
          <span>Message</span>
          <textarea
            ref={bodyRef}
            rows={4}
            value={draft.body}
            readOnly={!canEdit}
            maxLength={4000}
            placeholder={setting.inherited_body}
            onFocus={() => (lastFocused.current = 'body')}
            onChange={(e) => setDraft({ ...draft, body: e.target.value })}
          />
          <span className="muted small">
            {SOURCE_HINT[setting.sources.body]}. Plain text; line breaks are kept. In Slack,{' '}
            <code>*bold*</code> and mentions like <code>&lt;!channel&gt;</code> work; Telegram
            shows it as written.
          </span>
        </label>

        {canEdit && (
          <div className="field">
            <span>Insert a placeholder</span>
            <div className="placeholders">
              {Object.entries(setting.placeholders).map(([name, description]) => (
                <button
                  key={name}
                  type="button"
                  className="placeholder-chip"
                  title={description}
                  // Keep the caret in the field the chip is about to write to.
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => insertPlaceholder(name)}
                >
                  {`{{${name}}}`}
                </button>
              ))}
            </div>
          </div>
        )}

        {open && (
          <Preview
            kind={setting.kind}
            subject={draft.subject || setting.inherited_subject}
            body={draft.body || setting.inherited_body}
          />
        )}
      </details>

      <ErrorBanner error={error} />

      {canEdit && (dirty || hasOverrides(setting)) && (
        <div className="notif-actions">
          {dirty && (
            <>
              <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={busy}>
                {busy ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => setDraft(pickDraft(setting))}
                disabled={busy}
              >
                Discard changes
              </button>
              <span className="muted small">Unsaved changes</span>
            </>
          )}
          {!dirty && hasOverrides(setting) && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={reset} disabled={busy}>
              {scope === 'project' ? 'Reset to global settings' : 'Reset to built-in wording'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * Every notification kind at one level: `projectId` for a project's own
 * settings, none for the global defaults. `channels` (a project's configured
 * channels) only annotates the switches — a switch for a channel that is not
 * set up is kept but has no effect.
 */
export default function NotificationSettings({ projectId, canEdit, channels }) {
  const scope = projectId ? 'project' : 'global'
  const list = useApi(
    () => (projectId ? api.projectNotificationSettings(projectId) : api.notificationSettings()),
    [projectId],
  )

  if (list.loading) return <Loading />
  if (!list.data) return <ErrorBanner error={list.error} />

  const replace = (updated) =>
    list.setData({
      items: list.data.items.map((item) => (item.kind === updated.kind ? updated : item)),
    })

  const save = async (kind, payload) =>
    replace(
      await (projectId
        ? api.saveProjectNotification(projectId, kind, payload)
        : api.saveNotification(kind, payload)),
    )
  const reset = async (kind) =>
    replace(
      await (projectId ? api.resetProjectNotification(projectId, kind) : api.resetNotification(kind)),
    )

  return (
    <div>
      <ErrorBanner error={list.error} />
      {list.data.items.map((setting) => (
        <KindEditor
          key={setting.kind}
          setting={setting}
          scope={scope}
          canEdit={canEdit}
          channels={channels}
          onSave={save}
          onReset={reset}
        />
      ))}
    </div>
  )
}
