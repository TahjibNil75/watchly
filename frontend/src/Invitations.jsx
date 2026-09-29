import { useState } from 'react'
import { api } from './api.js'
import { Empty, ErrorBanner, Loading, RolePill } from './components.jsx'
import { withoutSpaces } from './fields.js'
import { duration, timeAgo } from './format.js'
import { canManageInvitation, invitableRoles } from './roles.js'
import { useApi } from './useApi.js'

// No 'expired': the server deletes invitations once they expire.
const STATUSES = ['pending', 'accepted', 'revoked']
const STATUS_BADGE = {
  pending: 'badge-unknown',
  accepted: 'badge-up',
  revoked: 'badge-paused',
}

// What to tell the sender after an invitation is saved. The API saves it even
// when the email cannot go out, and says so in `email_sent`.
function sentNotice(invitation) {
  if (invitation.email_sent) {
    return { ok: true, message: `Invitation sent to ${invitation.email}.` }
  }
  return {
    ok: false,
    message: `Invitation for ${invitation.email} was saved, but the email could not be sent. Check the mail settings, then use Resend.`,
  }
}

function when(invitation) {
  switch (invitation.status) {
    case 'pending':
      return `expires in ${duration((new Date(invitation.expires_at) - Date.now()) / 1000)}`
    case 'accepted':
      return `accepted ${timeAgo(invitation.accepted_at)}`
    default:
      return `revoked ${timeAgo(invitation.revoked_at)}`
  }
}

/**
 * Only offers the roles `me` may grant, so the choice mirrors what the API will
 * accept. It defaults to the least privileged of them: the API has no default
 * role, and a slip here should never hand out more than intended.
 */
export function InviteForm({ me, onNotice, onSent, onCancel }) {
  const roles = invitableRoles(me)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState(roles[0])
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onNotice(sentNotice(await api.invite(email.trim(), role)))
      onSent()
      setEmail('')
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h2>Invite a user</h2>
      <ErrorBanner error={error} />
      <div className="row-2">
        <label className="field">
          <span>Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(withoutSpaces(e.target.value))}
            placeholder="name@company.com"
            autoComplete="off"
            autoFocus
            required
          />
        </label>
        <label className="field">
          <span>Role</span>
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            {roles.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </label>
      </div>
      <p className="muted small">
        They get an email with a one-time link to choose a username and password. Inviting an
        address that already has a pending invitation replaces it.
      </p>
      <div className="form-actions">
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Sending…' : 'Send invitation'}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onCancel}>
          Close
        </button>
      </div>
    </form>
  )
}

// `version` is bumped by the parent after a send, to reload the list.
export function InvitationList({ me, version, onNotice }) {
  const [status, setStatus] = useState('pending')
  const [pending, setPending] = useState(null)
  const [actionError, setActionError] = useState(null)
  const invitations = useApi(() => api.listInvitations({ status }), [status, version])
  const items = invitations.data?.items ?? []

  async function act(invitation, action) {
    setPending(invitation.id)
    setActionError(null)
    try {
      await action()
      await invitations.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setPending(null)
    }
  }

  return (
    <section className="section section-spaced">
      <div className="section-head">
        <div>
          <h2>Invitations</h2>
          <p className="muted small">
            Sent by email. Each link works once. Expired invitations are deleted, and only the
            latest accepted and revoked ones are kept.
          </p>
        </div>
        <div className="segmented" role="group" aria-label="Invitation status">
          {STATUSES.map((s) => (
            <button key={s} type="button" aria-pressed={status === s} onClick={() => setStatus(s)}>
              {s[0].toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>
      </div>

      <ErrorBanner error={actionError ?? invitations.error} />

      {invitations.loading ? (
        <Loading />
      ) : items.length === 0 ? (
        <Empty>No {status} invitations.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="people-table">
            <thead>
              <tr>
                <th>Invitee</th>
                <th>Role</th>
                <th>Status</th>
                <th>Invited by</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((inv) => {
                const busy = pending === inv.id
                const mine = canManageInvitation(me, inv)
                return (
                  <tr key={inv.id}>
                    <td>
                      <div className="person">
                        <span className="avatar avatar-invite" aria-hidden="true">
                          <svg
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="1.9"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                          >
                            <rect x="3.5" y="5.5" width="17" height="13" rx="2" />
                            <path d="m4 7 8 6 8-6" />
                          </svg>
                        </span>
                        <div className="person-text">
                          <strong className="truncate">{inv.email}</strong>
                          <div className="muted small">{when(inv)}</div>
                        </div>
                      </div>
                    </td>
                    <td>
                      <RolePill role={inv.role} />
                    </td>
                    <td>
                      <span className={`badge ${STATUS_BADGE[inv.status]}`}>{inv.status}</span>
                    </td>
                    <td>{inv.invited_by_name ?? <span className="muted">—</span>}</td>
                    <td className="right nowrap">
                      {inv.status === 'pending' && mine && (
                        <button
                          type="button"
                          className="btn btn-sm"
                          disabled={busy}
                          onClick={() =>
                            act(inv, async () => onNotice(sentNotice(await api.invite(inv.email, inv.role))))
                          }
                        >
                          Resend
                        </button>
                      )}{' '}
                      {inv.status === 'pending' && mine && (
                        <button
                          type="button"
                          className="btn btn-sm btn-danger"
                          disabled={busy}
                          onClick={() => {
                            if (window.confirm(`Revoke the invitation for ${inv.email}? The link stops working.`)) {
                              act(inv, () => api.revokeInvitation(inv.id))
                            }
                          }}
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {invitations.data.total > items.length && (
            <p className="muted small">
              Showing {items.length} of {invitations.data.total} invitations.
            </p>
          )}
        </div>
      )}
    </section>
  )
}
