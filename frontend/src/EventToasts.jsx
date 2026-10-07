import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from './api.js'
import BrandMark from './BrandMark.jsx'
import { dateTime, duration, timeAgo } from './format.js'
import { SITE_EVENTS } from './useApi.js'

// Toasts for what happens to the sites you can see: outages, recoveries, slow
// spells, packet loss, DNS changes, expiring certificates; and, with
// infrastructure on, to AWS servers and load balancers. Polls each event feed and remembers, per user
// in this browser, the newest event already shown, so signing in catches up on
// what happened while you were away and a reload replays nothing.

const POLL_MS = 30000
// At most this many at once; the oldest make way. A catch-up shows one fewer
// events and a count of the rest.
const SHOWN = 4
// Good news and warnings leave on their own; outages stay until dismissed.
const DISMISS_MS = 8000
const LEAVE_MS = 200
const DAY_MS = 86400000

// The mascot's face for each tone: happy, fed up, tense.
export const TONE_MOODS = { up: 'up', pending: 'slow', down: 'down' }

function readSeen(key) {
  try {
    const raw = localStorage.getItem(key)
    const id = raw === null ? NaN : Number(raw)
    return Number.isInteger(id) && id >= 0 ? id : null
  } catch {
    return null
  }
}

function writeSeen(key, id) {
  try {
    localStorage.setItem(key, String(id))
  } catch {
    // Storage unavailable (private mode); this tab still remembers.
  }
}

const plural = (count, noun) => `${count} ${noun}${count === 1 ? '' : 's'}`

// What a toast says about one event; null for a kind this version doesn't know.
function describe(event) {
  const name = event.website.name
  switch (event.kind) {
    case 'down':
      return { tone: 'down', sticky: true, title: `${name} is down`, detail: event.summary }
    case 'recovered':
      return {
        tone: 'up',
        title: `${name} is back up`,
        detail: event.downtime_seconds
          ? `Down for ${duration(event.downtime_seconds)}`
          : event.summary,
      }
    case 'slow_response':
      return {
        tone: 'pending',
        title:
          event.website.check_type === 'ping'
            ? `${name} has high latency`
            : event.website.check_type === 'dns'
              ? `${name} is resolving slowly`
              : event.website.check_type === 'database'
                ? `${name} is answering slowly`
                : `${name} is responding slowly`,
        detail: `${event.response_time_ms} ms, over its ${event.threshold_ms} ms threshold`,
      }
    case 'packet_loss':
      return { tone: 'pending', title: `${name} is losing packets`, detail: event.summary }
    case 'dns_changed':
      return { tone: 'pending', title: `DNS records changed for ${name}`, detail: event.summary }
    case 'ssl_expiring': {
      const left = new Date(event.ssl_expires_at) - Date.now()
      if (left <= 0) {
        return {
          tone: 'down',
          sticky: true,
          title: `SSL certificate for ${name} has expired`,
          detail: `Ended ${dateTime(event.ssl_expires_at)}`,
        }
      }
      // Whole days, rounded down: "6 days" for 6 days 2 hours, never more than is left.
      const days = Math.floor(left / DAY_MS)
      const when = days ? `in ${plural(days, 'day')}` : 'within a day'
      return {
        tone: 'pending',
        title: `SSL certificate for ${name} expires ${when}`,
        detail: `Ends ${dateTime(event.ssl_expires_at)}`,
      }
    }
    case 'nameservers_changed':
      // Possibly a hijack: it stays until someone looks.
      return {
        tone: 'down',
        sticky: true,
        title: `Nameservers changed for ${event.summary.split(' ')[0]}`,
        detail: event.summary,
      }
    case 'domain_expiring': {
      // The summary is "example.com expires in 13 days"; the domain leads it.
      const domain = event.summary.split(' ')[0]
      const left = new Date(event.domain_expires_at) - Date.now()
      if (left <= 0) {
        return {
          tone: 'down',
          sticky: true,
          title: `The domain ${domain} has expired`,
          detail: `Found via ${name} · ended ${dateTime(event.domain_expires_at)}`,
        }
      }
      const days = Math.floor(left / DAY_MS)
      const when = days ? `in ${plural(days, 'day')}` : 'within a day'
      return {
        tone: days < 7 ? 'down' : 'pending',
        title: `The domain ${domain} expires ${when}`,
        detail: `Found via ${name} · ends ${dateTime(event.domain_expires_at)}`,
      }
    }
    default:
      return null
  }
}

