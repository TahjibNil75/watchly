import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { ErrorBanner } from '../components.jsx'

/**
 * Where the link in an email-change confirmation lands. Confirming takes a
 * click rather than happening on load, so a mail scanner that opens the link
 * cannot use it up.
 */
export default function ConfirmEmail() {
  const { user, updateUser } = useAuth()
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''
  const [confirmed, setConfirmed] = useState(null)
  const [error, setError] = useState(
    token ? null : new Error('This link is incomplete. Open the link from your email again.'),
  )
  const [busy, setBusy] = useState(false)

  async function confirm() {
    setBusy(true)
    setError(null)
    try {
      const result = await api.confirmEmail(token)
      if (user?.id === result.id) {
        updateUser({ ...user, ...result, pending_email: null, pending_email_expires_at: null })
      }
      setConfirmed(result)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const back = user ? <Link to="/profile">Go to your profile</Link> : <Link to="/login">Sign in</Link>

  return (
    <div className="auth-screen">
      <div className="card auth-card">
        <div className="brand brand-lg">
          <span className="brand-mark" aria-hidden="true" />
          Watchly
        </div>
        {confirmed ? (
          <>
            <h1>Email address changed</h1>
            <p className="muted small">
              Your account now uses <strong>{confirmed.email}</strong>, for alerts and for
              signing in.
            </p>
            <p className="muted small center">{back}</p>
          </>
        ) : (
          <>
            <h1>Confirm your new email address</h1>
            <p className="muted small">
              This switches your Watchly account to the address this link was sent to, for alerts
              and for signing in.
            </p>
            <ErrorBanner error={error} />
            {token && (
              <button
                type="button"
                className="btn btn-primary btn-block"
                onClick={confirm}
                disabled={busy}
              >
                {busy ? 'Confirming…' : 'Confirm new address'}
              </button>
            )}
            <p className="muted small center">{back}</p>
          </>
        )}
      </div>
    </div>
  )
}
