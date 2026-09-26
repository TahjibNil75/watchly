import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import { ErrorBanner } from '../components.jsx'

export default function Signup() {
  const { signup } = useAuth()
  const navigate = useNavigate()
  const [form, setForm] = useState({
    username: '',
    email: '',
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
      await signup({ ...form, full_name: form.full_name.trim() || null })
      navigate('/', { replace: true })
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  return (
    <div className="auth-screen">
      <form className="card auth-card" onSubmit={submit}>
        <Link to="/" className="brand brand-lg">
          <span className="brand-mark" aria-hidden="true" />
          Watchly
        </Link>
        <h1>Create an account</h1>
        <p className="muted small">
          New accounts start as viewers. An admin can add you to projects or change your role.
          On a fresh install, the first account becomes the admin.
        </p>
        <ErrorBanner error={error} />
        <label className="field">
          <span>Username</span>
          <input
            value={form.username}
            onChange={set('username')}
            autoComplete="username"
            required
            minLength={3}
            maxLength={50}
          />
        </label>
        <label className="field">
          <span>Email</span>
          <input
            type="email"
            value={form.email}
            onChange={set('email')}
            autoComplete="email"
            required
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
          {busy && <span className="spinner" aria-hidden="true" />}
          {busy ? 'Creating account…' : 'Create account'}
        </button>
        <p className="muted small center">
          Already have an account? <Link to="/login">Sign in</Link>
        </p>
      </form>
    </div>
  )
}
