import { useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { ErrorBanner, PageHeader } from '../components.jsx'
import { dateTime } from '../format.js'

// Each card is its own form, with its own busy state, error and notice.
function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)

  async function run(action) {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      setNotice(await action())
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return { busy, error, notice, run }
}

function Notice({ children }) {
  return children ? (
    <div className="banner banner-info" role="status">
      {children}
    </div>
  ) : null
}

function DetailsCard({ user, updateUser }) {
  const [fullName, setFullName] = useState(user.full_name ?? '')
  const { busy, error, notice, run } = useAction()

  const submit = (event) => {
    event.preventDefault()
    run(async () => {
      updateUser(await api.updateMe({ full_name: fullName }))
      return 'Saved.'
    })
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h2>Details</h2>
      <ErrorBanner error={error} />
      <Notice>{notice}</Notice>
      <div className="row-2">
        <label className="field">
          <span>Username</span>
          <input value={user.username} readOnly disabled />
        </label>
        <label className="field">
          <span>Role</span>
          <input value={user.role} readOnly disabled />
        </label>
      </div>
      <label className="field">
        <span>Full name</span>
        <input
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
          autoComplete="name"
          maxLength={255}
        />
      </label>
      <p className="muted small">Your username and role can only be changed by an administrator.</p>
      <div className="form-actions">
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>
    </form>
  )
}

function EmailCard({ user, updateUser }) {
  const [newEmail, setNewEmail] = useState('')
  const [password, setPassword] = useState('')
  const { busy, error, notice, run } = useAction()

  const pending = user.pending_email
  const expiresAt = user.pending_email_expires_at
  const expired = expiresAt && new Date(expiresAt) <= new Date()

  const submit = (event) => {
    event.preventDefault()
    run(async () => {
      const { email_sent: sent, ...profile } = await api.requestEmailChange(newEmail.trim(), password)
      updateUser(profile)
      setNewEmail('')
      setPassword('')
      return sent
        ? `We emailed a link to ${profile.pending_email}. Open it to finish the change.`
        : `The change is saved, but the email to ${profile.pending_email} could not be sent. Check the mail settings, then send it again.`
    })
  }

  const cancel = () =>
    run(async () => {
      updateUser(await api.cancelEmailChange())
      return 'Email change cancelled.'
    })

  return (
    <form className="card form" onSubmit={submit}>
      <h2>Email</h2>
      <ErrorBanner error={error} />
      <Notice>{notice}</Notice>
      <label className="field">
        <span>Current address</span>
        <input type="email" value={user.email} readOnly disabled />
      </label>
      {pending && (
        <div className={`banner ${expired ? 'banner-error' : 'banner-info'}`}>
          {expired
            ? `The link to confirm ${pending} expired ${dateTime(expiresAt)}. Send a new one, or cancel.`
            : `Waiting for you to confirm ${pending}, using the link we emailed to it. It works until ${dateTime(expiresAt)}.`}{' '}
          <button type="button" className="btn btn-sm btn-ghost" onClick={cancel} disabled={busy}>
            Cancel change
          </button>
        </div>
      )}
      <label className="field">
        <span>New address</span>
        <input
          type="email"
          value={newEmail}
          onChange={(e) => setNewEmail(e.target.value)}
          autoComplete="email"
          required
        />
      </label>
      <label className="field">
        <span>Current password</span>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />
      </label>
      <p className="muted small">
        Your address changes once you open the link we send to the new one. Until then, alerts and
        sign-in keep using the current address.
      </p>
      <div className="form-actions">
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Sending…' : 'Send confirmation link'}
        </button>
      </div>
    </form>
  )
}

const EMPTY_PASSWORDS = { current_password: '', new_password: '', confirm_password: '' }

function PasswordCard() {
  const [form, setForm] = useState(EMPTY_PASSWORDS)
  const { busy, error, notice, run } = useAction()
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value })

  const submit = (event) => {
    event.preventDefault()
    run(async () => {
      if (form.new_password !== form.confirm_password) {
        throw new Error('The new passwords do not match.')
      }
      await api.changePassword(form)
      setForm(EMPTY_PASSWORDS)
      return 'Password changed. Use the new one next time you sign in.'
    })
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h2>Password</h2>
      <ErrorBanner error={error} />
      <Notice>{notice}</Notice>
      <label className="field">
        <span>Current password</span>
        <input
          type="password"
          value={form.current_password}
          onChange={set('current_password')}
          autoComplete="current-password"
          required
        />
      </label>
      <div className="row-2">
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
      </div>
      <div className="form-actions">
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Changing…' : 'Change password'}
        </button>
      </div>
    </form>
  )
}

export default function Profile() {
  const { user, updateUser } = useAuth()
  return (
    <>
      <PageHeader title="Profile" subtitle="Your name, email address and password." />
      <div className="grid-2">
        <DetailsCard user={user} updateUser={updateUser} />
        <EmailCard user={user} updateUser={updateUser} />
      </div>
      <PasswordCard />
    </>
  )
}
