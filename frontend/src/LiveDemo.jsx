import { useEffect, useReducer } from 'react'
import ChannelLogo from './ChannelLogo.jsx'
import { CHANNELS } from './channels.js'
import { EnvironmentBadge, StatusBadge } from './components.jsx'
import { duration } from './format.js'
import { useMediaQuery } from './useMediaQuery.js'

// A status board that plays out what Watchly does: checks landing, an outage
// and its recovery, a slow site, an SSL reminder, and each alert lighting up
// the channels it went out on. Shown beside the sign-in form and on the
// landing page. Its sites and alerts are made up, so screen readers skip it.

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'

// One site is checked per tick, round robin, so the strips move out of step.
const TICK_MS = 700
const BARS = 32
// Each site replays a script this many checks long.
const CYCLE = 16
// What the made-up sites are checked every, for "down for …" wording.
const CHECK_EVERY_S = 60
const SLOW_MS = 1500
const TOAST_TICKS = 7

// `offset` shifts a site's script so its first event lands a few seconds in
// and the alerts take turns rather than piling up. `channels` are its
// project's; the webhook takes every alert, as it does for real.
const SITES = [
  {
    host: 'shop.example.com',
    environment: 'production',
    ms: 210,
    script: 'slow',
    offset: 13,
    channels: ['email', 'telegram', 'webhook'],
  },
  {
    host: 'api.example.com',
    environment: 'production',
    ms: 130,
    script: 'outage',
    offset: 9,
    channels: ['slack', 'whatsapp', 'webhook'],
  },
  {
    host: 'docs.example.com',
    environment: 'staging',
    ms: 95,
    script: 'ssl',
    offset: 5,
    channels: ['email', 'slack', 'webhook'],
  },
]

