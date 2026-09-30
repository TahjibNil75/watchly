"""SMTP email: drawing a `Message` as HTML and plain text, and sending it.

Provider-agnostic: point SMTP_HOST at AWS SES, Resend, Mailgun, Postmark,
Gmail or anything else that speaks SMTP. Port 587 with STARTTLS
(`SMTP_USE_TLS=true`) is the common case; port 465 wants `SMTP_USE_SSL=true`.

The HTML is table-based with inline styles, because mail clients ignore
`<style>` blocks and most layout CSS. Two layouts: a card for one site's alert,
and a report with headline tiles and a per-site table. Both open with the
Watchly logo, attached to the message itself (`cid:`), so it shows without the
client fetching anything or the dashboard being reachable.
"""

import base64
import logging
from email.message import EmailMessage
from email.utils import formataddr

import aiosmtplib

from app.core.config import settings
from app.monitoring.alerts.base import (
    Alerter,
    Message,
    Notification,
    NotificationKind,
    SiteRow,
    Stat,
    Tone,
    logo_png,
)
from app.monitoring.alerts.events import SiteEvent

logger = logging.getLogger(__name__)

#: (strong colour, pale tint) per tone.
_PALETTE = {
    Tone.CRITICAL: ("#dc2626", "#fef2f2"),
    Tone.WARNING: ("#d97706", "#fffbeb"),
    Tone.SUCCESS: ("#16a34a", "#f0fdf4"),
    Tone.INFO: ("#4f46e5", "#eef2ff"),
}

#: How the HTML refers to the logo attached to the message.
LOGO_CID = "watchly-logo"
_BRAND = "#1c8f4e"

_FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _paragraphs(text: str) -> str:
    """Escaped, with line breaks kept. Templates are plain text."""
    return _escape(text.strip()).replace("\r\n", "\n").replace("\n", "<br>")


def logo_data_url() -> str:
    """The logo inline, for showing the HTML outside a mail client."""
    return "data:image/png;base64," + base64.b64encode(logo_png()).decode("ascii")


def _brand(logo_src: str) -> str:
    """The logo and the name, above the coloured header."""
    return (
        f'<tr><td style="padding:16px 28px;border-bottom:1px solid #f3f4f6">'
        f'<table role="presentation" cellspacing="0" cellpadding="0"><tr>'
        f'<td style="vertical-align:middle"><img src="{_escape(logo_src)}" width="32" '
        f'height="32" alt="Watchly" style="display:block;border:0;outline:none"></td>'
        f'<td style="vertical-align:middle;padding-left:10px;font-size:18px;'
        f'font-weight:800;color:{_BRAND};letter-spacing:.01em">Watchly</td>'
        f"</tr></table></td></tr>"
    )


def _header(message: Message) -> str:
    strong, _ = _PALETTE[message.tone]
    return (
        f'<tr><td style="background:{strong};padding:22px 28px">'
        f'<div style="font-size:11px;font-weight:700;letter-spacing:.08em;'
        f'text-transform:uppercase;color:#ffffff;opacity:.85">{_escape(message.kicker)}</div>'
        f'<div style="margin-top:6px;font-size:22px;line-height:1.25;font-weight:700;'
        f'color:#ffffff">{_escape(message.title)}</div></td></tr>'
    )


def _body_text(message: Message) -> str:
    if not message.body.strip():
        return ""
    return (
        f'<p style="margin:0 0 20px;font-size:15px;line-height:1.6;color:#111827">'
        f"{_paragraphs(message.body)}</p>"
    )


def _facts_table(facts: list[tuple[str, str]]) -> str:
    if not facts:
        return ""
    rows = "".join(
        f'<tr><td style="padding:7px 16px 7px 0;color:#6b7280;font-size:13px;'
        f'white-space:nowrap;vertical-align:top;border-top:1px solid #f3f4f6">'
        f"{_escape(label)}</td>"
        f'<td style="padding:7px 0;color:#111827;font-size:13px;font-family:{_MONO};'
        f'word-break:break-all;border-top:1px solid #f3f4f6">{_escape(value)}</td></tr>'
        for label, value in facts
    )
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse;margin:0 0 20px">{rows}</table>'
    )


