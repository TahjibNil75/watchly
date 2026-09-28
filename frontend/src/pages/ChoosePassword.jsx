import { useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import BrandMark from '../BrandMark.jsx'
import { ErrorBanner } from '../components.jsx'

/**
 * Shown instead of the app after signing in with a temporary password. The API
 * refuses everything else until a new password is chosen, so there is nowhere
 * else to go.
 */
export default function ChoosePassword() {
  const { user, updateUser, logout } = useAuth()
  const [form, setForm] = useState({ current_password: '', new_password: '', confirm_password: '' })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value })

  async function submit(event) {
    event.preventDefault()
    if (form.new_password !== form.confirm_password) {
      setError(new Error('The new passwords do not match.'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      updateUser(await api.changePassword(form))
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  return (
    <div className="auth-screen">
      <form className="card auth-card" onSubmit={submit}>
        <div className="brand brand-lg">
          <BrandMark />
          Watchly
        </div>
        <h1>Choose a new password</h1>
        <p className="muted small">
          You signed in as <strong>{user.username}</strong> with a temporary password. Choose your
          own to continue.
        </p>
        <ErrorBanner error={error} />
        <label className="field">
          <span>Temporary password</span>
          <input
            type="password"
            value={form.current_password}
            onChange={set('current_password')}
            autoComplete="current-password"
            autoFocus
            required
          />
        </label>
        <label className="field">
          <span>New password</span>
          <input
            type="password"
            value={form.new_password}
            onChange={set('new_password')}
            autoComplete="new-password"
            required
            minLength={8}
            maxLength={128}
          />
        </label>
        <label className="field">
          <span>Confirm new password</span>
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
        <button className="btn btn-primary btn-block" disabled={busy}>
          {busy && <span className="spinner" aria-hidden="true" />}
          {busy ? 'Saving…' : 'Save and continue'}
        </button>
        <p className="muted small center">
          <button type="button" className="btn btn-ghost btn-sm" onClick={logout}>
            Sign out
          </button>
        </p>
      </form>
    </div>
  )
}
