"""SMTP email alerts.

Provider-agnostic: point SMTP_HOST at AWS SES, Resend, Mailgun, Postmark,
Gmail or anything else that speaks SMTP. Port 587 with STARTTLS
(`SMTP_USE_TLS=true`) is the common case; port 465 wants `SMTP_USE_SSL=true`.
"""

import logging
from email.message import EmailMessage
from email.utils import formataddr

import aiosmtplib

from app.core.config import settings
from app.monitoring.alerts.base import Alerter, AlertEvent, AlertKind

logger = logging.getLogger(__name__)

#: Left border colour per alert kind, for the HTML body.
_ACCENT = {
    AlertKind.DOWN: "#dc2626",
    AlertKind.STILL_DOWN: "#d97706",
    AlertKind.RECOVERED: "#16a34a",
}


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html(event: AlertEvent) -> str:
    accent = _ACCENT[event.kind]
    rows = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;color:#6b7280;white-space:nowrap;'
        f'vertical-align:top">{_escape(label)}</td>'
        f'<td style="padding:6px 0;color:#111827;font-family:ui-monospace,SFMono-Regular,'
        f'Menlo,monospace;word-break:break-all">{_escape(value)}</td></tr>'
        for label, value in event.facts()
    )
    link = ""
    if settings.ALERT_DASHBOARD_URL:
        href = _escape(settings.ALERT_DASHBOARD_URL.rstrip("/"))
        link = (
            f'<p style="margin:20px 0 0"><a href="{href}" '
            f'style="color:#2563eb">Open the monitoring dashboard</a></p>'
        )
    return f"""\
<!doctype html><html><body style="margin:0;padding:24px;background:#f9fafb;
 font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
 <div style="max-width:640px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;
   border-radius:10px;border-left:5px solid {accent};padding:24px">
  <h1 style="margin:0 0 4px;font-size:17px;color:{accent}">{_escape(event.subject)}</h1>
  <p style="margin:0 0 18px;color:#6b7280;font-size:13px">
    {_escape(event.result.summary)}</p>
  <table style="border-collapse:collapse;font-size:13px;width:100%">{rows}</table>
  {link}
  <p style="margin:22px 0 0;padding-top:14px;border-top:1px solid #e5e7eb;
    color:#9ca3af;font-size:11px">Sent by Watchly uptime monitoring.</p>
 </div></body></html>"""


class EmailAlerter(Alerter):
    name = "email"

    async def is_configured(self, event: AlertEvent) -> bool:
        return bool(
            settings.ALERT_EMAIL_ENABLED
            and settings.SMTP_HOST
            and settings.SMTP_FROM_EMAIL
        )

    def build_message(self, event: AlertEvent, recipients: list[str]) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = event.subject
        message["From"] = formataddr(
            (settings.SMTP_FROM_NAME, settings.SMTP_FROM_EMAIL)
        )
        message["To"] = ", ".join(recipients)
        # Lets mail clients thread an outage and its recovery together.
        message["X-Watchly-Website-Id"] = str(event.website.id)
        message["X-Watchly-Alert-Kind"] = event.kind.value
        message.set_content(event.as_text())
        message.add_alternative(render_html(event), subtype="html")
        return message

    async def send(self, event: AlertEvent) -> bool:
        recipients = list(event.recipients)
        if not recipients:
            logger.warning(
                "No email recipients for %s; skipping alert.", event.website.url
            )
            return False
        if not await self.is_configured(event):
            logger.warning(
                "SMTP is not configured (set SMTP_HOST); alert for %s not sent.",
                event.website.url,
            )
            return False

        try:
            await aiosmtplib.send(
                self.build_message(event, recipients),
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME or None,
                password=settings.SMTP_PASSWORD or None,
                start_tls=settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL,
                use_tls=settings.SMTP_USE_SSL,
                timeout=settings.SMTP_TIMEOUT_SECONDS,
            )
        except Exception:
            # A broken mail server must never stop the monitoring loop.
            logger.exception(
                "Failed to email %s alert for %s", event.kind.value, event.website.url
            )
            return False

        logger.info(
            "Emailed %s alert for %s to %s",
            event.kind.value,
            event.website.url,
            ", ".join(recipients),
        )
        return True