def _note(message: Message) -> str:
    if not message.note:
        return ""
    strong, tint = _PALETTE[message.tone]
    return (
        f'<div style="margin:0 0 22px;padding:12px 14px;background:{tint};'
        f'border-left:3px solid {strong};border-radius:4px;font-size:13px;'
        f'line-height:1.55;color:#374151">{_escape(message.note)}</div>'
    )


def _button(message: Message) -> str:
    if message.link is None:
        return ""
    label, url = message.link
    strong, _ = _PALETTE[message.tone]
    return (
        f'<a href="{_escape(url)}" style="display:inline-block;padding:11px 20px;'
        f"background:{strong};color:#ffffff;text-decoration:none;font-size:14px;"
        f'font-weight:600;border-radius:8px">{_escape(label)}</a>'
    )


def _tiles(stats: list[Stat]) -> str:
    if not stats:
        return ""
    width = 100 // len(stats)
    cells = ""
    last = len(stats) - 1
    for index, stat in enumerate(stats):
        colour = _PALETTE[stat.tone][0] if stat.tone else "#111827"
        # Gutters between tiles only: no negative margins or calc(), which mail
        # clients (Outlook above all) ignore.
        gutter = f"padding:0 {0 if index == last else 4}px 0 {0 if index == 0 else 4}px"
        cells += (
            f'<td width="{width}%" style="{gutter}" valign="top">'
            f'<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;'
            f'padding:14px 12px;text-align:center">'
            f'<div style="font-size:22px;font-weight:700;color:{colour};'
            f'line-height:1.2">{_escape(stat.value)}</div>'
            f'<div style="margin-top:4px;font-size:11px;letter-spacing:.05em;'
            f'text-transform:uppercase;color:#6b7280">{_escape(stat.label)}</div>'
            f"</div></td>"
        )
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="margin:0 0 22px"><tr>{cells}</tr></table>'
    )


def _site_table(sites: list[SiteRow], omitted: int) -> str:
    if not sites:
        return ""
    th = (
        "padding:8px 8px;font-size:11px;letter-spacing:.05em;text-transform:uppercase;"
        "color:#6b7280;text-align:{align};border-bottom:2px solid #e5e7eb;font-weight:600"
    )
    head = (
        f'<tr><th style="{th.format(align="left")}">Website</th>'
        f'<th style="{th.format(align="left")}" width="112">Uptime</th>'
        f'<th style="{th.format(align="right")}" width="70">Downtime</th>'
        f'<th style="{th.format(align="right")}" width="62">Incidents</th>'
        f'<th style="{th.format(align="right")}" width="70">Avg resp.</th></tr>'
    )
    body = ""
    for site in sites:
        colour = _PALETTE[site.tone][0]
        bar = min(max(site.uptime_percent or 0.0, 0.0), 100.0)
        td = "padding:10px 8px;border-bottom:1px solid #f3f4f6;font-size:13px;color:#111827"
        body += (
            f"<tr>"
            f'<td style="{td}"><div style="font-weight:600">{_escape(site.name)}</div>'
            f'<div style="font-size:11px;color:#9ca3af;word-break:break-all">'
            f"{_escape(site.url)}</div></td>"
            f'<td style="{td}"><div style="font-weight:700;color:{colour}">'
            f"{_escape(site.uptime)}</div>"
            f'<div style="height:4px;background:#e5e7eb;border-radius:2px;margin-top:5px">'
            f'<div style="height:4px;width:{bar:.1f}%;background:{colour};'
            f'border-radius:2px"></div></div></td>'
            f'<td style="{td};text-align:right;white-space:nowrap">{_escape(site.downtime)}</td>'
            f'<td style="{td};text-align:right">{site.incidents}</td>'
            f'<td style="{td};text-align:right;white-space:nowrap">{_escape(site.response)}</td>'
            f"</tr>"
        )
    more = (
        f'<p style="margin:10px 0 0;font-size:12px;color:#6b7280">…and {omitted} more '
        f"website(s), not shown.</p>"
        if omitted
        else ""
    )
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse;margin:0 0 20px">{head}{body}</table>{more}'
    )


