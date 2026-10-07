import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import ChannelLogo from './ChannelLogo.jsx'
import { CHANNELS } from './channels.js'
import { ErrorBanner, Loading } from './components.jsx'
import { useApi } from './useApi.js'

// Each kind's colour, icon (24x24 strokes, like the dashboard's) and group. A
// kind missing here still shows, under "Other".
const KIND_META = {
  down: { tone: 'down', group: 'outages', icon: <path d="M12 3.5 2.5 20h19zM12 10v4M12 17v.01" /> },
  still_down: {
    tone: 'down',
    group: 'outages',
    icon: <path d="m17 2 4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3" />,
  },
  recovered: {
    tone: 'up',
    group: 'outages',
    icon: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="m8 12.5 2.8 2.8L16.5 9.5" />
      </>
    ),
  },
  ssl_expiring: {
    tone: 'pending',
    group: 'warnings',
    icon: (
      <>
        <rect x="4.5" y="10.5" width="15" height="10" rx="2" />
        <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" />
      </>
    ),
  },
  domain_expiring: {
    tone: 'pending',
    group: 'warnings',
    icon: (
      <>
        <rect x="3.5" y="5" width="17" height="15.5" rx="2" />
        <path d="M3.5 10h17M8 3v4M16 3v4M12 13.5v3" />
      </>
    ),
  },
  nameservers_changed: {
    tone: 'maintenance',
    group: 'warnings',
    icon: (
      <>
        <rect x="3.5" y="4" width="17" height="6.5" rx="1.5" />
        <rect x="3.5" y="13.5" width="17" height="6.5" rx="1.5" />
        <path d="M7.5 7.25h.01M7.5 16.75h.01M11 7.25h5.5M11 16.75h5.5" />
      </>
    ),
  },
  slow_response: {
    tone: 'pending',
    group: 'warnings',
    icon: <path d="M4.5 18a8.5 8.5 0 1 1 15 0M12 14l4-4" />,
  },
  packet_loss: { tone: 'pending', group: 'warnings', icon: <path d="M5 20v-4M10 20v-8M15 20v-3M20 20V5" /> },
  dns_changed: {
    tone: 'maintenance',
    group: 'warnings',
    icon: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
      </>
    ),
  },
  monthly_report: { tone: 'info', group: 'reports', icon: <path d="M4 20h16M7 16v-5M12 16V7M17 16v-8" /> },
  infra_down: {
    tone: 'down',
    group: 'infrastructure',
    icon: (
      <>
        <rect x="3.5" y="4" width="17" height="6.5" rx="1.5" />
        <rect x="3.5" y="13.5" width="17" height="6.5" rx="1.5" />
        <path d="M7.5 7.25h.01M7.5 16.75h.01M14 15l4 4M18 15l-4 4" />
      </>
    ),
  },
  infra_still_down: {
    tone: 'down',
    group: 'infrastructure',
    icon: <path d="m17 2 4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3" />,
  },
  infra_recovered: {
    tone: 'up',
    group: 'infrastructure',
    icon: (
      <>
        <rect x="3.5" y="4" width="17" height="6.5" rx="1.5" />
        <rect x="3.5" y="13.5" width="17" height="6.5" rx="1.5" />
        <path d="m13.5 17 2 2 4-4" />
      </>
    ),
  },
  infra_degraded: {
    tone: 'pending',
    group: 'infrastructure',
    icon: (
      <>
        <circle cx="12" cy="5" r="2.5" />
        <circle cx="5" cy="19" r="2.5" />
        <circle cx="19" cy="19" r="2.5" />
        <path d="M12 7.5v4M10.5 9.5 6 16.8M13.5 9.5 18 16.8M12 15v1.5M12 19.5v.01" />
      </>
    ),
  },
  asg_scaled_out: {
    tone: 'pending',
    group: 'infrastructure',
    icon: <path d="M12 19V5M5 12l7-7 7 7" />,
  },
  asg_scaled_in: {
    tone: 'pending',
    group: 'infrastructure',
    icon: <path d="M12 5v14M19 12l-7 7-7-7" />,
  },
  vpc_unreachable: {
    tone: 'down',
    group: 'infrastructure',
    icon: (
      <>
        <rect x="3" y="3" width="18" height="18" rx="3" />
        <path d="m9 9 6 6M15 9l-6 6" />
      </>
    ),
  },
  vpc_recovered: {
    tone: 'up',
    group: 'infrastructure',
    icon: (
      <>
        <rect x="3" y="3" width="18" height="18" rx="3" />
        <path d="m8 12.5 2.8 2.8L16.5 9.5" />
      </>
    ),
  },
  // A gauge close to its end: Elastic IPs left loose, or the quota nearly used.
  account_capacity: {
    tone: 'pending',
    group: 'infrastructure',
    icon: (
      <>
        <path d="M4 16a8 8 0 1 1 16 0" />
        <path d="M12 16l4.5-5" />
        <path d="M4 20h16" />
      </>
    ),
  },
  deploy_started: {
    tone: 'maintenance',
    group: 'deployments',
    icon: <path d="M12 3c3 2 4.5 5 4.5 9l-2 3h-5l-2-3c0-4 1.5-7 4.5-9zM9.5 15 7 18l3-.5M14.5 15l2.5 3-3-.5M12 21v-3M12 9.5h.01" />,
  },
  deploy_finished: {
    tone: 'up',
    group: 'deployments',
    icon: (
      <>
        <path d="M4 12.5 9 17.5 20 6.5" />
        <path d="M4 20h16" />
      </>
    ),
  },
  docker_container_down: {
    tone: 'down',
    group: 'docker',
    icon: (
      <>
        <rect x="3" y="7" width="13" height="10" rx="1.5" />
        <path d="M7 10v4M10 10v4M18.5 9.5l3 3M21.5 9.5l-3 3" />
      </>
    ),
  },
  docker_container_recovered: {
    tone: 'up',
    group: 'docker',
    icon: (
      <>
        <rect x="3" y="7" width="13" height="10" rx="1.5" />
        <path d="M7 10v4M10 10v4M17.5 12l2 2 3-4" />
      </>
    ),
  },
  docker_container_unhealthy: {
    tone: 'pending',
    group: 'docker',
    icon: <path d="M3 12h4l2-5 4 10 2-5h6" />,
  },
  docker_container_oom: {
    tone: 'down',
    group: 'docker',
    icon: (
      <>
        <rect x="4" y="5" width="16" height="14" rx="2" />
        <path d="M8 9v6M12 9v6M16 9v6M2 9h2M2 15h2M20 9h2M20 15h2" />
      </>
    ),
  },
  docker_restart_loop: {
    tone: 'down',
    group: 'docker',
    icon: <path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v4h-4" />,
  },
  docker_resource_high: {
    tone: 'pending',
    group: 'docker',
    icon: <path d="M12 3c1 3 4 5 4 9a4 4 0 0 1-8 0c0-2 1-3 2-4 0 2 1 3 2 3 0-3-1-5 0-8z" />,
  },
  docker_host_offline: {
    tone: 'down',
    group: 'docker',
    icon: (
      <>
        <rect x="3.5" y="5" width="17" height="11" rx="1.5" />
        <path d="M8 20h8M12 16v4M9.5 8.5l5 5M14.5 8.5l-5 5" />
      </>
    ),
  },
  docker_host_recovered: {
    tone: 'up',
    group: 'docker',
    icon: (
      <>
        <rect x="3.5" y="5" width="17" height="11" rx="1.5" />
        <path d="M8 20h8M12 16v4M9 10.5l2 2 4-4" />
      </>
    ),
  },
}

