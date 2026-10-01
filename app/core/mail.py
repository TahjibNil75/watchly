"""Account emails: invitations, address confirmations, temporary passwords.

Sent over the same SMTP settings as alerts (see `app/monitoring/alerts/email.py`),
but they are not alerts: they ignore the alert on/off switches, and links are
built from `ALERT_DASHBOARD_URL` because the recipient has to be able to open them.

Each carries a secret — a link's token, or a temporary password — that proves
the recipient owns the address. It appears in the message and nowhere else: not
in logs, not in any API response.
"""

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from html import escape

import aiosmtplib

from app.core.config import settings

logger = logging.getLogger(__name__)

_FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_ACCENT = "#4f46e5"


def app_url(path: str) -> str:
    """A link into the web UI, or "" when ALERT_DASHBOARD_URL is not set."""
    base = settings.ALERT_DASHBOARD_URL.strip().rstrip("/")
    return f"{base}{path}" if base else ""


def app_link(path: str, token: str) -> str:
    """A link into the web UI carrying `token`, or "" when ALERT_DASHBOARD_URL
    is not set.

    The token goes in the fragment, not the query string: browsers never send a
    fragment to the server, so it stays out of access logs and `Referer`
    headers. The page reads it and clears it from the address bar."""
    url = app_url(path)
    return f"{url}#token={token}" if url else ""


@dataclass(frozen=True, slots=True)
class LinkEmail:
    """One card: a heading, a paragraph, some facts, a button, a closing note.
    The button is left out when there is no `url`."""

    to: str
    subject: str
    #: Small label above the title, e.g. "Invitation".
    kicker: str
    title: str
    intro: str
    facts: list[tuple[str, str]]
    button: str
    url: str
    note: str
    #: The whole plain-text version, written by the caller.
    text: str


def _button(email: LinkEmail) -> str:
    if not email.url:
        return ""
    return (
        f'<a href="{escape(email.url)}" style="display:inline-block;padding:11px 20px;'
        f"background:{_ACCENT};\n    color:#ffffff;text-decoration:none;font-size:14px;"
        f'font-weight:600;border-radius:8px">\n    {escape(email.button)}</a>'
    )


def render_html(email: LinkEmail) -> str:
    rows = "".join(
        f'<tr><td style="padding:7px 16px 7px 0;color:#6b7280;font-size:13px;'
        f'white-space:nowrap;border-top:1px solid #f3f4f6">{escape(label)}</td>'
        f'<td style="padding:7px 0;color:#111827;font-size:13px;'
        f'border-top:1px solid #f3f4f6">{escape(value)}</td></tr>'
        for label, value in email.facts
    )
    return f"""\
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(email.title)}</title></head>
<body style="margin:0;padding:24px 12px;background:#f3f4f6;font-family:{_FONT}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center">
 <table role="presentation" width="600" cellspacing="0" cellpadding="0"
  style="width:100%;max-width:600px;background:#ffffff;border:1px solid #e5e7eb;
  border-radius:12px;overflow:hidden">
  <tr><td style="background:{_ACCENT};padding:22px 28px">
   <div style="font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
    color:#ffffff;opacity:.85">{escape(email.kicker)}</div>
   <div style="margin-top:6px;font-size:22px;line-height:1.25;font-weight:700;color:#ffffff">
    {escape(email.title)}</div>
  </td></tr>
  <tr><td style="padding:26px 28px 8px">
   <p style="margin:0 0 20px;font-size:15px;line-height:1.6;color:#111827">
    {escape(email.intro)}</p>
   <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
    style="border-collapse:collapse;margin:0 0 22px">{rows}</table>
   {_button(email)}
   <p style="margin:22px 0 0;font-size:13px;line-height:1.55;color:#374151">
    {escape(email.note)}</p>
  </td></tr>
  <tr><td style="padding:14px 28px 20px;font-size:11px;color:#9ca3af">
   Sent by {escape(settings.PROJECT_NAME)}.</td></tr>
 </table>
</td></tr></table>
</body></html>"""


def build_message(email: LinkEmail) -> EmailMessage:
    mail = EmailMessage()
    mail["Subject"] = email.subject
    mail["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM_EMAIL))
    mail["To"] = email.to
    mail.set_content(email.text)
    mail.add_alternative(render_html(email), subtype="html")
    return mail


async def send_link_email(email: LinkEmail, what: str, *, needs_link: bool = True) -> bool:
    """Send `email`. Returns True once the mail server accepted it.

    `what` names it in log lines, e.g. "invitation 12". With `needs_link`, an
    email whose link could not be built (ALERT_DASHBOARD_URL unset) is not sent
    at all. Never raises on delivery trouble: the caller has already saved
    whatever the email is about.
    """
    if needs_link and not email.url:
        logger.warning("ALERT_DASHBOARD_URL is not set; %s has no link to send.", what)
        return False
    if not (settings.SMTP_HOST and settings.SMTP_FROM_EMAIL):
        logger.warning("SMTP is not configured (set SMTP_HOST); %s not sent.", what)
        return False

    try:
        await aiosmtplib.send(
            build_message(email),
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USERNAME or None,
            password=settings.SMTP_PASSWORD or None,
            start_tls=settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL,
            use_tls=settings.SMTP_USE_SSL,
            timeout=settings.SMTP_TIMEOUT_SECONDS,
        )
    except Exception:
        # Deliberately not logging the message: its link or password is a secret.
        logger.exception("Failed to email %s", what)
        return False

    logger.info("Emailed %s to %s", what, email.to)
    return True
