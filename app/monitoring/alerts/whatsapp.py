"""WhatsApp: drawing a `Message` for WhatsApp, and sending it through Meta's
WhatsApp Cloud API.

Like Slack and Telegram, each project carries its own sender — a Meta access
token and the phone number id of the business number that sends, set through
the API and the token stored encrypted — and the numbers to alert. A site may
alert numbers of its own instead, still sent from its project's number.

If a project has no WhatsApp of its own, the global WHATSAPP_ACCESS_TOKEN,
WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_RECIPIENTS are used as a firehose
fallback when all three are configured.

WhatsApp lets a business write freely to someone only within 24 hours of that
person's last message to it; outside that window only a pre-approved template
is delivered. A free-form message sent outside it is still accepted by the
API, then dropped with error 131047 — reported to a status webhook, which
Watchly does not have. So alerts go out as the template WHATSAPP_TEMPLATE_NAME,
created with `TEMPLATE_BODY` below. Blank sends free-form text instead, which
is only good for trying it out with numbers that have just written in.

Unlike Slack and Telegram there are no threads: each alert of an outage is a
message of its own, sent to each number in turn.
"""

import logging
import re

import httpx

from app.core.config import settings
from app.monitoring.alerts.base import (
    Alerter,
    Message,
    Notification,
    NotificationKind,
    SiteRow,
    Tone,
    WhatsAppTarget,
)

logger = logging.getLogger(__name__)

_EMOJI = {
    NotificationKind.DOWN: "🚨",
    NotificationKind.STILL_DOWN: "⚠️",
    NotificationKind.RECOVERED: "✅",
    NotificationKind.SSL_EXPIRING: "🔒",
    NotificationKind.DOMAIN_EXPIRING: "📅",
    NotificationKind.NAMESERVERS_CHANGED: "📡",
    NotificationKind.SLOW_RESPONSE: "⏳",
    NotificationKind.PACKET_LOSS: "📶",
    NotificationKind.DNS_CHANGED: "🌐",
    NotificationKind.MONTHLY_REPORT: "📊",
    NotificationKind.INFRA_DOWN: "🚨",
    NotificationKind.INFRA_STILL_DOWN: "⚠️",
    NotificationKind.INFRA_RECOVERED: "✅",
    NotificationKind.INFRA_DEGRADED: "🟡",
    NotificationKind.VPC_UNREACHABLE: "⛔",
    NotificationKind.VPC_RECOVERED: "🔗",
    NotificationKind.ASG_SCALED_OUT: "📈",
    NotificationKind.ASG_SCALED_IN: "📉",
    NotificationKind.DEPLOY_STARTED: "🚀",
    NotificationKind.DEPLOY_FINISHED: "🏁",
    NotificationKind.ACCOUNT_CAPACITY: "📏",
}

_TONE_DOT = {
    Tone.SUCCESS: "🟢",
    Tone.WARNING: "🟡",
    Tone.CRITICAL: "🔴",
    Tone.INFO: "🔵",
}

#: The template to create in WhatsApp Manager, word for word, in the category
#: Utility: `{{1}}` is the headline and `{{2}}` the details. Meta refuses a
#: template that starts or ends with a variable, or is mostly variables, hence
#: the fixed text around them.
TEMPLATE_BODY = (
    "Watchly monitoring update: {{1}}\n\n"
    "{{2}}\n\n"
    "You are receiving this because your number is on the WhatsApp alert list "
    "of a Watchly project."
)
#: A template message's body, variables filled in.
TEMPLATE_MAX_LENGTH = 1024
HEADLINE_MAX = 150
_VARIABLE = re.compile(r"\{\{([12])\}\}")

#: A free-form text message.
MAX_LENGTH = 4096
#: Fact rows drawn per message, as for Slack and Telegram.
MAX_FACTS = 20
#: A single fact's value, e.g. a long error text or header.
MAX_FACT_VALUE = 300
#: Websites listed in a report; the rest are counted.
MAX_SITES = 30
#: Websites named in a report's template details, which have far less room.
TEMPLATE_SITES = 5

