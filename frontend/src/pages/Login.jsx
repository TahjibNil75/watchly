import { useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth, useSignupOpen } from '../auth.jsx'
import BrandMark from '../BrandMark.jsx'
import { ErrorBanner } from '../components.jsx'

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
      setBusy(false)
    }
  }

  return (
    <div className="auth-screen">
      <form className="card auth-card" onSubmit={submit}>
        <Link to="/" className="brand brand-lg">
          <BrandMark />
          Watchly
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