const GROUPS = [
  { key: 'outages', title: 'Outages', note: 'When a site stops answering, and when it answers again.' },
  { key: 'warnings', title: 'Early warnings', note: 'Trouble spotted while a site is still up.' },
  { key: 'reports', title: 'Reports', note: 'How each project did last month, sent on the 1st.' },
  {
    key: 'infrastructure',
    title: 'Infrastructure',
    note: 'AWS servers, load balancers and databases: one alert per resource, one per VPC when the whole VPC is lost, and an account’s Elastic IPs when it watches them.',
  },
  {
    key: 'deployments',
    title: 'Deployments',
    note: 'AWS CodeDeploy, for accounts that watch it: down alerts pause while a deployment runs.',
  },
  {
    key: 'docker',
    title: 'Docker',
    note: 'Containers on Docker hosts, from their agents: one alert per container, and one per host when its agent stops reporting.',
  },
  { key: 'other', title: 'Other' },
]

const groupOf = (kind) => KIND_META[kind]?.group ?? 'other'

// Which kind of project each group's notifications come from; the rest are
// websites'. `other` shows everywhere.
const GROUP_MONITORS = { infrastructure: 'infrastructure', deployments: 'infrastructure', docker: 'docker' }
const groupShows = (group, monitors) =>
  !monitors || group.key === 'other' || (GROUP_MONITORS[group.key] ?? 'websites') === monitors

// The channels a kind can go out on, in CHANNELS order, with their switch.
const TOGGLES = CHANNELS.filter((ch) => ch.id !== 'webhook').map((ch) => ({
  ...ch,
  field: `${ch.id}_enabled`,
}))

const FIELDS = [
  'email_enabled',
  'slack_enabled',
  'telegram_enabled',
  'whatsapp_enabled',
  'subject',
  'body',
]

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