#: Meta's error codes worth explaining rather than echoing raw.
_HINTS = {
    190: "the access token was rejected or has expired — use a system user's "
    "permanent token, not the 24-hour one from API setup",
    10: "the token lacks the whatsapp_business_messaging permission",
    200: "the token lacks the whatsapp_business_messaging permission",
    131026: "the number cannot receive it — it may not be on WhatsApp, or has not "
    "accepted WhatsApp's latest terms",
    131030: "the number is not on the test number's list of allowed recipients — "
    "add it in API setup, or send from a real business number",
    131031: "the WhatsApp Business account is locked",
    131042: "the WhatsApp Business account has a payment problem",
    131047: "more than 24 hours since this person last wrote to the business "
    "number — set WHATSAPP_TEMPLATE_NAME to an approved template",
    131048: "WhatsApp is limiting this business number for spam; the alert was dropped",
    131056: "too many messages to this number in a short time; the alert was dropped",
    130429: "WhatsApp is rate limiting this business number; the alert was dropped",
    132000: "the template's variables do not match — its text must have exactly "
    "{{1}} and {{2}}",
    132001: "no approved template by that name and language — create it in "
    "WhatsApp Manager (see WHATSAPP_TEMPLATE_NAME) and wait for its approval",
    132015: "the template is paused for low quality",
    132016: "the template was disabled for low quality",
    133010: "the business number is not registered with the Cloud API",
}
#: Refusals about the sender or the template, not the number: every other
#: number would be refused the same way.
_SENDER_ERRORS = frozenset({10, 190, 200, 131031, 131042, 132000, 132001, 132015, 132016, 133010})


def explain(error: dict) -> str:
    """Turn a refused Cloud API call into something actionable."""
    code = error.get("code")
    text = str(error.get("message") or "no description")
    details = (error.get("error_data") or {}).get("details")
    if details and details not in text:
        text = f"{text}: {details}"
    hint = _HINTS.get(code)
    if code == 100 and error.get("error_subcode") == 33:
        # "Object with ID … does not exist": the phone number id is wrong.
        hint = (
            "check whatsapp_phone_number_id: it is the Phone number ID from "
            "WhatsApp Manager, not the phone number"
        )
    return f"{text} ({code}) — {hint}" if hint else f"{text} ({code})"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…" if limit > 0 else ""


def _one_line(text: str) -> str:
    """A template variable may not contain a line break, a tab, or more than
    four spaces in a row."""
    return " ".join(text.split())


def _site_detail(site: SiteRow) -> str:
    detail = [
        site.uptime,
        f"{site.downtime} down" if site.downtime != "none" else "no downtime",
    ]
    if site.incidents:
        detail.append(f"{site.incidents} incident{'' if site.incidents == 1 else 's'}")
    if site.response != "—":
        detail.append(f"{site.response} avg")
    return " · ".join(detail)


# -- the template -----------------------------------------------------------


def template_parameters(message: Message) -> tuple[str, str]:
    """`{{1}}` and `{{2}}`: the headline, and everything else on one line,
    trimmed so the filled-in template fits WhatsApp's limit."""
    headline = _clip(_one_line(f"{_EMOJI[message.kind]} {message.title}"), HEADLINE_MAX)

    parts = [_one_line(message.body)]
    parts.append(" · ".join(f"{stat.label}: {stat.value}" for stat in message.stats))
    if message.sites:
        sites = [
            f"{_TONE_DOT[site.tone]} {site.name} {_site_detail(site)}"
            for site in message.sites[:TEMPLATE_SITES]
        ]
        omitted = message.sites_omitted + len(message.sites) - len(sites)
        if omitted:
            sites.append(f"and {omitted} more")
        parts.append(" · ".join(sites))
    parts.append(
        " · ".join(
            f"{label}: {_clip(value, MAX_FACT_VALUE) or '—'}"
            for label, value in message.facts[:MAX_FACTS]
        )
    )
    # The link goes last but is never trimmed: it is where the full story is.
    link = f" | {message.link[0]}: {message.link[1]}" if message.link else ""

    fixed = len(_VARIABLE.sub("", TEMPLATE_BODY))
    room = TEMPLATE_MAX_LENGTH - fixed - len(headline) - len(link)
    details = _clip(_one_line(" | ".join(part for part in parts if part)), room) + link
    # An empty variable is refused.
    return headline or "—", details.removeprefix(" | ") or "—"


def render_template(message: Message) -> str:
    """The template as the recipient reads it, variables filled in."""
    values = template_parameters(message)
    return _VARIABLE.sub(lambda m: values[int(m.group(1)) - 1], TEMPLATE_BODY)


# -- free-form text ---------------------------------------------------------


def _assemble(
    message: Message, body: str, sites: list[SiteRow], facts: list[tuple[str, str]]
) -> str:
    title = f"{_EMOJI[message.kind]} *{message.title}*"
    if message.kind is NotificationKind.MONTHLY_REPORT:
        title += f"\n_{message.kicker}_"
    blocks = [title]
    if body:
        blocks.append(body)
    if message.stats:
        blocks.append("\n".join(f"*{stat.label}:* {stat.value}" for stat in message.stats))
    if sites:
        lines = [
            f"{_TONE_DOT[site.tone]} *{_clip(site.name, 60)}* — {_site_detail(site)}"
            for site in sites
        ]
        omitted = message.sites_omitted + len(message.sites) - len(sites)
        if omitted:
            lines.append(f"_…and {omitted} more website(s), not shown._")
        blocks.append("\n".join(lines))
    if facts:
        blocks.append(
            "\n".join(
                f"*{label}:* {_clip(value, MAX_FACT_VALUE) or '—'}" for label, value in facts
            )
        )
    if message.note:
        blocks.append(f"_{message.note}_")
    if message.link:
        label, url = message.link
        blocks.append(f"{label}: {url}")
    return "\n\n".join(blocks)


