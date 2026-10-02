// Thin wrapper over fetch for the Watchly API.
//
// In development VITE_API_URL is left blank and Vite proxies /api to FastAPI
// (see vite.config.js). Set it to the API's origin for a deployed build.
//
// The short-lived access token is kept here and sent as a bearer header. The
// refresh token is an httpOnly cookie that this code never sees: on a 401 we
// trade it for a new access token at /auth/refresh and retry once.
const API_ORIGIN = import.meta.env.VITE_API_URL ?? ''
const TOKEN_KEY = 'watchly.accessToken'

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // Storage unavailable (private mode); the session just won't survive a reload.
  }
}

// Called when the session is over and can't be refreshed. AuthProvider
// registers the handler that signs the user out.
let onUnauthorized = () => {}
export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler
}

// FastAPI sends {"detail": "..."} for most errors and a list of
// {loc, msg} objects for 422 validation errors.
function describeError(detail) {
  if (!detail) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((err) => {
        const field = (err.loc ?? []).filter((part) => part !== 'body').join('.')
        return field ? `${field}: ${err.msg}` : err.msg
      })
      .join('; ')
  }
  return JSON.stringify(detail)
}

async function send(url, init) {
  try {
    return await fetch(url, init)
  } catch {
    throw new ApiError(0, 'Cannot reach the Watchly API. Is the backend running?')
  }
}

let refreshing = null

// Trade the refresh cookie for a new access token. Resolves to the token, or to
// null when the session is over.
//
// The server rotates the cookie on every use and treats a second use of the
// old one as theft, ending the session. So only one refresh may be in flight:
// requests in this tab share one, and a Web Lock queues other tabs behind it.
function refreshAccessToken(staleToken) {
  refreshing ??= withLock('watchly.refresh', async () => {
    // Another tab may have refreshed while this one waited for the lock.
    const current = getToken()
    if (current && current !== staleToken) return current

    const res = await send(new URL(API_ORIGIN + '/api/v1/auth/refresh', window.location.origin), {
      method: 'POST',
    })
    if (res.status === 401 || res.status === 403) return null
    // Anything else is the API having a bad moment, not the session ending.
    if (!res.ok) throw new ApiError(res.status, `Could not renew your session (${res.status}).`)
    const { access_token: accessToken } = await res.json()
    setToken(accessToken)
    return accessToken
  }).finally(() => {
    refreshing = null
  })
  return refreshing
}

function withLock(name, callback) {
  return navigator.locks ? navigator.locks.request(name, callback) : callback()
}

// `text: true` returns a successful body as a string instead of parsing JSON,
// for downloads; errors are JSON either way.
async function request(path, { method = 'GET', body, query, text = false } = {}) {
  const url = new URL(API_ORIGIN + path, window.location.origin)
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, value)
    }
  }

  const call = (token) => {
    const headers = {}
    if (token) headers.Authorization = `Bearer ${token}`
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    return send(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  }

  const token = getToken()
  let res = await call(token)
  // Most likely the access token expired: refresh it and retry, once.
  if (res.status === 401 && token) {
    const fresh = await refreshAccessToken(token)
    if (fresh) res = await call(fresh)
  }

  if (res.status === 204) return null
  if (text && res.ok) return res.text()
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    if (res.status === 401 && token) onUnauthorized()
    throw new ApiError(res.status, describeError(data?.detail) || `${res.status} ${res.statusText}`)
  }
  return data
}

const v1 = (path, options) => request(`/api/v1${path}`, options)

