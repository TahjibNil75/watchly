import { useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Empty, ErrorBanner, Loading, PageHeader } from '../components.jsx'
import { dateTime, timeAgo } from '../format.js'
import { InvitationList, InviteForm } from '../Invitations.jsx'
import { ROLES, canChangeRole, canInvite, canManageUsers, canSuspend } from '../roles.js'
import { useApi } from '../useApi.js'

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
        <label className="inline-field">
          <span>Status</span>
          <select value={active} onChange={(e) => setActive(e.target.value)}>
            <option value="">Everyone</option>
            <option value="true">Active</option>
            <option value="false">Suspended</option>
          </select>
        </label>
      </div>

      <ErrorBanner error={actionError ?? users.error} />

      {users.loading ? (
        <Loading />
      ) : items.length === 0 ? (
        <Empty>No users match these filters.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
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
                  <tr key={u.id}>
                    <td>
                      <strong>{u.full_name || u.username}</strong>
                      {u.id === me.id && <span className="muted small"> (you)</span>}
                      <div className="muted small">
                        @{u.username} · {u.email}
                      </div>
                    </td>
                    <td>
                      {canChangeRole(me, u) ? (
                        <select
                          value={u.role}
                          disabled={busy}
                          onChange={(e) => update(u, () => api.setRole(u.id, e.target.value))}
                        >
                          {ROLES.map((r) => (
                            <option key={r}>{r}</option>
                          ))}
                        </select>
                      ) : (
                        u.role
                      )}
                    </td>
                    <td>
                      {u.is_active ? (
                        <span className="badge badge-up">active</span>
                      ) : (
                        <span className="badge badge-down">suspended</span>
                      )}
                      {u.locked_until && new Date(u.locked_until) > new Date() ? (
                        <div className="muted small">
                          sign-in locked until {dateTime(u.locked_until)}
                        </div>
                      ) : (
                        u.failed_login_attempts > 0 && (
                          <div className="muted small">
                            {u.failed_login_attempts} failed sign-in
                            {u.failed_login_attempts === 1 ? '' : 's'}
                          </div>
                        )
                      )}
                    </td>
                    <td className="nowrap">{timeAgo(u.last_activity)}</td>
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
