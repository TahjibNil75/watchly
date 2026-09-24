"""The invitation email: one card with the role, who invited, and the link.

Sent over the same SMTP settings as alerts (see `app/monitoring/alerts/email.py`),
but it is not an alert: it ignores the alert on/off switches, and the link is
built from `ALERT_DASHBOARD_URL` because the invitee has to be able to open it.

The link carries the token, which is the invitee's only proof of owning the
address. It appears in the message and nowhere else — not in logs, not in any
API response.
"""

import logging
from datetime import UTC
from email.message import EmailMessage
from email.utils import formataddr
from html import escape

import aiosmtplib

from app.core.config import settings
from app.db.models.invitation import Invitation

logger = logging.getLogger(__name__)

_FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_ACCENT = "#4f46e5"


def invitation_url(token: str) -> str:
    """The link into the web UI, or "" when ALERT_DASHBOARD_URL is not set."""
    base = settings.ALERT_DASHBOARD_URL.strip().rstrip("/")
    return f"{base}/accept-invite?token={token}" if base else ""


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


def render_html(invitation: Invitation, url: str) -> str:
    inviter = invitation.invited_by_name or "An administrator"
    rows = "".join(
        f'<tr><td style="padding:7px 16px 7px 0;color:#6b7280;font-size:13px;'
        f'white-space:nowrap;border-top:1px solid #f3f4f6">{escape(label)}</td>'
        f'<td style="padding:7px 0;color:#111827;font-size:13px;'
        f'border-top:1px solid #f3f4f6">{escape(value)}</td></tr>'
        for label, value in _facts(invitation)
    )
    return f"""\
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>You're invited to {escape(settings.PROJECT_NAME)}</title></head>
<body style="margin:0;padding:24px 12px;background:#f3f4f6;font-family:{_FONT}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center">
 <table role="presentation" width="600" cellspacing="0" cellpadding="0"
  style="width:100%;max-width:600px;background:#ffffff;border:1px solid #e5e7eb;
  border-radius:12px;overflow:hidden">
  <tr><td style="background:{_ACCENT};padding:22px 28px">
   <div style="font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
    color:#ffffff;opacity:.85">Invitation</div>
   <div style="margin-top:6px;font-size:22px;line-height:1.25;font-weight:700;color:#ffffff">
    You're invited to {escape(settings.PROJECT_NAME)}</div>
  </td></tr>
  <tr><td style="padding:26px 28px 8px">
   <p style="margin:0 0 20px;font-size:15px;line-height:1.6;color:#111827">
    {escape(inviter)} invited you to join. Accept to choose your username and password.</p>
   <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
    style="border-collapse:collapse;margin:0 0 22px">{rows}</table>
   <a href="{escape(url)}" style="display:inline-block;padding:11px 20px;background:{_ACCENT};
    color:#ffffff;text-decoration:none;font-size:14px;font-weight:600;border-radius:8px">
    Accept invitation</a>
   <p style="margin:22px 0 0;font-size:13px;line-height:1.55;color:#374151">
    The link works once. If you were not expecting this, ignore this email — no account is
    created until you accept.</p>
  </td></tr>
  <tr><td style="padding:14px 28px 20px;font-size:11px;color:#9ca3af">
   Sent by {escape(settings.PROJECT_NAME)}.</td></tr>
 </table>
</td></tr></table>
</body></html>"""


def build_message(invitation: Invitation, url: str) -> EmailMessage:
    mail = EmailMessage()
    mail["Subject"] = f"You're invited to {settings.PROJECT_NAME}"
    mail["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM_EMAIL))
    mail["To"] = invitation.email
    mail.set_content(render_text(invitation, url))
    mail.add_alternative(render_html(invitation, url), subtype="html")
    return mail


async def send_invitation(invitation: Invitation, token: str) -> bool:
    """Email the invitation link. Returns True once the mail server accepted it.

    Never raises on delivery trouble: the invitation is already saved, and the
    caller reports `email_sent=False` so the sender can try again.
    """
    url = invitation_url(token)
    if not url:
        logger.warning(
            "ALERT_DASHBOARD_URL is not set; invitation %s has no link to send.",
            invitation.id,
        )
        return False
    if not (settings.SMTP_HOST and settings.SMTP_FROM_EMAIL):
        logger.warning(
            "SMTP is not configured (set SMTP_HOST); invitation %s not sent.",
            invitation.id,
        )
        return False

    try:
        await aiosmtplib.send(
            build_message(invitation, url),
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USERNAME or None,
            password=settings.SMTP_PASSWORD or None,
            start_tls=settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL,
            use_tls=settings.SMTP_USE_SSL,
            timeout=settings.SMTP_TIMEOUT_SECONDS,
        )
    except Exception:
        # Deliberately not logging `url`: it holds the token.
        logger.exception("Failed to email invitation %s", invitation.id)
        return False

    logger.info("Emailed invitation %s to %s", invitation.id, invitation.email)
    return True