function toToast(event) {
  const text = describe(event)
  if (!text) return null
  return {
    ...text,
    key: `event-${event.id}`,
    href: `/websites/${event.website.id}`,
    at: event.occurred_at,
  }
}

// The infrastructure feed: resources down, back, degraded; VPCs lost and found.
function describeInfra(event) {
  const name = event.resource?.name
  switch (event.kind) {
    case 'infra_down':
      return { tone: 'down', sticky: true, title: `${name} is down`, detail: event.summary }
    case 'infra_recovered':
      return {
        tone: 'up',
        title: `${name} is back up`,
        detail: event.downtime_seconds ? `Down for ${duration(event.downtime_seconds)}` : event.summary,
      }
    case 'infra_degraded':
      return { tone: 'pending', title: `${name} is degraded`, detail: event.summary }
    case 'asg_scaled_out':
      return { tone: 'pending', title: `${name} scaled out`, detail: event.summary }
    case 'asg_scaled_in':
      return { tone: 'pending', title: `${name} scaled in`, detail: event.summary }
    case 'vpc_unreachable':
      return { tone: 'down', sticky: true, title: `${event.vpc.name} is unreachable`, detail: event.summary }
    case 'vpc_recovered':
      return { tone: 'up', title: `${event.vpc.name} answers again`, detail: event.summary }
    case 'account_capacity':
      return { tone: 'pending', title: 'AWS account capacity', detail: event.summary }
    case 'deploy_started':
      return { tone: 'info', title: 'Deployment started', detail: event.summary }
    case 'deploy_finished':
      // The summary ends "… succeeded / failed / was stopped after 4m".
      return {
        tone: / succeeded /.test(event.summary) ? 'up' : / failed /.test(event.summary) ? 'down' : 'pending',
        title: 'Deployment ended',
        detail: event.summary,
      }
    default:
      return null
  }
}

// Where a toast leads: the resource, else the VPC, else (a deployment, an
// account's capacity) the project.
function infraHref(event) {
  if (event.resource) return `/infra/resources/${event.resource.id}`
  if (event.vpc) return `/infra/vpcs/${event.vpc.id}`
  return `/projects/${event.project_id}`
}

function infraToast(event) {
  const text = describeInfra(event)
  if (!text) return null
  return {
    ...text,
    key: `infra-${event.id}`,
    href: infraHref(event),
    at: event.occurred_at,
  }
}

// Each feed keeps its own cursor; the websites' key predates the others.
const FEEDS = {
  websites: {
    fetch: (query) => api.websiteEvents(query),
    seenKey: (userId) => `watchly.lastEventId.${userId}`,
    toToast,
    home: '/websites',
  },
  infra: {
    fetch: (query) => api.infraEvents(query),
    seenKey: (userId) => `watchly.lastInfraEventId.${userId}`,
    toToast: infraToast,
    home: '/infra',
  },
}