def render_html(message: Message, logo_src: str = f"cid:{LOGO_CID}") -> str:
    """`logo_src` is the attached logo; a preview outside a mail client can
    pass a data: URL instead."""
    is_report = message.kind is NotificationKind.MONTHLY_REPORT
    inner = (
        _body_text(message)
        + (_tiles(message.stats) if is_report else "")
        + (_site_table(message.sites, message.sites_omitted) if is_report else "")
        + _facts_table(message.facts)
        + _note(message)
        + _button(message)
    )
    return f"""\
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(message.subject)}</title></head>
<body style="margin:0;padding:24px 12px;background:#f3f4f6;font-family:{_FONT}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center">
 <table role="presentation" width="600" cellspacing="0" cellpadding="0"
  style="width:100%;max-width:600px;background:#ffffff;border:1px solid #e5e7eb;
  border-radius:12px;overflow:hidden">
  {_brand(logo_src)}
  {_header(message)}
  <tr><td style="padding:26px 28px 8px">{inner}</td></tr>
  <tr><td style="padding:14px 28px 20px;border-top:1px solid #f3f4f6;font-size:11px;
   color:#9ca3af">Sent by Watchly uptime monitoring.</td></tr>
 </table>
</td></tr></table>
</body></html>"""


def render_text(message: Message) -> str:
    """The plain-text alternative; also what a text-only client shows."""
    parts = [message.title, message.kicker.upper(), ""]
    if message.body.strip():
        parts += [message.body.strip(), ""]
    if message.stats:
        parts += [f"{stat.label}: {stat.value}" for stat in message.stats] + [""]
    if message.sites:
        name_width = max(len(site.name) for site in message.sites)
        parts.append(
            f"{'Website'.ljust(name_width)}  {'Uptime':>8}  {'Downtime':>9}  "
            f"{'Incidents':>9}  {'Avg resp.':>9}"
        )
        for site in message.sites:
            parts.append(
                f"{site.name.ljust(name_width)}  {site.uptime:>8}  {site.downtime:>9}  "
                f"{site.incidents:>9}  {site.response:>9}"
            )
        if message.sites_omitted:
            parts.append(f"...and {message.sites_omitted} more website(s), not shown.")
        parts.append("")
    if message.facts:
        width = max(len(label) for label, _ in message.facts)
        parts += [f"{label.ljust(width)}  {value}" for label, value in message.facts]
        parts.append("")
    if message.note:
        parts += [message.note, ""]
    if message.link:
        parts.append(f"{message.link[0]}: {message.link[1]}")
    return "\n".join(parts).rstrip() + "\n"


class EmailAlerter(Alerter):
    name = "email"

    async def is_configured(self, event: Notification) -> bool:
        return bool(
            settings.ALERT_EMAIL_ENABLED
            and settings.SMTP_HOST
            and settings.SMTP_FROM_EMAIL
        )

    def build_message(
        self, event: Notification, message: Message, recipients: list[str]
    ) -> EmailMessage:
        mail = EmailMessage()
        mail["Subject"] = message.subject
        mail["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM_EMAIL))
        mail["To"] = ", ".join(recipients)
        if isinstance(event, SiteEvent):
            mail["X-Watchly-Website-Id"] = str(event.website.id)
        mail["X-Watchly-Project-Id"] = str(event.project_id)
        mail["X-Watchly-Alert-Kind"] = event.kind.value
        mail.set_content(render_text(message))
        mail.add_alternative(render_html(message), subtype="html")
        # multipart/related around the HTML, so clients show the logo inline
        # rather than as an attachment.
        html = mail.get_payload()[1]
        html.add_related(
            logo_png(),
            maintype="image",
            subtype="png",
            cid=f"<{LOGO_CID}>",
            filename="watchly-logo.png",
            disposition="inline",
        )
        return mail

    async def send(self, event: Notification, message: Message) -> bool:
        recipients = list(event.recipients)
        if not recipients:
            logger.warning("No email recipients for %s; skipping.", event.describe())
            return False
        if not await self.is_configured(event):
            logger.warning(
                "SMTP is not configured (set SMTP_HOST); %s not sent.", event.describe()
            )
            return False

        try:
            await aiosmtplib.send(
                self.build_message(event, message, recipients),
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
            logger.exception("Failed to email %s", event.describe())
            return False

        logger.info("Emailed %s to %s", event.describe(), ", ".join(recipients))
        return True