// "email, Telegram and webhook": only the brands keep their capitals.
function listChannels(ids) {
  const names = ids.map((id) =>
    id === 'email' || id === 'webhook' ? id : CHANNELS.find((c) => c.id === id).name,
  )
  return names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names.at(-1)}` : names[0]
}

// Stable pseudo-random in [0, 1), so the reducer stays pure.
function noise(n) {
  const x = Math.sin(n * 12.9898) * 43758.5453
  return x - Math.floor(x)
}

const jitter = (site, index, n) =>
  Math.round(site.ms * (0.75 + 0.5 * noise(n * 31 + index * 101)))

function trailingDown(history) {
  let count = 0
  for (let k = history.length - 1; k >= 0 && !history[k].up; k--) count++
  return count
}

// The site's nth check since the page opened, and the alert it would raise.
function simulate(site, index, n, history) {
  const phase = (n + site.offset) % CYCLE
  const prev = history[history.length - 1]
  const up = !(site.script === 'outage' && phase >= 10 && phase <= 12)
  const slow = site.script === 'slow' && (phase === 4 || phase === 5)
  const ms = slow ? 2200 + Math.round(noise(n) * 500) : jitter(site, index, n)
  const check = { n, up, ms: up ? ms : null }

  const { host, channels } = site
  const via = `via ${listChannels(channels)}`
  let alert = null
  if (prev.up && !up) {
    alert = {
      tone: 'down',
      title: `${host} is down`,
      detail: `HTTP 503 · ${via}`,
    }
  } else if (!prev.up && up) {
    alert = {
      tone: 'up',
      title: `${host} is back up`,
      detail: `Down for ${duration(trailingDown(history) * CHECK_EVERY_S)} · all-clear ${via}`,
    }
  } else if (ms >= SLOW_MS && prev.ms >= SLOW_MS) {
    alert = {
      tone: 'pending',
      title: `${host} is responding slowly`,
      detail: `${(ms / 1000).toFixed(1)} s, over its ${SLOW_MS / 1000} s threshold · ${via}`,
    }
  } else if (site.script === 'ssl' && phase === 0) {
    alert = {
      tone: 'pending',
      title: `SSL certificate for ${host} expires in 14 days`,
      detail: `Reminder ${via}`,
    }
  }
  return { check, alert: alert && { ...alert, channels } }
}

function initialState() {
  return {
    step: 0,
    // A calm history to start from, so the first outage stands out.
    history: SITES.map((site, index) =>
      Array.from({ length: BARS }, (_, k) => {
        const n = k - BARS
        return { n, up: true, ms: jitter(site, index, n), seed: true }
      }),
    ),
    toast: null,
  }
}

function advance(state) {
  const index = state.step % SITES.length
  const n = Math.floor(state.step / SITES.length)
  const { check, alert } = simulate(SITES[index], index, n, state.history[index])
  return {
    step: state.step + 1,
    history: state.history.map((h, i) => (i === index ? [...h.slice(1), check] : h)),
    toast: alert ? { ...alert, id: `${index}-${n}`, step: state.step } : state.toast,
  }
}

function LiveRow({ site, checks, order }) {
  const last = checks[checks.length - 1]
  const slowest = Math.max(1, ...checks.map((c) => c.ms ?? 0))
  const status = last.up ? 'up' : 'down'
  return (
    <div className="live-row">
      <div className="live-row-head">
        {/* Keyed on the check, so it pings each time one lands. */}
        <span key={last.n} className={`live-ping is-${status}`} />
        <strong className="truncate">{site.host}</strong>
        <EnvironmentBadge environment={site.environment} />
        <span className={last.ms >= SLOW_MS ? 'live-ms is-slow' : 'live-ms'}>
          {last.up ? `${last.ms} ms` : 'no response'}
        </span>
        <StatusBadge key={status} status={status} />
      </div>
      <div className="strip live-strip">
        {checks.map((c, k) => (
          <span
            key={c.n}
            className={`strip-bar ${!c.up ? 'is-down' : c.ms >= SLOW_MS ? 'is-slow' : 'is-up'}`}
            style={{
              height: `${c.up ? 25 + (75 * c.ms) / slowest : 100}%`,
              animationDelay: c.seed ? `${250 + order * 90 + k * 10}ms` : undefined,
            }}
          />
        ))}
      </div>
    </div>
  )
}

// Every channel an alert can take; the ones the showing alert went out on
// light up in turn, in its tone, and the rest fade back.
function LiveChannels({ toast }) {
  const sent = toast?.channels ?? []
  return (
    <div
      className={sent.length ? `live-channels is-sending tone-${toast.tone}` : 'live-channels'}
    >
      <span className="live-channels-label">Alerts go out by</span>
      <div className="live-channels-list">
        {CHANNELS.map(({ id, name }) => {
          const order = sent.indexOf(id)
          const isSent = order !== -1
          return (
            // A lit one is keyed on the alert too, so it lights up again for the next.
            <span
              key={isSent ? `${id}-${toast.id}` : id}
              className={isSent ? 'live-channel is-sent' : 'live-channel'}
              style={isSent ? { '--order': order } : undefined}
            >
              <ChannelLogo channel={id} />
              {name}
            </span>
          )
        })}
      </div>
    </div>
  )
}

function LiveToast({ toast }) {
  return (
    <div
      className={`toast live-toast tone-${toast.tone}`}
      style={{ '--life': `${TOAST_TICKS * TICK_MS}ms` }}
    >
      <span className="toast-dot" />
      <div className="toast-body">
        <strong>{toast.title}</strong>
        <span className="muted">{toast.detail}</span>
      </div>
      <span className="muted toast-time">now</span>
    </div>
  )
}

// The board and the alert under it. Stops ticking for reduced motion, and
// while the tab is hidden.
export default function LiveDemo() {
  const reduceMotion = useMediaQuery(REDUCED_MOTION_QUERY)
  const [{ step, history, toast }, tick] = useReducer(advance, undefined, initialState)

  useEffect(() => {
    if (reduceMotion) return undefined
    const timer = setInterval(() => {
      if (!document.hidden) tick()
    }, TICK_MS)
    return () => clearInterval(timer)
  }, [reduceMotion])

  const down = history.filter((h) => !h[h.length - 1].up).length
  const showToast = toast && step - toast.step <= TOAST_TICKS

  return (
    <>
      <div className="live-board" aria-hidden="true">
        <div className="live-board-head">
          <strong>Example dashboard</strong>
          <span className="live-counts">
            <span key={`up-${SITES.length - down}`}>
              <span className="dot dot-up" />
              {SITES.length - down} up
            </span>
            {down > 0 && (
              <span key={`down-${down}`} className="text-down">
                <span className="dot dot-down" />
                {down} down
              </span>
            )}
          </span>
        </div>
        {SITES.map((site, i) => (
          <LiveRow key={site.host} site={site} checks={history[i]} order={i} />
        ))}
        <LiveChannels toast={showToast ? toast : null} />
      </div>

      <div className="live-toasts" aria-hidden="true">
        {showToast && <LiveToast key={toast.id} toast={toast} />}
      </div>
    </>
  )
}
