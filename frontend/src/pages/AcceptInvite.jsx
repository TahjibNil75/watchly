import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { ErrorBanner, Loading } from '../components.jsx'
import { useApi } from '../useApi.js'

function Shell({ children }) {
  return (
    <div className="auth-screen">
      <div className="card auth-card">
        <div className="brand brand-lg">
          <span className="brand-mark" aria-hidden="true" />
          Watchly
        </div>
        {children}
      </div>
    </div>
  )
}

export default function AcceptInvite() {
  const { user, acceptInvitation } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''
  // Looking an invitation up does not use it up, so this is safe to repeat.
  const invitation = useApi(() => (token ? api.previewInvitation(token) : null), [token])
  const [form, setForm] = useState({
    username: '',
    full_name: '',
    password: '',
    confirm_password: '',
  })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value })

  async function submit(event) {
    event.preventDefault()
    if (form.password !== form.confirm_password) {
      setError(new Error('Passwords do not match.'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      await acceptInvitation({ ...form, token, full_name: form.full_name.trim() || null })
      navigate('/', { replace: true })
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  if (!token || invitation.error) {
    return (
      <Shell>
        <h1>Invitation unavailable</h1>
        <ErrorBanner
          error={invitation.error ?? new Error('This invitation link is incomplete. Open the link from your email again.')}
        />
        <p className="muted small">Ask whoever invited you to send a new one.</p>
        <p className="muted small center">
          <Link to="/login">Sign in</Link>
        </p>
      </Shell>
    )
  }

  if (invitation.loading || !invitation.data) {
    return (
      <Shell>
        <Loading />
      </Shell>
    )
  }

  const { email, role, invited_by_name: invitedBy } = invitation.data

  return (
    <div className="auth-screen">
      <form className="card auth-card" onSubmit={submit}>
        <div className="brand brand-lg">
          <span className="brand-mark" aria-hidden="true" />
          Watchly
        </div>
        <h1>Accept your invitation</h1>
        <p className="muted small">
          {invitedBy ? `${invitedBy} invited` : 'You have been invited'} you to join as{' '}
          <strong>{role}</strong>. Choose a username and password to finish.
        </p>
        {user && (
          <p className="muted small">
            You are signed in as {user.username}. Accepting signs you in as the new account
            instead.
          </p>
        )}
        <ErrorBanner error={error} />
        <label className="field">
          <span>Email</span>
          <input type="email" value={email} readOnly disabled />
        </label>
        <label className="field">
          <span>Username</span>
          <input
            value={form.username}
            onChange={set('username')}
            autoComplete="username"
            autoFocus
            required
            minLength={3}
            maxLength={50}
          />
        </label>
        <label className="field">
          <span>
            Full name <span className="muted">(optional)</span>
          </span>
          <input value={form.full_name} onChange={set('full_name')} autoComplete="name" />
        </label>
        <div className="row-2">
          <label className="field">
            <span>Password</span>
            <input
              type="password"
              value={form.password}
              onChange={set('password')}
              autoComplete="new-password"
              required
              minLength={8}
            />
          </label>
          <label className="field">
            <span>Confirm password</span>
            <input
              type="password"
              value={form.confirm_password}
              onChange={set('confirm_password')}
              autoComplete="new-password"
              required
              minLength={8}
            />
          </label>
        </div>
        <button className="btn btn-primary btn-block" disabled={busy}>
          {busy ? 'Creating account…' : 'Create account'}
        </button>
      </form>
    </div>
  )
}
