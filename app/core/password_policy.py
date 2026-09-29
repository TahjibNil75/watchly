"""What a new password must be. `frontend/src/password.js` shows the same rules
as a checklist while it is typed; keep the two in step.

Only passwords someone chooses are checked: signing in, and the temporary
passwords "forgot password" emails, are left alone.
"""

import re

MIN_LENGTH = 8
#: Shorter pieces of a name ("Al", "Li") would turn up in too many passwords.
MIN_IDENTITY_PART = 3

_WORD = re.compile(r"[^\W_]+")


class WeakPasswordError(ValueError):
    """The password misses one or more of the rules; the message lists them."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Choose a stronger password: " + "; ".join(problems) + ".")


def _identity_parts(username: str | None, email: str | None, full_name: str | None) -> set[str]:
    """The username, the email's local part and the full name, whole and word
    by word, lowercased."""
    local = (email or "").partition("@")[0]
    parts: set[str] = set()
    for value in (username, local, full_name):
        value = (value or "").strip().lower()
        parts.add(value)
        parts.update(_WORD.findall(value))
    return {part for part in parts if len(part) >= MIN_IDENTITY_PART}


def password_problems(
    password: str,
    *,
    username: str | None = None,
    email: str | None = None,
    full_name: str | None = None,
) -> list[str]:
    """Every rule `password` breaks, worded for the person choosing it."""
    problems = []
    # Spaces are allowed but do not count: eight spaces are not a password.
    if sum(not c.isspace() for c in password) < MIN_LENGTH:
        problems.append(f"use at least {MIN_LENGTH} characters, not counting spaces")
    if not any(c.isupper() for c in password):
        problems.append("add an uppercase letter")
    if not any(c.islower() for c in password):
        problems.append("add a lowercase letter")
    if not any(c.isdecimal() for c in password):
        problems.append("add a number")
    if not any(not c.isalnum() and not c.isspace() for c in password):
        problems.append("add a special character such as ! @ # $")
    lowered = password.lower()
    if any(part in lowered for part in _identity_parts(username, email, full_name)):
        problems.append("leave out your username, email and name")
    return problems


def check_password(
    password: str,
    *,
    username: str | None = None,
    email: str | None = None,
    full_name: str | None = None,
) -> None:
    """Raise :class:`WeakPasswordError` unless `password` keeps every rule."""
    problems = password_problems(password, username=username, email=email, full_name=full_name)
    if problems:
        raise WeakPasswordError(problems)
