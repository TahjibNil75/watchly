"""The email confirming a new address: sent to that address, carrying the link
that makes the change.

Drawn and sent by `app/core/mail.py`; this module only says what it contains.
"""

from datetime import UTC

from app.core import mail
from app.core.config import settings
from app.db.models.user import User


def confirmation_url(token: str) -> str:
    """The link into the web UI, or "" when ALERT_DASHBOARD_URL is not set."""
    return mail.app_link("/confirm-email", token)


def _email(user: User, url: str) -> mail.LinkEmail:
    assert user.pending_email and user.email_token_expires_at
    expires = user.email_token_expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    facts = [
        ("Account", user.username),
        ("New address", user.pending_email),
        ("Link expires", expires),
    ]
    note = (
        "Until you confirm, the account keeps its current address. If this wasn't "
        "you, ignore this email and nothing changes."
    )
    text = (
        f"{user.username} asked to use this address for their {settings.PROJECT_NAME} "
        "account.\n\n"
        + "\n".join(f"{label}: {value}" for label, value in facts)
        + f"\n\nConfirm the change:\n{url}\n\n{note}\n"
    )
    return mail.LinkEmail(
        to=user.pending_email,
        subject=f"Confirm your new email address for {settings.PROJECT_NAME}",
        kicker="Email change",
        title="Confirm your new email address",
        intro=(
            f"{user.username} asked to use this address for their "
            f"{settings.PROJECT_NAME} account. Confirm to make the change."
        ),
        facts=facts,
        button="Confirm new address",
        url=url,
        note=note,
        text=text,
    )


async def send_email_confirmation(user: User, token: str) -> bool:
    """Email the confirmation link to the pending address. Returns True once the
    mail server accepted it; never raises on delivery trouble."""
    return await mail.send_link_email(
        _email(user, confirmation_url(token)), f"email confirmation for user {user.id}"
    )
