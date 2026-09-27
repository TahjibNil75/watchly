import { useEffect, useState } from 'react'
import { api } from './api.js'
import { ErrorBanner } from './components.jsx'
import { dateTime, duration, until } from './format.js'

// How long "Start maintenance" lasts; 30 minutes unless picked otherwise.
const DURATIONS = [
  [15, '15 min'],
  [30, '30 min'],
  [60, '1 hour'],
  [120, '2 hours'],
  [240, '4 hours'],
]

const time = (iso) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

const sameDay = (a, b) => new Date(a).toDateString() === new Date(b).toDateString()

const length = (w) => duration((new Date(w.ends_at) - new Date(w.starts_at)) / 1000)

// "27/09/2026, 22:00:00 → 23:30 (1h 30m)", the end date only when it differs.
const span = (w) =>
  `${dateTime(w.starts_at)} → ${sameDay(w.starts_at, w.ends_at) ? time(w.ends_at) : dateTime(w.ends_at)} (${length(w)})`

// A datetime-local input's value is local time with no zone; the API wants one.
const toIso = (local) => new Date(local).toISOString()

// Now, as a datetime-local value, for the inputs' `min`.
function localNow() {
  const d = new Date()
  d.setSeconds(0, 0)
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}

const ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone

// Starting, ending and scheduling maintenance windows, during which the site
// is not checked and nobody is alerted. `onChange` takes the updated site;
// `onBoundary` refetches it when a window starts or ends by the clock.
export default function Maintenance({ site, canManage, onChange, onBoundary }) {
  const [minutes, setMinutes] = useState(30)
  const [reason, setReason] = useState('')
  const [plan, setPlan] = useState({ starts_at: '', ends_at: '', reason: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const active = site.maintenance
  const upcoming = site.upcoming_maintenance

  // Refresh the page the moment the current window ends or the next begins,
  // rather than on the next poll.
  const boundary = active?.ends_at ?? upcoming[0]?.starts_at
  useEffect(() => {
    if (!boundary) return undefined
    const wait = new Date(boundary).getTime() - Date.now() + 1000
    // setTimeout overflows past ~24.8 days; the poll catches those.
    if (wait > 2 ** 31 - 1) return undefined
    const timer = setTimeout(onBoundary, Math.max(wait, 0))
    return () => clearTimeout(timer)
  }, [boundary, onBoundary])

  if (!canManage && !active && !upcoming.length) return null

  async function run(action) {
    setBusy(true)
    setError(null)
    try {
      onChange(await action())
      return true
    } catch (err) {
      setError(err)
      return false
    } finally {
      setBusy(false)
    }
  }

  const start = async () => {
    const body = { duration_minutes: Number(minutes), reason: reason.trim() || null }
    if (await run(() => api.startMaintenance(site.id, body))) setReason('')
  }

  const end = () => run(() => api.endMaintenance(site.id))

  const schedule = async (event) => {
    event.preventDefault()
    const body = {
      starts_at: toIso(plan.starts_at),
      ends_at: toIso(plan.ends_at),
      reason: plan.reason.trim() || null,
    }
    if (await run(() => api.startMaintenance(site.id, body))) {
      setPlan({ starts_at: '', ends_at: '', reason: '' })
    }
  }

  const cancel = (w) => {
    if (!window.confirm(`Cancel the maintenance planned for ${span(w)}?`)) return
    run(() => api.cancelMaintenance(site.id, w.id))
  }

  const setPlanField = (key) => (e) => setPlan({ ...plan, [key]: e.target.value })
  const picked = DURATIONS.find(([value]) => value === Number(minutes))?.[1]

  return (
    <section className={`card${active ? ' card-maintenance' : ''}`}>
      <h2>Maintenance</h2>
      <p className="muted small">
        During a maintenance window this site isn&apos;t checked and nobody is alerted, so a
        deployment doesn&apos;t page anyone. When it ends, checks resume: if the site is still
        down, the usual alert goes out.
      </p>
      <ErrorBanner error={error} />

      {active ? (
        <div className="maint-row">
          <span className="maint-grow">
            <strong>In maintenance</strong> until{' '}
            <strong title={dateTime(active.ends_at)}>{time(active.ends_at)}</strong>
            {' · '}
            {until(active.ends_at)} left
            {active.reason && <> · {active.reason}</>}
          </span>
          {canManage && (
            <button type="button" className="btn btn-primary" onClick={end} disabled={busy}>
              End now
            </button>
          )}
        </div>
      ) : (
        canManage && (
          <div className="maint-row">
            <select
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              aria-label="How long"
            >
              {DURATIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <input
              className="maint-grow"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Reason (optional), e.g. Deploying 2.4"
              maxLength={255}
              aria-label="Reason"
            />
            <button type="button" className="btn btn-primary" onClick={start} disabled={busy}>
              Start maintenance for {picked}
            </button>
          </div>
        )
      )}

      <h3>Scheduled</h3>
      {upcoming.length ? (
        <ul className="people">
          {upcoming.map((w) => (
            <li key={w.id}>
              <div>
                <strong>{span(w)}</strong>
                {w.reason && <div className="muted small">{w.reason}</div>}
              </div>
              {canManage && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => cancel(w)}
                  disabled={busy}
                >
                  Cancel
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted small">
          {active ? 'Nothing else scheduled.' : 'No maintenance scheduled.'}
        </p>
      )}

      {canManage && (
        <details className="advanced">
          <summary>Schedule a window</summary>
          <form className="form" onSubmit={schedule}>
            <div className="row-3">
              <label className="field">
                <span>Starts</span>
                <input
                  type="datetime-local"
                  value={plan.starts_at}
                  onChange={setPlanField('starts_at')}
                  min={localNow()}
                  required
                />
              </label>
              <label className="field">
                <span>Ends</span>
                <input
                  type="datetime-local"
                  value={plan.ends_at}
                  onChange={setPlanField('ends_at')}
                  min={plan.starts_at || localNow()}
                  required
                />
              </label>
              <label className="field">
                <span>Reason</span>
                <input
                  value={plan.reason}
                  onChange={setPlanField('reason')}
                  placeholder="Database upgrade"
                  maxLength={255}
                />
              </label>
            </div>
            <span className="muted small">
              Times are in your time zone ({ZONE}). A window lasts at most 7 days, and can&apos;t
              overlap another.
            </span>
            <div className="form-actions">
              <button type="submit" className="btn btn-primary" disabled={busy}>
                Schedule
              </button>
            </div>
          </form>
        </details>
      )}
    </section>
  )
}
