import { useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { useApi } from '../useApi.js'
import { Avatar, ErrorBanner, PageHeader, PasswordChecklist, RolePill } from '../components.jsx'
import { withoutSpaces } from '../fields.js'
import { isStrongPassword, WEAK_PASSWORD } from '../password.js'
import { dateTime, timeAgo } from '../format.js'
import { roleClass } from '../roles.js'

// 24x24 strokes for the card heads, drawn like the sidebar's.
const ICONS = {
  details: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 20a8 8 0 0 1 16 0" />
    </>
  ),
  email: (
    <>
      <rect x="3.5" y="5.5" width="17" height="13" rx="2" />
      <path d="m4 7 8 6 8-6" />
    </>
  ),
  globe: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M3.5 12h17M12 3.5c2.4 2.5 3.6 5.3 3.6 8.5s-1.2 6-3.6 8.5c-2.4-2.5-3.6-5.3-3.6-8.5s1.2-6 3.6-8.5Z" />
    </>
  ),
  password: (
    <>
      <rect x="4.5" y="10.5" width="15" height="10" rx="2" />
      <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5M12 14.5v2" />
    </>
  ),
}

function Icon({ name }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICONS[name]}
    </svg>
  )
}

// A card's title beside its icon, like the notification cards.
function CardHead({ icon, title, note }) {
  return (
    <div className="card-head">
      <span className="card-icon">
        <Icon name={icon} />
      </span>
      <div>
        <h2>{title}</h2>
        <p className="muted small">{note}</p>
      </div>
    </div>
  )
}

const day = (iso) =>
  iso ? new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—'

// Who is signed in, in their role's colour, like the head of a website's page.
function ProfileHero({ user }) {
  return (
    <section className={`profile-hero ${roleClass(user.role)}`}>
      <div className="profile-hero-head">
        <Avatar user={user} className="avatar-lg" />
        <div className="profile-hero-title">
          <h2>
            {user.full_name || user.username}
            <RolePill role={user.role} />
          </h2>
          <p className="muted">
            @{user.username} · {user.email}
          </p>
        </div>
      </div>
      <dl className="profile-facts">
        <div>
          <dt>Member since</dt>
          <dd>{day(user.created_at)}</dd>
        </div>
        <div>
          <dt>Last active</dt>
          <dd>{timeAgo(user.last_activity)}</dd>
        </div>
        {user.pending_email && (
          <div>
            <dt>Email change</dt>
            <dd className="text-pending">Waiting for {user.pending_email}</dd>
          </div>
        )}
      </dl>
    </section>
  )
}

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
      <CardHead icon="details" title="Details" note="How your name shows to your team." />
      <ErrorBanner error={error} />
      <Notice>{notice}</Notice>
      <dl className="profile-fixed">
        <div>
          <dt>Username</dt>
          <dd>@{user.username}</dd>
        </div>
        <div>
          <dt>Role</dt>
          <dd>
            <RolePill role={user.role} />
          </dd>
        </div>
      </dl>
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
      <CardHead icon="email" title="Email" note="Where sign-in links and your alerts go." />
      <ErrorBanner error={error} />
      <Notice>{notice}</Notice>
      <div className="profile-current">
        <span className="muted small">Current address</span>
        <strong>{user.email}</strong>
      </div>
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
          onChange={(e) => setNewEmail(withoutSpaces(e.target.value))}
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

// "NP" as "Nepal"; the code itself if the browser does not know it.
const countryName = (code) => {
  try {
    return new Intl.DisplayNames(undefined, { type: 'region' }).of(code) ?? code
  } catch {
    return code
  }
}

// Where the account has been signed in from. Left out until the server reads a
// country (COUNTRY_HEADER), so an install without one never sees it.
function CountriesCard() {
  const { data } = useApi(() => api.myCountries(), [])
  if (!data || (!data.current && data.countries.length === 0)) return null
  return (
    <section className="card form">
      <CardHead
        icon="globe"
        title="Sign-in countries"
        note="Where your sign-ins appear to come from. A VPN shows as its own country."
      />
      {data.current && (
        <div className="profile-current">
          <span className="muted small">This connection</span>
          <strong>{countryName(data.current)}</strong>
        </div>
      )}
      {data.countries.length > 0 && (
        <dl className="profile-fixed">
          {data.countries.map((entry) => (
            <div key={entry.country}>
              <dt>{countryName(entry.country)}</dt>
              <dd>
                {entry.sign_ins} {entry.sign_ins === 1 ? 'sign-in' : 'sign-ins'}, last{' '}
                {timeAgo(entry.last_seen_at)}
              </dd>
            </div>
          ))}
        </dl>
      )}
      <p className="muted small">
        Don&apos;t recognise one of these? Change your password: it signs out every other session.
      </p>
    </section>
  )
}

const EMPTY_PASSWORDS = { current_password: '', new_password: '', confirm_password: '' }

function PasswordCard({ user }) {
  const [form, setForm] = useState(EMPTY_PASSWORDS)
  const { busy, error, notice, run } = useAction()
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value })
  const identity = { username: user.username, email: user.email, fullName: user.full_name }
  // Said once both are typed in, not on the first keystroke.
  const matches = form.confirm_password ? form.new_password === form.confirm_password : null

  const submit = (event) => {
    event.preventDefault()
    run(async () => {
      if (!isStrongPassword(form.new_password, identity)) {
        throw new Error(WEAK_PASSWORD)
      }
      if (form.new_password !== form.confirm_password) {
        throw new Error('The new passwords do not match.')
      }
      await api.changePassword(form)
      setForm(EMPTY_PASSWORDS)
      return 'Password changed. Your other devices have been signed out.'
    })
  }

  return (
    <form className="card form" onSubmit={submit}>
      <CardHead
        icon="password"
        title="Password"
        note="Changing it signs you out everywhere else."
      />
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
            aria-describedby="password-rules"
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
      <PasswordChecklist id="password-rules" password={form.new_password} identity={identity} />
      {matches !== null && (
        <p className={`match-hint ${matches ? 'is-ok' : 'is-bad'}`}>
          {matches ? 'The new passwords match.' : 'The new passwords do not match yet.'}
        </p>
      )}
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
      <ProfileHero user={user} />
      <div className="grid-2 profile-grid">
        <DetailsCard user={user} updateUser={updateUser} />
        <EmailCard user={user} updateUser={updateUser} />
      </div>
      <PasswordCard user={user} />
      <CountriesCard />
    </>
  )
}
