// Mirrors app/core/permissions.py so the UI only offers actions that will
// succeed. These are hints, not security: the API enforces every rule itself.

export const ROLES = ['Viewer', 'Developer', 'Project Manager', 'DevOps', 'Admin']

const ROLE_MANAGERS = ['Admin', 'DevOps']
const USER_MANAGERS = [...ROLE_MANAGERS, 'Project Manager']

const SUSPENDABLE_BY = {
  admin: ROLES,
  DevOps: ['Viewer', 'Developer', 'Project Manager'],
  'Project Manager': ['DevOps', 'Developer', 'Viewer'],
}

// Which roles each actor may hand out when inviting.
const INVITABLE_BY = {
  Admin: ROLES,
  DevOps: ['Viewer', 'Developer', 'Project Manager', 'DevOps'],
}

export const canManageUsers = (user) => USER_MANAGERS.includes(user.role)

// PROJECT_CREATORS is currently the same set as USER_MANAGERS.
export const canCreateProjects = (user) => USER_MANAGERS.includes(user.role)

// GLOBAL_VIEWERS: everyone else sees only the projects they own or were added
// to, and the sites they were added to.
export const canViewAllProjects = (user) => ROLE_MANAGERS.includes(user.role)

// The defaults every project inherits. Projects are edited by whoever can
// manage them, via canManageProject.
export const canEditNotificationDefaults = (user) => ROLE_MANAGERS.includes(user.role)

export function canManageProject(user, project) {
  if (ROLE_MANAGERS.includes(user.role)) return true
  return user.role === 'Project Manager' && project?.owner_id === user.id
}

export function canChangeRole(actor, target) {
  if (actor.id === target.id) return false
  if (actor.role === 'Admin') return true
  if (actor.role === 'DevOps') return !ROLE_MANAGERS.includes(target.role)
  return false
}

export function canSuspend(actor, target) {
  if (actor.id === target.id) return false
  return (SUSPENDABLE_BY[actor.role] ?? []).includes(target.role)
}

// In ROLES order, least privileged first, so a form can default to the first.
export const invitableRoles = (user) => INVITABLE_BY[user.role] ?? []

export const canInvite = (user) => invitableRoles(user).length > 0

// Replacing or withdrawing an invitation takes the right to have sent it.
export const canManageInvitation = (user, invitation) =>
  invitableRoles(user).includes(invitation.role)
