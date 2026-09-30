import { useEffect, useRef, useState } from 'react'
import ChannelLogo from './ChannelLogo.jsx'
import { CHANNELS } from './channels.js'
import { useMediaQuery } from './useMediaQuery.js'

// The landing page's channels as tabs, each showing one made-up outage as it
// arrives there, drawn after what app/monitoring/alerts sends. Until someone
// picks a channel they take turns while the section is in view; pointing at
// or focusing them holds the turn. Each turn is the active tab's progress bar
// running out (see .alert-tab-turn), so pausing the bar pauses the turn.
// All the panels are drawn, one over another, so the stage is as tall as the
// tallest and nothing below it moves when the channel changes.

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'

const SITE = 'api.example.com'
const TITLE = `${SITE} is down`
const SUBJECT = `[DOWN] Shop / ${SITE} is not responding`
const BODY = `${SITE} did not respond as expected: HTTP 503 Service Unavailable`
const FACTS = [
  ['Status', 'DOWN'],
  ['Expected status', '200'],
  ['Checked at', '14:02 UTC'],
]
const LINK = 'Open in Watchly'

// One line each, so they can arrive one after another.
const PAYLOAD = [
  '{',
  '  "event": "down",',
  `  "subject": "${SUBJECT}",`,
  `  "summary": "HTTP 503 Service Unavailable",`,
  `  "website": { "name": "${SITE}", "url": "https://${SITE}" },`,
  '  "check": { "is_up": false, "status_code": 503 },',
  '  "outage": { "attempt": 1, "max_attempts": 4 }',
  '}',
]
const JSON_LINE = /^(\s*)("[^"]+")(: )(.*)$/

// --n staggers a preview's parts as it arrives.
const part = (n) => ({ style: { '--n': n } })

function EmailPreview() {
  return (
    <div className="alert-preview preview-email">
      <div className="preview-email-head" {...part(0)}>
        <span className="preview-avatar is-logo" aria-hidden="true">
          <img src="/favicon.svg" alt="" />
        </span>
        <span className="preview-from">
          <strong>Watchly Alerts</strong>
          <span className="muted">alerts@yourdomain.com</span>
        </span>
        <span className="muted preview-time">14:02</span>
      </div>
      <strong className="preview-subject" {...part(1)}>
        {SUBJECT}
      </strong>
      <div className="preview-email-body" {...part(2)}>
        <span className="preview-kicker">Outage</span>
        <strong>{TITLE}</strong>
        <p className="muted">{BODY}</p>
        <span className="preview-button">{LINK}</span>
      </div>
    </div>
  )
}

function SlackPreview() {
  return (
    <div className="alert-preview preview-slack">
      <div className="preview-slack-channel" {...part(0)}>
        # ops
      </div>
      <div className="preview-slack-msg" {...part(1)}>
        <span className="preview-avatar is-app is-logo" aria-hidden="true">
          <img src="/favicon.svg" alt="" />
        </span>
        <div className="preview-slack-body">
          <span>
            <strong>Watchly</strong> <span className="preview-app">APP</span>{' '}
            <span className="muted">14:02</span>
          </span>
          <strong className="preview-title">{TITLE}</strong>
          <span className="muted small">Outage</span>
          <span className="preview-fields" {...part(2)}>
            {FACTS.map(([label, value]) => (
              <span key={label}>
                <b>{label}</b>
                {value}
              </span>
            ))}
          </span>
          <span className="preview-button" {...part(3)}>
            {LINK}
          </span>
          <span className="preview-thread" {...part(4)}>
            2 replies · reminders while it stays down
          </span>
        </div>
      </div>
    </div>
  )
}

function TelegramPreview() {
  return (
    <div className="alert-preview preview-chat is-telegram">
      <div className="preview-chat-head" {...part(0)}>
        <ChannelLogo channel="telegram" />
        <strong>Watchly Alerts</strong>
        <span className="muted">bot</span>
      </div>
      <div className="preview-bubble">
        <strong>🚨 {TITLE}</strong>
        <em>Outage</em>
        <span>{BODY}</span>
        {FACTS.map(([label, value]) => (
          <span key={label}>
            <b>{label}:</b> {value}
          </span>
        ))}
        <span className="preview-link">{LINK}</span>
        <span className="preview-meta">14:02</span>
      </div>
    </div>
  )
}

