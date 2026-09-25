import { useCallback, useEffect, useReducer, useSyncExternalStore } from 'react'
import { Outlet } from 'react-router-dom'
import { EnvironmentBadge, StatusBadge } from './components.jsx'
import { duration } from './format.js'

// Frame for the public pages (sign in, sign up, password reset, invitations).
// On wide screens a status board beside the form plays out what Watchly does:
// checks landing, an outage and its recovery, a slow site, an SSL reminder.
// Its sites and alerts are made up, so screen readers skip the board.

const SPLIT_QUERY = '(min-width: 960px)'
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
// and the alerts take turns rather than piling up.
const SITES = [
  { host: 'shop.example.com', environment: 'production', ms: 210, script: 'slow', offset: 13 },
  { host: 'api.example.com', environment: 'production', ms: 130, script: 'outage', offset: 9 },
  { host: 'docs.example.com', environment: 'staging', ms: 95, script: 'ssl', offset: 5 },
]

function useMediaQuery(query) {
  const subscribe = useCallback(
    (onChange) => {
      const list = window.matchMedia(query)
      list.addEventListener('change', onChange)
      return () => list.removeEventListener('change', onChange)
    },
    [query],
  )
  return useSyncExternalStore(subscribe, () => window.matchMedia(query).matches)
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

  const { host } = site
  let alert = null
  if (prev.up && !up) {
    alert = {
      tone: 'down',
      title: `${host} is down`,
      detail: 'HTTP 503 · Slack alert sent to #ops',
    }
  } else if (!prev.up && up) {
    alert = {
      tone: 'up',
      title: `${host} is back up`,
      detail: `Down for ${duration(trailingDown(history) * CHECK_EVERY_S)} · all-clear sent`,
    }
  } else if (ms >= SLOW_MS && prev.ms >= SLOW_MS) {
    alert = {
      tone: 'pending',
      title: `${host} is responding slowly`,
      detail: `${(ms / 1000).toFixed(1)} s, over its ${SLOW_MS / 1000} s threshold · team emailed`,
    }
  } else if (site.script === 'ssl' && phase === 0) {
    alert = {
      tone: 'pending',
      title: `SSL certificate for ${host} expires in 14 days`,
      detail: 'Reminder emailed to the project team',
    }
  }
  return { check, alert }
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

function Showcase() {
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
    <aside className="auth-aside">
      <div className="auth-pitch">
        <span className="live-pill">
          <span className="live-pill-dot" />
          Uptime monitoring
        </span>
        <h2>Know the moment a site goes down.</h2>
        <p className="muted">
          Watchly checks each of your websites on its own schedule, alerts the right people by
          email, Slack or webhook, and tells them when it&apos;s back.
        </p>
      </div>

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
      </div>

      <div className="live-toasts" aria-hidden="true">
        {showToast && <LiveToast key={toast.id} toast={toast} />}
      </div>
    </aside>
  )
}

export default function AuthLayout() {
  const split = useMediaQuery(SPLIT_QUERY)
  return (
    <div className={split ? 'auth-layout is-split' : 'auth-layout'}>
      <Outlet />
      {split && <Showcase />}
    </div>
  )
}
