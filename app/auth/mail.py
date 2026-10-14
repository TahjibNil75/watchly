"""The "forgot password" email: a temporary password, and what happens next.

Drawn and sent by `app/core/mail.py`; this module only says what it contains.
"""

from datetime import UTC

from app.core import mail
from app.core.config import settings
from app.db.models.user import User


def temporary_password_email(user: User, password: str) -> mail.LinkEmail:
    assert user.temp_password_expires_at
    expires = user.temp_password_expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    facts = [
        ("Username", user.username),
        ("Temporary password", password),
        ("Works until", expires),
    ]
    url = mail.app_url("/login")
    intro = (
        f"Someone asked to reset the password for your {settings.PROJECT_NAME} "
        "account. Sign in with this temporary password, then choose a new one."
    )
    note = (
        "You will be asked for a new password as soon as you sign in. If you did "
        "not ask for this, ignore this email: your current password still works, "
        "and this one expires on its own."
    )
    text = (
        f"{intro}\n\n"
        + "\n".join(f"{label}: {value}" for label, value in facts)
        + (f"\n\nSign in: {url}" if url else "")
        + f"\n\n{note}\n"
    )
    return mail.LinkEmail(
        to=user.email,
        subject=f"Your temporary {settings.PROJECT_NAME} password",
        kicker="Password reset",
        title="Your temporary password",
        intro=intro,
        facts=facts,
        button="Sign in",
        url=url,
        note=note,
        text=text,
    )


def temporary_password_reminder_email(user: User) -> mail.LinkEmail:
    """Sent when asked again while a temporary password is still live. Says that
    one is waiting but not what it is: only its hash is kept."""
    assert user.temp_password_expires_at
    expires = user.temp_password_expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    facts = [("Username", user.username), ("Works until", expires)]
    url = mail.app_url("/login")
    intro = (
        f"Someone asked again to reset the password for your {settings.PROJECT_NAME} "
        "account. A temporary password was already emailed to you and still works, "
        "so no new one was made. Look for the earlier email titled "
        f"\"Your temporary {settings.PROJECT_NAME} password\"."
    )
    note = (
        "If you did not ask for this, ignore this email: your current password "
        "still works. If you cannot find the earlier email, ask again after the "
        "time above and a new password will be sent."
    )
    text = (
        f"{intro}\n\n"
        + "\n".join(f"{label}: {value}" for label, value in facts)
        + (f"\n\nSign in: {url}" if url else "")
        + f"\n\n{note}\n"
    )
    return mail.LinkEmail(
        to=user.email,
        subject=f"Your {settings.PROJECT_NAME} temporary password is still waiting",
        kicker="Password reset",
        title="Your temporary password is still valid",
        intro=intro,
        facts=facts,
        button="Sign in",
        url=url,
        note=note,
        text=text,
    )


async def send_temporary_password(email: mail.LinkEmail, user_id: int) -> None:
    """Send it, without needing a link: the password is the point, and it works
    without one. Never raises."""
    await mail.send_link_email(email, f"temporary password for user {user_id}", needs_link=False)
