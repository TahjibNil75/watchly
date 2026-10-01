import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import BrandMark from '../BrandMark.jsx'
import { ErrorBanner } from '../components.jsx'
import { withoutSpaces } from '../fields.js'

export default function RequestAccount() {
  const [form, setForm] = useState({ email: '', full_name: '', message: '' })
  const [done, setDone] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const set =
    (key, clean = (value) => value) =>
    (e) =>
      setForm({ ...form, [key]: clean(e.target.value) })

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      // The API answers the same whether or not it saved the request.
      const { detail } = await api.requestAccount({
        email: form.email.trim(),
        full_name: form.full_name.trim() || null,
        message: form.message.trim() || null,
      })
      setDone(detail)
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
        <h1>Request an account</h1>
        {done ? (
          <div className="banner banner-info" role="status">
            {done}
          </div>
        ) : (
          <>
            <p className="muted small">
              An admin or DevOps user reviews your request. If they approve it, you get an email
              with a link to create your account, which starts as a viewer. A rejected request
              cannot be sent again.
            </p>
            <ErrorBanner error={error} />
            <label className="field">
              <span>Email</span>
              <input
                type="email"
                value={form.email}
                onChange={set('email', withoutSpaces)}
                autoComplete="email"
                autoFocus
                required
              />
            </label>
            <label className="field">
              <span>
                Full name <span className="muted">(optional)</span>
              </span>
              <input
                value={form.full_name}
                onChange={set('full_name')}
                autoComplete="name"
                maxLength={255}
              />
            </label>
            <label className="field">
              <span>
                Why you need an account <span className="muted">(optional)</span>
              </span>
              <textarea
                value={form.message}
                onChange={set('message')}
                rows={3}
                maxLength={500}
              />
            </label>
            <button className="btn btn-primary btn-block" disabled={busy}>
              {busy && <span className="spinner" aria-hidden="true" />}
              {busy ? 'Sending…' : 'Request an account'}
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
