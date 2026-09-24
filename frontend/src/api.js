// Thin wrapper over fetch for the Watchly API.
//
// In development VITE_API_URL is left blank and Vite proxies /api to FastAPI
// (see vite.config.js). Set it to the API's origin for a deployed build.
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

// There is no refresh endpoint yet, so an expired access token means signing
// in again. AuthProvider registers the handler that does that.
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

async function request(path, { method = 'GET', body, query } = {}) {
  const url = new URL(API_ORIGIN + path, window.location.origin)
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, value)
    }
  }

  const headers = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let res
  try {
    res = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, 'Cannot reach the Watchly API. Is the backend running?')
  }

  if (res.status === 204) return null
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
  forgotPassword: (email) => v1('/auth/forgot-password', { method: 'POST', body: { email } }),

  me: () => v1('/users/me'),
  updateMe: (payload) => v1('/users/me', { method: 'PATCH', body: payload }),
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
  checkNow: (id) => v1(`/monitoring/websites/${id}/check`, { method: 'POST' }),

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
  sendReport: (id, month) =>
    v1(`/monitoring/projects/${id}/report`, { method: 'POST', body: month ? { month } : {} }),
}