def build_text(message: Message) -> str:
    """The free-form text, with WhatsApp's `*bold*` and `_italic_`, trimmed
    to fit in one message."""
    body = message.body.strip()
    sites = message.sites[:MAX_SITES]
    facts = message.facts[:MAX_FACTS]
    while True:
        text = _assemble(message, body, sites, facts)
        overflow = len(text) - MAX_LENGTH
        if overflow <= 0:
            return text
        # Least important first, as for Telegram: the trailing facts, then the
        # report's best-performing sites, then the body.
        if facts:
            facts = facts[:-1]
        elif len(sites) > 1:
            sites = sites[:-1]
        elif body:
            body = _clip(body, len(body) - overflow - 1)
        else:
            return text


# -- sending ----------------------------------------------------------------


def build_payload(message: Message) -> dict:
    """Everything the `messages` call needs for the message, minus the number."""
    if settings.WHATSAPP_TEMPLATE_NAME:
        return {
            "messaging_product": "whatsapp",
            "type": "template",
            "template": {
                "name": settings.WHATSAPP_TEMPLATE_NAME,
                "language": {"code": settings.WHATSAPP_TEMPLATE_LANGUAGE},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": value}
                            for value in template_parameters(message)
                        ],
                    }
                ],
            },
        }
    return {
        "messaging_product": "whatsapp",
        "type": "text",
        # The dashboard link would otherwise unfurl into a preview card.
        "text": {"body": build_text(message), "preview_url": False},
    }


def preview_text(message: Message) -> str:
    """What the recipient reads, in whichever form it would be sent."""
    if settings.WHATSAPP_TEMPLATE_NAME:
        return render_template(message)
    return build_text(message)


def _fallback() -> WhatsAppTarget | None:
    if not (
        settings.WHATSAPP_ACCESS_TOKEN
        and settings.WHATSAPP_PHONE_NUMBER_ID
        and settings.WHATSAPP_RECIPIENTS
    ):
        return None
    return WhatsAppTarget(
        access_token=settings.WHATSAPP_ACCESS_TOKEN,
        phone_number_id=settings.WHATSAPP_PHONE_NUMBER_ID,
        recipients=tuple(settings.WHATSAPP_RECIPIENTS),
    )


class WhatsAppAlerter(Alerter):
    name = "whatsapp"

    async def is_configured(self, event: Notification) -> bool:
        return bool(event.whatsapp) or _fallback() is not None

    async def send(self, event: Notification, message: Message) -> bool:
        target = event.whatsapp or _fallback()
        if target is None:
            return False

        url = f"{settings.WHATSAPP_API_URL.rstrip('/')}/{target.phone_number_id}/messages"
        payload = build_payload(message)
        delivered = 0
        async with httpx.AsyncClient(
            timeout=settings.WHATSAPP_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {target.access_token}"},
        ) as client:
            for number in target.recipients:
                error = await self._send_one(client, url, event, number, payload)
                if error is None:
                    delivered += 1
                elif error in _SENDER_ERRORS:
                    break

        if not delivered:
            return False
        logger.info(
            "Sent %s to %d of %d WhatsApp number(s)%s",
            event.describe(),
            delivered,
            len(target.recipients),
            "" if event.whatsapp else " of the fallback",
        )
        return True

    async def _send_one(
        self,
        client: httpx.AsyncClient,
        url: str,
        event: Notification,
        number: str,
        payload: dict,
    ) -> int | None:
        """Message one number: None once WhatsApp accepts it, otherwise Meta's
        error code (0 when there was no answer to read one from)."""
        try:
            response = await client.post(url, json={**payload, "to": number})
        except Exception as exc:
            logger.error(
                "WhatsApp request failed for %s to %s: %s: %s",
                event.describe(),
                number,
                type(exc).__name__,
                exc,
            )
            return 0
        try:
            body = response.json()
        except ValueError:
            logger.error(
                "WhatsApp answered %s to %s with HTTP %s and no JSON body",
                event.describe(),
                number,
                response.status_code,
            )
            return 0

        if response.is_success and body.get("messages"):
            return None
        error = body.get("error") or {}
        logger.error(
            "WhatsApp refused %s to %s: %s", event.describe(), number, explain(error)
        )
        return error.get("code") or 0