// The logo as the channel fetches it; until that URL answers (it is served
// from GitHub once pushed), the copy bundled with the app.
function LogoImage({ src, alt, className }) {
  const [failed, setFailed] = useState(false)
  return (
    <img
      src={failed || !src ? '/favicon.svg' : src}
      alt={alt ?? 'Watchly'}
      className={className}
      onError={() => setFailed(true)}
    />
  )
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
          {block.elements.map((e, i) =>
            e.type === 'image' ? (
              <LogoImage key={i} src={e.image_url} alt={e.alt_text} className="sp-context-img" />
            ) : (
              <span key={i}>
                <Mrkdwn text={e.text} />
              </span>
            ),
          )}
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
          <img src="/favicon.svg" alt="" />
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

function TelegramPreview({ html, logo }) {
  return (
    <div className="telegram-preview">
      <div className="tg-bubble">
        <div className="tg-name">Watchly</div>
        {logo && (
          <div className="tg-logo-preview">
            <LogoImage src={logo} />
          </div>
        )}
        <div className="tg-text">
          <TelegramNodes nodes={parseTelegram(html)} />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// WhatsApp preview: the text as sent, with WhatsApp's *bold* and _italic_ —
// which, as in WhatsApp, only count at the edges of words.
// ---------------------------------------------------------------------------

const WA_FORMAT = /((?<![\w*])\*[^*\n]+\*(?![\w*])|(?<!\w)_[^_\n]+_(?!\w))/

function WhatsAppPreview({ text }) {
  // split() with a capturing group puts each match at an odd index.
  const parts = text.split(WA_FORMAT)
  return (
    <div className="whatsapp-preview">
      <div className="wa-bubble">
        {parts.map((part, i) =>
          i % 2 === 0 ? (
            part
          ) : part[0] === '*' ? (
            <strong key={i}>{part.slice(1, -1)}</strong>
          ) : (
            <em key={i}>{part.slice(1, -1)}</em>
          ),
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Live preview, rendered by the API exactly as it would be sent.
// ---------------------------------------------------------------------------

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
        {TOGGLES.map((ch) => (
          <button
            key={ch.id}
            type="button"
            role="tab"
            aria-selected={tab === ch.id}
            className={tab === ch.id ? 'tab active' : 'tab'}
            onClick={() => setTab(ch.id)}
          >
            <ChannelLogo channel={ch.id} />
            {ch.name}
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
      ) : tab === 'telegram' ? (
        <TelegramPreview html={data.telegram_html} logo={data.logo_url} />
      ) : (
        <WhatsAppPreview text={data.whatsapp_text} />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// One notification kind
// ---------------------------------------------------------------------------

function KindEditor({ setting, scope, canEdit, channels, titleTag: Title, onSave, onReset }) {
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

  const meta = KIND_META[setting.kind] ?? { tone: 'muted', icon: <circle cx="12" cy="12" r="9" /> }
  // A switch for a channel the project has not set up sends nothing, so it
  // does not count.
  const usable = TOGGLES.filter((ch) => hasChannel(ch.id))
  const on = usable.filter((ch) => draft[ch.field]).length

  return (
    <div className={`notif tone-${meta.tone}`}>
      <div className="notif-head">
        <span className="notif-icon" aria-hidden="true">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.9"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            {meta.icon}
          </svg>
        </span>
        <div className="notif-title">
          <Title className="notif-name">
            {setting.label}
            {customized && <span className="badge badge-unknown">customized</span>}
            {inherits && <span className="badge badge-paused">global wording</span>}
          </Title>
          <p className="muted small">{setting.description}</p>
        </div>
        <span className={`notif-count${on ? '' : ' is-off'}`}>
          {on ? `${on} of ${usable.length} ${usable.length === 1 ? 'channel' : 'channels'}` : 'Not sent'}
        </span>
      </div>

      <div className="notif-toggles" role="group" aria-label={`Send “${setting.label}” by`}>
        {TOGGLES.map((ch) => (
          <label
            key={ch.id}
            className={`channel-toggle${draft[ch.field] ? ' is-on' : ''}${hasChannel(ch.id) ? '' : ' is-unset'}`}
            title={hasChannel(ch.id) ? undefined : `${ch.name} is not set up for this project`}
          >
            <input
              type="checkbox"
              className="visually-hidden"
              checked={draft[ch.field]}
              disabled={!canEdit || busy}
              onChange={(e) => setDraft({ ...draft, [ch.field]: e.target.checked })}
            />
            <ChannelLogo channel={ch.id} />
            <span>{ch.name}</span>
            {!hasChannel(ch.id) && <span className="channel-toggle-note">not set up</span>}
            <span className="switch" aria-hidden="true" />
          </label>
        ))}
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
            shows it as written, and WhatsApp on a single line.
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
// `monitors`: a project's, to show only what it can send. Every group shows
// without it (the global settings).
export default function NotificationSettings({ projectId, canEdit, channels, monitors }) {
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

  // On its own page the groups are the top headings; under a project's
  // "Notifications" heading they sit one level down.
  const level = projectId ? 3 : 2
  const GroupTitle = `h${level}`

  return (
    <div>
      <ErrorBanner error={list.error} />
      {GROUPS.filter((group) => groupShows(group, monitors)).map((group) => {
        const items = list.data.items.filter((s) => groupOf(s.kind) === group.key)
        if (!items.length) return null
        return (
          <section key={group.key} className="notif-group">
            <div className="notif-group-head">
              <GroupTitle className="notif-group-title">{group.title}</GroupTitle>
              {group.note && <p className="muted small">{group.note}</p>}
            </div>
            {items.map((setting) => (
              <KindEditor
                key={setting.kind}
                setting={setting}
                scope={scope}
                canEdit={canEdit}
                channels={channels}
                titleTag={`h${level + 1}`}
                onSave={save}
                onReset={reset}
              />
            ))}
          </section>
        )
      })}
    </div>
  )
}
