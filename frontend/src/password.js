// What a new password must be, as a checklist ticked off while it is typed.
// The API holds the same rules (app/core/password_policy.py); keep the two in step.

export const MIN_LENGTH = 8
// Shorter pieces of a name ("Al", "Li") would turn up in too many passwords.
const MIN_IDENTITY_PART = 3

// The username, the email's local part and the full name, whole and word by
// word, lowercased.
function identityParts({ username, email, fullName }) {
  const local = (email ?? '').split('@')[0]
  const parts = []
  for (const value of [username, local, fullName]) {
    const lowered = (value ?? '').trim().toLowerCase()
    parts.push(lowered, ...(lowered.match(/[\p{L}\p{N}]+/gu) ?? []))
  }
  return parts.filter((part) => part.length >= MIN_IDENTITY_PART)
}

/**
 * Each rule with whether `password` meets it. `identity` is whatever is known
 * of the account so far: { username, email, fullName }.
 */
export function passwordRules(password, identity = {}) {
  const lowered = password.toLowerCase()
  // Spaces are allowed but do not count: eight spaces are not a password.
  const counted = password.replace(/\s/gu, '').length
  return [
    { key: 'length', label: `At least ${MIN_LENGTH} characters (spaces don't count)`, met: counted >= MIN_LENGTH },
    { key: 'upper', label: 'An uppercase letter', met: /\p{Lu}/u.test(password) },
    { key: 'lower', label: 'A lowercase letter', met: /\p{Ll}/u.test(password) },
    { key: 'number', label: 'A number', met: /\p{Nd}/u.test(password) },
    {
      key: 'special',
      label: 'A special character, like ! @ # $',
      met: /[^\p{L}\p{N}\s]/u.test(password),
    },
    {
      key: 'identity',
      label: 'Not your username, email or name',
      met: password.length > 0 && !identityParts(identity).some((part) => lowered.includes(part)),
    },
  ]
}

export const isStrongPassword = (password, identity) =>
  passwordRules(password, identity).every((rule) => rule.met)

export const WEAK_PASSWORD = 'Your password does not meet every requirement yet.'