function WhatsAppPreview() {
  return (
    <div className="alert-preview preview-chat is-whatsapp">
      <div className="preview-chat-head" {...part(0)}>
        <ChannelLogo channel="whatsapp" />
        <strong>Watchly</strong>
        <span className="muted">business account</span>
      </div>
      <div className="preview-bubble">
        <span>Watchly monitoring update: {TITLE}</span>
        <span>
          {BODY} | {FACTS.map(([label, value]) => `${label}: ${value}`).join(' · ')} | {LINK}:
          <span className="preview-link"> watchly.example.com/websites/12</span>
        </span>
        <span className="muted small">
          You are receiving this because your number is on the WhatsApp alert list of a Watchly
          project.
        </span>
        <span className="preview-meta">14:02 ✓✓</span>
      </div>
    </div>
  )
}

function WebhookPreview() {
  return (
    <div className="alert-preview preview-code">
      <div className="preview-code-head" {...part(0)}>
        <span className="preview-method">POST</span>
        <span className="truncate">https://hooks.example.com/watchly</span>
      </div>
      <pre>
        {PAYLOAD.map((line, n) => {
          const m = JSON_LINE.exec(line)
          return (
            <span key={line} className="preview-code-line" {...part(n + 1)}>
              {m ? (
                <>
                  {m[1]}
                  <span className="preview-key">{m[2]}</span>
                  {m[3]}
                  {m[4]}
                </>
              ) : (
                line
              )}
            </span>
          )
        })}
      </pre>
    </div>
  )
}

const PREVIEWS = {
  email: EmailPreview,
  slack: SlackPreview,
  telegram: TelegramPreview,
  whatsapp: WhatsAppPreview,
  webhook: WebhookPreview,
}

export default function AlertPreviews() {
  const reduceMotion = useMediaQuery(REDUCED_MOTION_QUERY)
  const [active, setActive] = useState(0)
  // Once someone picks a channel, it stays until they pick another.
  const [chosen, setChosen] = useState(false)
  const [held, setHeld] = useState(false)
  const [inView, setInView] = useState(false)
  const rootRef = useRef(null)
  const tabRefs = useRef([])
  const cycling = !chosen && !reduceMotion

  useEffect(() => {
    const observer = new IntersectionObserver(([entry]) => setInView(entry.isIntersecting), {
      threshold: 0.35,
    })
    observer.observe(rootRef.current)
    return () => observer.disconnect()
  }, [])

  function pick(index, focus = false) {
    setActive(index)
    setChosen(true)
    if (focus) tabRefs.current[index]?.focus()
  }

  // Arrow keys, Home and End move along the tabs, as tabs do.
  function onKeyDown(e) {
    const last = CHANNELS.length - 1
    const next = {
      ArrowRight: active === last ? 0 : active + 1,
      ArrowLeft: active === 0 ? last : active - 1,
      Home: 0,
      End: last,
    }[e.key]
    if (next === undefined) return
    e.preventDefault()
    pick(next, true)
  }

  return (
    <div
      ref={rootRef}
      className={`alert-previews${cycling && (held || !inView) ? ' is-held' : ''}`}
      onPointerEnter={() => setHeld(true)}
      onPointerLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)}
      onBlur={() => setHeld(false)}
    >
      <div className="landing-channels" role="tablist" aria-label="Alert channels">
        {CHANNELS.map((c, i) => (
          <button
            key={c.id}
            ref={(el) => {
              tabRefs.current[i] = el
            }}
            type="button"
            role="tab"
            id={`alert-tab-${c.id}`}
            aria-selected={i === active}
            aria-controls={`alert-panel-${c.id}`}
            tabIndex={i === active ? 0 : -1}
            className="landing-channel"
            style={{ '--i': i }}
            onClick={() => pick(i)}
            onKeyDown={onKeyDown}
          >
            <ChannelLogo channel={c.id} />
            {c.name}
            {cycling && i === active && (
              <span
                className="alert-tab-turn"
                onAnimationEnd={() => setActive((a) => (a + 1) % CHANNELS.length)}
              />
            )}
          </button>
        ))}
      </div>

      <div className="alert-stage">
        <span className="alert-stage-tag">Example alert</span>
        {CHANNELS.map((c, i) => {
          const Preview = PREVIEWS[c.id]
          return (
            <div
              key={c.id}
              role="tabpanel"
              id={`alert-panel-${c.id}`}
              aria-labelledby={`alert-tab-${c.id}`}
              tabIndex={0}
              className={i === active ? 'alert-panel is-active' : 'alert-panel'}
            >
              <Preview />
            </div>
          )
        })}
      </div>
    </div>
  )
}
