import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import BrandMark from '../BrandMark.jsx'
import { ErrorBanner } from '../components.jsx'
import { withoutSpaces } from '../fields.js'

export default function ForgotPassword() {
  const [email, setEmail] = useState('')
  const [done, setDone] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      // The API answers the same whether or not the address has an account.
      setDone((await api.forgotPassword(email.trim())).detail)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-screen">
      <form className="card auth-card" onSubmit={submit}>
        <Link to="/" className="brand brand-lg">
          <BrandMark />
          <span className="brand-word">Watchly</span>
        </Link>
        <h1>Forgot your password?</h1>
        {done ? (
          <div className="banner banner-info" role="status">
            {done} Sign in with it, and you will be asked to choose a new password.
          </div>
        ) : (
          <>
            <p className="muted small">
              Enter your account&apos;s email address and we&apos;ll email you a temporary password.
              Your current password keeps working until you use it.
            </p>
            <ErrorBanner error={error} />
            <label className="field">
              <span>Email</span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(withoutSpaces(e.target.value))}
                autoComplete="email"
                autoFocus
                required
              />
            </label>
            <button className="btn btn-primary btn-block" disabled={busy}>
              {busy && <span className="spinner" aria-hidden="true" />}
              {busy ? 'Sending…' : 'Email me a temporary password'}
            </button>
          </>
        )}
        <p className="muted small center">
          <Link to="/login">Back to sign in</Link>
        </p>
      </form>
    </div>
  )
}
