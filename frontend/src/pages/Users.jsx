import { useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import {
  Avatar,
  Empty,
  ErrorBanner,
  Loading,
  PageHeader,
  RolePill,
  RoleSelect,
} from '../components.jsx'
import { dateTime, timeAgo } from '../format.js'
import { InvitationList, InviteForm } from '../Invitations.jsx'
import { ROLES, canChangeRole, canInvite, canManageUsers, canSuspend } from '../roles.js'
import { useApi } from '../useApi.js'

const STATUS_FILTERS = [
  { value: '', label: 'Everyone' },
  { value: 'true', label: 'Active' },
  { value: 'false', label: 'Suspended' },
]

const LOCK_ICON = (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <rect x="4.5" y="10.5" width="15" height="10" rx="2" />
    <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" />
  </svg>
)

export default function Users() {
  const { user: me } = useAuth()
  const allowed = canManageUsers(me)
  const [role, setRole] = useState('')
  const [active, setActive] = useState('')
  const [actionError, setActionError] = useState(null)
  const [pending, setPending] = useState(null)
  const [inviting, setInviting] = useState(false)
  const [notice, setNotice] = useState(null)
  // Bumped after an invitation is sent, so the invitation list reloads.
  const [invitesVersion, setInvitesVersion] = useState(0)

  const users = useApi(
    () => (allowed ? api.listUsers({ role, is_active: active }) : null),
    [allowed, role, active],
  )

  if (!allowed) {
    return <Empty>Only admins, DevOps and project managers can manage users.</Empty>
  }

  const items = users.data?.items ?? []

  async function update(target, action) {
    setPending(target.id)
    setActionError(null)
    try {
      const updated = await action()
      users.setData({
        ...users.data,
        items: items.map((u) => (u.id === updated.id ? updated : u)),
      })
    } catch (err) {
      setActionError(err)
    } finally {
      setPending(null)
    }
  }

  return (
    <>
      <PageHeader
        title="Users"
        subtitle="Invite people, change roles, and suspend or reactivate accounts."
      >
        {canInvite(me) && !inviting && (
          <button type="button" className="btn btn-primary" onClick={() => setInviting(true)}>
            Invite user
          </button>
        )}
      </PageHeader>

      {notice && (
        <div className={`banner ${notice.ok ? 'banner-info' : 'banner-error'}`} role="status">
          {notice.message}
        </div>
      )}

      {inviting && (
        <InviteForm
          me={me}
          onNotice={setNotice}
          onSent={() => setInvitesVersion((v) => v + 1)}
          onCancel={() => setInviting(false)}
        />
      )}

      <div className="toolbar">
        <label className="inline-field">
          <span>Role</span>
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="">All roles</option>
            {ROLES.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </label>
        <div className="segmented" role="group" aria-label="Status">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              aria-pressed={active === f.value}
              onClick={() => setActive(f.value)}
            >
              {f.label}
            </button>
          ))}
        </div>
        {users.data && (
          <span className="toolbar-count muted small">
            {users.data.total} {users.data.total === 1 ? 'user' : 'users'}
          </span>
        )}
      </div>

      <ErrorBanner error={actionError ?? users.error} />

      {users.loading ? (
        <Loading />
      ) : items.length === 0 ? (
        <Empty>No users match these filters.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="people-table">
            <thead>
              <tr>
                <th>User</th>
                <th>Role</th>
                <th>Status</th>
                <th>Last active</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((u) => {
                const busy = pending === u.id
                return (
                  <tr key={u.id} className={u.is_active ? undefined : 'is-suspended'}>
                    <td>
                      <div className="person">
                        <Avatar user={u} />
                        <div className="person-text">
                          <div className="person-name">
                            <strong>{u.full_name || u.username}</strong>
                            {u.id === me.id && <span className="you-pill">You</span>}
                          </div>
                          <div className="muted small truncate">
                            @{u.username} · {u.email}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td>
                      {canChangeRole(me, u) ? (
                        <RoleSelect
                          value={u.role}
                          disabled={busy}
                          label={`Role for ${u.username}`}
                          onChange={(r) => update(u, () => api.setRole(u.id, r))}
                        />
                      ) : (
                        <RolePill role={u.role} />
                      )}
                    </td>
                    <td>
                      <span className={`status-dot ${u.is_active ? 'is-up' : 'is-down'}`}>
                        {u.is_active ? 'Active' : 'Suspended'}
                      </span>
                      {u.locked_until && new Date(u.locked_until) > new Date() ? (
                        <div className="user-flag is-locked">
                          {LOCK_ICON}
                          Sign-in locked until {dateTime(u.locked_until)}
                        </div>
                      ) : (
                        u.failed_login_attempts > 0 && (
                          <div className="user-flag">
                            {u.failed_login_attempts} failed sign-in
                            {u.failed_login_attempts === 1 ? '' : 's'}
                          </div>
                        )
                      )}
                    </td>
                    <td className="nowrap" title={u.last_activity ? dateTime(u.last_activity) : undefined}>
                      {timeAgo(u.last_activity)}
                    </td>
                    <td className="right">
                      {canSuspend(me, u) &&
                        (u.is_active ? (
                          <button
                            type="button"
                            className="btn btn-sm btn-danger"
                            disabled={busy}
                            onClick={() => {
                              if (window.confirm(`Suspend ${u.username}? They'll be signed out immediately.`)) {
                                update(u, () => api.suspendUser(u.id))
                              }
                            }}
                          >
                            Suspend
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={busy}
                            onClick={() => update(u, () => api.reactivateUser(u.id))}
                          >
                            Reactivate
                          </button>
                        ))}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {users.data.total > items.length && (
            <p className="muted small">
              Showing {items.length} of {users.data.total} users.
            </p>
          )}
        </div>
      )}

      {canInvite(me) && (
        <InvitationList me={me} version={invitesVersion} onNotice={setNotice} />
      )}
    </>
  )
}
