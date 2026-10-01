import { useState } from 'react'
import { api } from './api.js'
import { Empty, ErrorBanner, Loading } from './components.jsx'
import { timeAgo } from './format.js'
import { useApi } from './useApi.js'

const STATUSES = ['pending', 'approved', 'rejected']
const STATUS_BADGE = {
  pending: 'badge-unknown',
  approved: 'badge-up',
  rejected: 'badge-paused',
}

// What to tell whoever answered a request. The API saves the answer even when
// the email cannot go out, and says so in `email_sent`.
function decidedNotice(request) {
  const approved = request.status === 'approved'
  if (request.email_sent) {
    return {
      ok: true,
      message: approved
        ? `Approved. ${request.email} was emailed a link to create a Viewer account.`
        : `Rejected. ${request.email} was told by email and cannot ask again.`,
    }
  }
  return {
    ok: false,
    message: approved
      ? `The request from ${request.email} was approved, but the email could not be sent. Check the mail settings, then use Resend on their invitation.`
      : `The request from ${request.email} was rejected, but the email telling them could not be sent.`,
  }
}

function when(request) {
  switch (request.status) {
    case 'pending':
      return `asked ${timeAgo(request.created_at)}`
    case 'approved':
      return `approved ${timeAgo(request.approved_at)}`
    default:
      return `rejected ${timeAgo(request.rejected_at)}`
  }
}

// `onApproved` lets the parent reload the invitation list, which an approval
// adds to.
export function AccountRequestList({ onNotice, onApproved }) {
  const [status, setStatus] = useState('pending')
  const [pending, setPending] = useState(null)
  const [actionError, setActionError] = useState(null)
  const requests = useApi(() => api.listAccountRequests({ status }), [status])
  const items = requests.data?.items ?? []

  async function unblock(request) {
    setPending(request.id)
    setActionError(null)
    try {
      await api.unblockAccountRequest(request.id)
      onNotice({ ok: true, message: `${request.email} is unblocked and may ask for an account again.` })
      await requests.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setPending(null)
    }
  }

  async function decide(request, action) {
    setPending(request.id)
    setActionError(null)
    try {
      const decided = await action()
      onNotice(decidedNotice(decided))
      if (decided.status === 'approved') onApproved()
      await requests.reload()
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
          <h2>Account requests</h2>
          <p className="muted small">
            Sent from the sign-in page by people without an account. Approving emails them a
            link to create one as a Viewer. Rejecting is final: they are emailed the refusal and
            cannot ask again unless you unblock them, and an invitation from an admin or DevOps
            user gives them access either way.
          </p>
        </div>
        <div className="segmented" role="group" aria-label="Account request status">
          {STATUSES.map((s) => (
            <button key={s} type="button" aria-pressed={status === s} onClick={() => setStatus(s)}>
              {s[0].toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>
      </div>

      <ErrorBanner error={actionError ?? requests.error} />

      {requests.loading ? (
        <Loading />
      ) : items.length === 0 ? (
        <Empty>No {status} account requests.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="people-table">
            <thead>
              <tr>
                <th>From</th>
                <th>Message</th>
                <th>Status</th>
                <th>Answered by</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((request) => {
                const busy = pending === request.id
                return (
                  <tr key={request.id}>
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
                            <circle cx="12" cy="8.5" r="3.5" />
                            <path d="M5 19.5a7 7 0 0 1 14 0" />
                          </svg>
                        </span>
                        <div className="person-text">
                          <strong className="truncate">{request.full_name || request.email}</strong>
                          <div className="muted small truncate">
                            {request.full_name ? `${request.email} · ` : ''}
                            {when(request)}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="request-message">
                      {request.message ?? <span className="muted">—</span>}
                    </td>
                    <td>
                      <span className={`badge ${STATUS_BADGE[request.status]}`}>{request.status}</span>
                    </td>
                    <td>{request.decided_by_name ?? <span className="muted">—</span>}</td>
                    <td className="right nowrap">
                      {request.status === 'pending' && (
                        <>
                          <button
                            type="button"
                            className="btn btn-sm btn-primary"
                            disabled={busy}
                            onClick={() => decide(request, () => api.approveAccountRequest(request.id))}
                          >
                            Approve
                          </button>{' '}
                          <button
                            type="button"
                            className="btn btn-sm btn-danger"
                            disabled={busy}
                            onClick={() => {
                              if (window.confirm(`Reject the request from ${request.email}? They are told by email and cannot ask again unless you unblock them.`)) {
                                decide(request, () => api.rejectAccountRequest(request.id))
                              }
                            }}
                          >
                            Reject
                          </button>
                        </>
                      )}
                      {request.status === 'rejected' && (
                        <button
                          type="button"
                          className="btn btn-sm"
                          disabled={busy}
                          onClick={() => {
                            if (window.confirm(`Unblock ${request.email}? They may ask for an account again.`)) {
                              unblock(request)
                            }
                          }}
                        >
                          Unblock
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {requests.data.total > items.length && (
            <p className="muted small">
              Showing {items.length} of {requests.data.total} account requests.
            </p>
          )}
        </div>
      )}
    </section>
  )
}
