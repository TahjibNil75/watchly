"""The invitation email: one card with the role, who invited, and the link.

Drawn and sent by `app/core/mail.py`; this module only says what it contains.
"""

from datetime import UTC
from email.message import EmailMessage

from app.core import mail
from app.core.config import settings
from app.db.models.invitation import Invitation


def invitation_url(token: str) -> str:
    """The link into the web UI, or "" when ALERT_DASHBOARD_URL is not set."""
    return mail.app_link("/accept-invite", token)


def _facts(invitation: Invitation) -> list[tuple[str, str]]:
    facts = [("Role", invitation.role.value)]
    if invitation.invited_by_name:
        facts.append(("Invited by", invitation.invited_by_name))
    expires = invitation.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    facts.append(("Link expires", expires))
    return facts


def render_text(invitation: Invitation, url: str) -> str:
    inviter = invitation.invited_by_name or "An administrator"
    facts = "\n".join(f"{label}: {value}" for label, value in _facts(invitation))
    return (
        f"{inviter} invited you to {settings.PROJECT_NAME}.\n\n"
        f"{facts}\n\n"
        f"Accept the invitation and choose your username and password:\n{url}\n\n"
        "The link works once. If you were not expecting this, ignore this email — "
        "no account is created until you accept.\n"
    )


def _email(invitation: Invitation, url: str) -> mail.LinkEmail:
    inviter = invitation.invited_by_name or "An administrator"
    return mail.LinkEmail(
        to=invitation.email,
        subject=f"You're invited to {settings.PROJECT_NAME}",
        kicker="Invitation",
        title=f"You're invited to {settings.PROJECT_NAME}",
        intro=f"{inviter} invited you to join. Accept to choose your username and password.",
        facts=_facts(invitation),
        button="Accept invitation",
        url=url,
        note=(
            "The link works once. If you were not expecting this, ignore this email — "
            "no account is created until you accept."
        ),
        text=render_text(invitation, url),
    )


def render_html(invitation: Invitation, url: str) -> str:
    return mail.render_html(_email(invitation, url))


def build_message(invitation: Invitation, url: str) -> EmailMessage:
    return mail.build_message(_email(invitation, url))


async def send_invitation(invitation: Invitation, token: str) -> bool:
    """Email the invitation link. Returns True once the mail server accepted it.

    Never raises on delivery trouble: the invitation is already saved, and the
    caller reports `email_sent=False` so the sender can try again.
    """
    return await mail.send_link_email(
        _email(invitation, invitation_url(token)), f"invitation {invitation.id}"
    )
