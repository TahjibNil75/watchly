import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import BrandMark from '../BrandMark.jsx'
import { ErrorBanner, Loading, PasswordChecklist } from '../components.jsx'
import { USERNAME_MAX_LENGTH, USERNAME_MIN_LENGTH, withoutSpaces } from '../fields.js'
import { isStrongPassword, WEAK_PASSWORD } from '../password.js'
import { useApi } from '../useApi.js'
import { useLinkToken } from '../useLinkToken.js'

function Shell({ children }) {
  return (
    <div className="auth-screen">
      <div className="card auth-card">
        <div className="brand brand-lg">
          <BrandMark />
          <span className="brand-word">Watchly</span>
        </div>
        {children}
      </div>
    </div>
  )
}

export default function AcceptInvite() {
  const { user, acceptInvitation } = useAuth()
  const navigate = useNavigate()
  const token = useLinkToken()
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

  const set =
    (key, clean = (value) => value) =>
    (e) =>
      setForm({ ...form, [key]: clean(e.target.value) })
  const identity = {
    username: form.username,
    email: invitation.data?.email,
    fullName: form.full_name,
  }

  async function submit(event) {
    event.preventDefault()
    if (!isStrongPassword(form.password, identity)) {
      setError(new Error(WEAK_PASSWORD))
      return
    }
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
          <BrandMark />
          <span className="brand-word">Watchly</span>
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
          <span>
            Username{' '}
            <span className="muted">
              ({USERNAME_MIN_LENGTH}–{USERNAME_MAX_LENGTH} characters, no spaces)
            </span>
          </span>
          <input
            value={form.username}
            onChange={set('username', withoutSpaces)}
            autoComplete="username"
            autoFocus
            required
            minLength={USERNAME_MIN_LENGTH}
            maxLength={USERNAME_MAX_LENGTH}
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
              aria-describedby="password-rules"
              required
              minLength={8}
              maxLength={128}
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
              maxLength={128}
            />
          </label>
        </div>
        <PasswordChecklist id="password-rules" password={form.password} identity={identity} />
        <button className="btn btn-primary btn-block" disabled={busy}>
          {busy && <span className="spinner" aria-hidden="true" />}
          {busy ? 'Creating account…' : 'Create account'}
        </button>
      </form>
    </div>
  )
}
