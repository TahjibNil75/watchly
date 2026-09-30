import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth, useSignupOpen } from '../auth.jsx'
import BrandMark from '../BrandMark.jsx'
import { ErrorBanner, Loading, PasswordChecklist } from '../components.jsx'
import { USERNAME_MAX_LENGTH, USERNAME_MIN_LENGTH, withoutSpaces } from '../fields.js'
import { isStrongPassword, WEAK_PASSWORD } from '../password.js'

export default function Signup() {
  const { signup } = useAuth()
  const open = useSignupOpen()
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

  const set =
    (key, clean = (value) => value) =>
    (e) =>
      setForm({ ...form, [key]: clean(e.target.value) })
  const identity = { username: form.username, email: form.email, fullName: form.full_name }

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
      await signup({ ...form, full_name: form.full_name.trim() || null })
      navigate('/', { replace: true })
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  if (open === null) {
    return (
      <div className="auth-screen">
        <Loading />
      </div>
    )
  }

  if (!open) {
    return (
      <div className="auth-screen">
        <div className="card auth-card">
          <Link to="/" className="brand brand-lg">
            <BrandMark />
            <span className="brand-word">Watchly</span>
          </Link>
          <h1>Invitation only</h1>
          <p className="muted small">
            This Watchly doesn&apos;t take sign-ups. Ask an admin or DevOps user to invite you;
            the email they send has a link to create your account.
          </p>
          <p className="muted small center">
            Already have an account? <Link to="/login">Sign in</Link>
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="auth-screen">
      <form className="card auth-card" onSubmit={submit}>
        <Link to="/" className="brand brand-lg">
          <BrandMark />
          <span className="brand-word">Watchly</span>
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
            onChange={set('username', withoutSpaces)}
            autoComplete="username"
            required
            minLength={USERNAME_MIN_LENGTH}
            maxLength={USERNAME_MAX_LENGTH}
          />
        </label>
        <label className="field">
          <span>Email</span>
          <input
            type="email"
            value={form.email}
            onChange={set('email', withoutSpaces)}
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
        <p className="muted small center">
          Already have an account? <Link to="/login">Sign in</Link>
        </p>
      </form>
    </div>
  )
}