export const api = {
  health: () => request('/health'),

  login: (identifier, password) =>
    v1('/auth/login', { method: 'POST', body: { identifier, password } }),
  signup: (payload) => v1('/auth/signup', { method: 'POST', body: payload }),
  // { open }: false once the admin exists; after that people join by invitation.
  signupStatus: () => v1('/auth/signup'),
  // Revokes the session in the refresh cookie, which only the API can clear.
  logout: () => v1('/auth/logout', { method: 'POST' }),
  forgotPassword: (email) => v1('/auth/forgot-password', { method: 'POST', body: { email } }),

  me: () => v1('/users/me'),
  updateMe: (payload) => v1('/users/me', { method: 'PATCH', body: payload }),
  // Where you have signed in from; empty unless the server reads a country header.
  myCountries: () => v1('/users/me/countries'),
  changePassword: (payload) => v1('/users/me/password', { method: 'POST', body: payload }),
  // A new address only takes over once the link emailed to it is opened;
  // confirmEmail is that link's call, and needs no sign-in.
  requestEmailChange: (newEmail, currentPassword) =>
    v1('/users/me/email', {
      method: 'POST',
      body: { new_email: newEmail, current_password: currentPassword },
    }),
  cancelEmailChange: () => v1('/users/me/email', { method: 'DELETE' }),
  confirmEmail: (token) => v1('/auth/confirm-email', { method: 'POST', body: { token } }),
  listUsers: (query) => v1('/users', { query: { limit: 100, ...query } }),
  setRole: (id, role) => v1(`/users/${id}/role`, { method: 'PATCH', body: { role } }),
  suspendUser: (id) => v1(`/users/${id}/suspend`, { method: 'PATCH' }),
  reactivateUser: (id) => v1(`/users/${id}/reactivate`, { method: 'PATCH' }),

  // Inviting: admin/DevOps send and manage; the invitee's two calls are public
  // and authenticated by the token from the email.
  invite: (email, role) => v1('/invitations', { method: 'POST', body: { email, role } }),
  listInvitations: (query) => v1('/invitations', { query: { limit: 100, ...query } }),
  revokeInvitation: (id) => v1(`/invitations/${id}/revoke`, { method: 'PATCH' }),
  previewInvitation: (token) => v1('/invitations/preview', { method: 'POST', body: { token } }),
  acceptInvitation: (payload) => v1('/invitations/accept', { method: 'POST', body: payload }),

  // Asking for an account is public; admin/DevOps answer. Approving sends an
  // invitation to join as a Viewer, rejecting emails the refusal.
  requestAccount: (payload) => v1('/account-requests', { method: 'POST', body: payload }),
  listAccountRequests: (query) => v1('/account-requests', { query: { limit: 100, ...query } }),
  approveAccountRequest: (id) => v1(`/account-requests/${id}/approve`, { method: 'PATCH' }),
  rejectAccountRequest: (id) => v1(`/account-requests/${id}/reject`, { method: 'PATCH' }),
  // Undoes a rejection, so that address may ask again.
  unblockAccountRequest: (id) => v1(`/account-requests/${id}/unblock`, { method: 'PATCH' }),

  listProjects: (query) => v1('/monitoring/projects', { query: { limit: 100, ...query } }),
  getProject: (id) => v1(`/monitoring/projects/${id}`),
  createProject: (payload) => v1('/monitoring/projects', { method: 'POST', body: payload }),
  updateProject: (id, payload) =>
    v1(`/monitoring/projects/${id}`, { method: 'PATCH', body: payload }),
  deleteProject: (id) => v1(`/monitoring/projects/${id}`, { method: 'DELETE' }),
  addMembers: (id, memberIds) =>
    v1(`/monitoring/projects/${id}/members`, {
      method: 'POST',
      body: { member_ids: memberIds },
    }),
  removeMember: (id, userId) =>
    v1(`/monitoring/projects/${id}/members/${userId}`, { method: 'DELETE' }),

  listWebsites: (query) => v1('/monitoring/websites', { query: { limit: 100, ...query } }),
  // Counts by state over the same sites as listWebsites with the same project_id, check_type and q.
  websiteSummary: (query) => v1('/monitoring/websites/summary', { query }),
  // Outages, recoveries, slow spells, packet loss, DNS changes and expiring certificates, newest first.
  // after_id: only events newer than that one.
  websiteEvents: (query) => v1('/monitoring/websites/events', { query }),
  getWebsite: (id) => v1(`/monitoring/websites/${id}`),
  createWebsite: (payload) => v1('/monitoring/websites', { method: 'POST', body: payload }),
  updateWebsite: (id, payload) =>
    v1(`/monitoring/websites/${id}`, { method: 'PATCH', body: payload }),
  deleteWebsite: (id) => v1(`/monitoring/websites/${id}`, { method: 'DELETE' }),
  addRecipients: (id, recipientIds) =>
    v1(`/monitoring/websites/${id}/recipients`, {
      method: 'POST',
      body: { recipient_ids: recipientIds },
    }),
  removeRecipient: (id, userId) =>
    v1(`/monitoring/websites/${id}/recipients/${userId}`, { method: 'DELETE' }),
  listChecks: (id, limit = 50) => v1(`/monitoring/websites/${id}/checks`, { query: { limit } }),
  // range: 24h | 7d | 30d | 90d
  getWebsiteStats: (id, range) => v1(`/monitoring/websites/${id}/stats`, { query: { range } }),
  websiteStatsCsv: (id, range) =>
    v1(`/monitoring/websites/${id}/stats`, { query: { range, format: 'csv' }, text: true }),
  checkNow: (id) => v1(`/monitoring/websites/${id}/check`, { method: 'POST' }),
  // No checks or alerts while a maintenance window is on. { duration_minutes }
  // starts one now; { starts_at, ends_at } schedules one. Each returns the site.
  startMaintenance: (id, payload) =>
    v1(`/monitoring/websites/${id}/maintenance`, { method: 'POST', body: payload }),
  endMaintenance: (id) => v1(`/monitoring/websites/${id}/maintenance/end`, { method: 'POST' }),
  cancelMaintenance: (id, windowId) =>
    v1(`/monitoring/websites/${id}/maintenance/${windowId}`, { method: 'DELETE' }),

  // Notification wording and switches. Global = the admin's defaults; a
  // project's own settings override them field by field.
  notificationSettings: () => v1('/monitoring/notifications'),
  saveNotification: (kind, payload) =>
    v1(`/monitoring/notifications/${kind}`, { method: 'PUT', body: payload }),
  resetNotification: (kind) => v1(`/monitoring/notifications/${kind}`, { method: 'DELETE' }),
  projectNotificationSettings: (id) => v1(`/monitoring/projects/${id}/notifications`),
  saveProjectNotification: (id, kind, payload) =>
    v1(`/monitoring/projects/${id}/notifications/${kind}`, { method: 'PUT', body: payload }),
  resetProjectNotification: (id, kind) =>
    v1(`/monitoring/projects/${id}/notifications/${kind}`, { method: 'DELETE' }),
  previewNotification: (payload) =>
    v1('/monitoring/notifications/preview', { method: 'POST', body: payload }),
  // month: 'YYYY-MM', a month that has ended.
  projectReportCsv: (id, month) =>
    v1(`/monitoring/projects/${id}/report.csv`, { query: { month }, text: true }),
  sendReport: (id, month) =>
    v1(`/monitoring/projects/${id}/report`, { method: 'POST', body: month ? { month } : {} }),
}
