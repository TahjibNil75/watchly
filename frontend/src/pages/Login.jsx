import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth, useSignupOpen } from '../auth.jsx'
import BrandMark, { useBrandMood } from '../BrandMark.jsx'
import { ErrorBanner } from '../components.jsx'

// A night-shift monitor: a heartbeat sweeps a dark grid behind the form, with
// readouts in the corners. The whole page takes the mascot's mood, its colour,
// its trace and its readouts, and a failed sign-in turns it to down for a while.
// The readouts play out a site being watched; they aren't real data.

const READOUTS = {
  up: { status: 'Up', response: '142 ms', alerts: 'None' },
  slow: { status: 'Slow', response: '2.4 s', alerts: 'None' },
  down: { status: 'Down', response: 'Timeout', alerts: 'Sent' },
  maint: { status: 'Maintenance', response: 'Paused', alerts: 'Muted' },
}
const ALARM_MS = 4000
const BLIP_MS = 500

// A heartbeat, a lazy wave, a flatline, and a dashed line for paused checks,
// across a 1000x600 box stretched over the page.
const BEAT = 'h160l20-50 24 140 22-120 14 30'
const TRACES = {
  up: `M0 330h110l20-50 24 140 22-120 14 30${BEAT}${BEAT}${BEAT}h96`,
  slow: `M0 330q62.5-70 125 0${'t125 0'.repeat(7)}`,
  down: 'M0 330h1000',
  maint: 'M0 330h1000',
}

function UtcClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(timer)
  }, [])
  return now.toISOString().slice(11, 19)
}

export default function Login() {
  const { login } = useAuth()
  const signupOpen = useSignupOpen()
  const navigate = useNavigate()
  const location = useLocation()
  const [identifier, setIdentifier] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [alarm, setAlarm] = useState(false)
  const [blip, setBlip] = useState(false)
  const blipTimer = useRef()

  const cycling = useBrandMood()
  const mood = alarm ? 'down' : cycling
  const readout = READOUTS[mood]

  useEffect(() => {
    if (!alarm) return undefined
    const timer = setTimeout(() => setAlarm(false), ALARM_MS)
    return () => clearTimeout(timer)
  }, [alarm])
  useEffect(() => () => clearTimeout(blipTimer.current), [])

  // Each key quickens the heartbeat for a moment.
  function onType() {
    setBlip(true)
    clearTimeout(blipTimer.current)
    blipTimer.current = setTimeout(() => setBlip(false), BLIP_MS)
  }

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(identifier.trim(), password)
      const from = location.state?.from
      navigate(from ? `${from.pathname}${from.search ?? ''}` : '/', { replace: true })
    } catch (err) {
      setError(err)
      setAlarm(true)
      setBusy(false)
    }
  }

  return (
    <div className={`night is-${mood}${blip ? ' is-blip' : ''}`}>
      <svg className="night-ecg" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true">
        {Object.entries(TRACES).map(([name, d]) => (
          <path key={name} className={`night-trace night-trace-${name}`} pathLength="1" d={d} />
        ))}
      </svg>
      <div className="night-readouts" aria-hidden="true">
        <div className="night-read is-tl">
          Status<b>{readout.status}</b>
        </div>
        <div className="night-read is-tr">
          Response<b>{readout.response}</b>
        </div>
        <div className="night-read is-bl">
          UTC<b>
            <UtcClock />
          </b>
        </div>
        <div className="night-read is-br">
          Alerts<b>{readout.alerts}</b>
        </div>
      </div>

      <form className="card auth-card night-card" onSubmit={submit} onInput={onType}>
        <Link to="/" className="brand brand-lg">
          <BrandMark mood={mood} />
          <span className="brand-word">Watchly</span>
        </Link>
        <h1>Sign in</h1>
        <ErrorBanner error={error} />
        <label className="field">
          <span>Username or email</span>
          <input
            value={identifier}
            onChange={(e) => setIdentifier(e.target.value)}
            autoComplete="username"
            autoFocus
            required
            minLength={3}
          />
        </label>
        <label className="field">
          <span>Password</span>
          <div className="password-input">
            <input
              type={showPassword ? 'text' : 'password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
            <button
              type="button"
              className="password-toggle"
              onClick={() => setShowPassword((v) => !v)}
              aria-label={showPassword ? 'Hide password' : 'Show password'}
              aria-pressed={showPassword}
            >
              {showPassword ? 'Hide' : 'Show'}
            </button>
          </div>
        </label>
        <button className="btn btn-primary btn-block" disabled={busy}>
          {busy && <span className="spinner" aria-hidden="true" />}
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
        <p className="muted small center">
          <Link to="/forgot-password">Forgot your password?</Link>
        </p>
        <p className="muted small center">
          {signupOpen === false ? (
            'No account? Ask an admin to invite you.'
          ) : (
            <>
              No account? <Link to="/signup">Create one</Link>
            </>
          )}
        </p>
      </form>
    </div>
  )
}
