// Limits on what a new account's username may be; the API holds the same
// (app/schemas/user.py). Older accounts may have longer ones.
export const USERNAME_MIN_LENGTH = 4
export const USERNAME_MAX_LENGTH = 12

// For username and email inputs: spaces are never part of either, so typing
// or pasting one does nothing. The API drops them too.
export const withoutSpaces = (value) => value.replace(/\s/gu, '')