function Toast({ toast, onDismiss }) {
  const { key, sticky, leaving } = toast
  // Hovering or focusing holds a toast; letting go restarts its time in full.
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const [round, setRound] = useState(0)
  const held = hovered || focused

  useEffect(() => {
    if (sticky || held || leaving) return undefined
    const timer = setTimeout(() => onDismiss(key), DISMISS_MS)
    return () => clearTimeout(timer)
  }, [key, sticky, held, leaving, onDismiss])

  const release = (set) => () => {
    set(false)
    setRound((r) => r + 1)
  }

  const classes = ['toast', `tone-${toast.tone}`]
  if (held) classes.push('is-held')
  if (leaving) classes.push('is-leaving')

  return (
    <div
      className={classes.join(' ')}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={release(setHovered)}
      onFocus={() => setFocused(true)}
      onBlur={release(setFocused)}
    >
      <span className="toast-mark">
        <BrandMark mood={TONE_MOODS[toast.tone] ?? 'up'} />
      </span>
      <Link to={toast.href} className="toast-body" onClick={() => onDismiss(key)}>
        <strong>{toast.title}</strong>
        <span className="muted">{toast.detail}</span>
      </Link>
      {toast.at && <span className="muted toast-time">{timeAgo(toast.at)}</span>}
      <button
        type="button"
        className="toast-close"
        aria-label="Dismiss"
        onClick={() => onDismiss(key)}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
      {!sticky && (
        <span key={round} className="toast-timer" style={{ '--life': `${DISMISS_MS}ms` }} />
      )}
    </div>
  )
}

// `infra` adds the infrastructure feed, when the API has it switched on.
export default function EventToasts({ userId, infra = false }) {
  const [toasts, setToasts] = useState([])

  useEffect(() => {
    const feeds = infra ? [FEEDS.websites, FEEDS.infra] : [FEEDS.websites]
    const cursors = new Map()
    let busy = false
    let stopped = false

    async function pollFeed(feed) {
      const key = feed.seenKey(userId)
      let cursor = cursors.get(feed) ?? null
      // Another tab may already have shown some.
      const seen = readSeen(key)
      if (seen !== null && (cursor === null || seen > cursor)) cursor = seen
      try {
        const catchingUp = cursor !== null
        const page = await feed.fetch(catchingUp ? { after_id: cursor, limit: SHOWN - 1 } : { limit: 1 })
        if (stopped) return
        const newest = page.items[0]?.id
        if (!catchingUp) {
          // First time in this browser: start from now rather than replay history.
          cursor = newest ?? 0
        } else if (newest !== undefined) {
          cursor = newest
          const fresh = page.items.map(feed.toToast).filter(Boolean).reverse()
          const rest = page.total - page.items.length
          if (rest > 0) {
            fresh.unshift({
              key: `more-${feed.home}-${newest}`,
              tone: 'info',
              title: `${plural(rest, 'more alert')} since you last looked`,
              detail: 'The dashboard shows where everything stands now.',
              href: feed.home,
            })
          }
          setToasts((list) => [...list, ...fresh].slice(-SHOWN))
          // Pages that poll state refresh now, so they agree with the toast.
          window.dispatchEvent(new Event(SITE_EVENTS))
        }
        cursors.set(feed, cursor)
        writeSeen(key, cursor)
      } catch {
        // The API is having a moment; the next poll tries again.
      }
    }

    async function poll() {
      if (busy || document.hidden) return
      busy = true
      try {
        for (const feed of feeds) await pollFeed(feed)
      } finally {
        busy = false
      }
    }

    poll()
    const timer = setInterval(poll, POLL_MS)
    // Catch up as soon as the tab is looked at again, not up to POLL_MS later.
    document.addEventListener('visibilitychange', poll)
    return () => {
      stopped = true
      clearInterval(timer)
      document.removeEventListener('visibilitychange', poll)
    }
  }, [userId, infra])

  const dismiss = useCallback((key) => {
    setToasts((list) => list.map((t) => (t.key === key ? { ...t, leaving: true } : t)))
    setTimeout(() => setToasts((list) => list.filter((t) => t.key !== key)), LEAVE_MS)
  }, [])

  return (
    <section className="toasts" aria-live="polite" aria-label="Site alerts">
      {toasts.map((toast) => (
        <Toast key={toast.key} toast={toast} onDismiss={dismiss} />
      ))}
    </section>
  )
}
