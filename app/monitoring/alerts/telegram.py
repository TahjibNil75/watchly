"""Telegram: drawing a `Message` as Telegram HTML, and sending it.

Like Slack, each project carries its own bot token and chat id (set through the
API and stored encrypted), so every client can have its own group or channel. A
site may override the chat while reusing the project's bot, or bring its own.

If a project has no Telegram of its own, the global TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID are used as a firehose fallback when both are configured.

An outage stays together as a reply chain: the down alert is sent to the chat,
and the follow-ups and the recovery reply to it (`TelegramTarget.reply_to`).

The token is part of every Bot API URL, so nothing here logs a URL or an
exception that may carry one without redacting it first.

Telegram's limit, which the builder below stays inside: a message is at most
4096 characters after its HTML is parsed. The builder measures the HTML itself,
which is always the longer of the two.
"""

import html
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
)

logger = logging.getLogger(__name__)


class _RedactBotToken(logging.Filter):
    """httpx logs every request's URL at INFO, and a Bot API URL carries the
    token: mask it there too, should anyone turn that logging on."""

    _PATH = re.compile(r"/bot\d+:[A-Za-z0-9_-]+/")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/bot" in message:
            record.msg, record.args = self._PATH.sub("/bot<token>/", message), ()
        return True


logging.getLogger("httpx").addFilter(_RedactBotToken())

_EMOJI = {
    NotificationKind.DOWN: "🚨",
    NotificationKind.STILL_DOWN: "⚠️",
    NotificationKind.RECOVERED: "✅",
    NotificationKind.SSL_EXPIRING: "🔒",
    NotificationKind.SLOW_RESPONSE: "⏳",
    NotificationKind.PACKET_LOSS: "📶",
    NotificationKind.DNS_CHANGED: "🌐",
    NotificationKind.MONTHLY_REPORT: "📊",
}

_TONE_DOT = {
    Tone.SUCCESS: "🟢",
    Tone.WARNING: "🟡",
    Tone.CRITICAL: "🔴",
    Tone.INFO: "🔵",
}

MAX_LENGTH = 4096
#: Fact rows drawn per message, as for Slack; the HTTP headers that trail an
#: outage's facts are the first thing to go.
MAX_FACTS = 20
#: A single fact's value, e.g. a long error text or header.
MAX_FACT_VALUE = 300
#: Websites listed in a report; the rest are counted.
MAX_SITES = 30

#: Telegram's error descriptions worth explaining rather than echoing raw,
#: matched on a fragment of the lower-cased description.
_HINTS = (
    ("unauthorized", "the bot token was rejected — copy it again from @BotFather"),
    (
        "chat not found",
        "no such chat, or the bot is not in it — add the bot to the group or "
        "channel, or have the person send it /start first",
    ),
    ("bot was blocked by the user", "the person blocked the bot; they have to unblock it"),
    ("bot was kicked", "the bot was removed from that chat; add it back"),
    (
        "bot is not a member",
        "the bot is not in that channel — add it as an administrator that can post messages",
    ),
    (
        "not enough rights",
        "the bot may not post there — in a channel, make it an administrator "
        "that can post messages",
    ),
    (
        "administrator rights",
        "the bot may not post there — in a channel, make it an administrator "
        "that can post messages",
    ),
    (
        "can't initiate conversation",
        "the person has to open the bot and press Start before it can message them",
    ),
    ("too many requests", "Telegram is rate limiting this bot; the alert was dropped"),
    (
        "can't parse entities",
        "Telegram rejected the message formatting — this is a Watchly bug, please report it",
    ),
)


def explain(body: dict) -> str:
    """Turn a refused Bot API call into something actionable."""
    description = str(body.get("description") or f"error {body.get('error_code', 'unknown')}")
    migrated = (body.get("parameters") or {}).get("migrate_to_chat_id")
    if migrated:
        return (
            f"{description} — the group became a supergroup; change the chat id to {migrated}"
        )
    lowered = description.lower()
    hint = next((hint for fragment, hint in _HINTS if fragment in lowered), None)
    return f"{description} — {hint}" if hint else description


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


def _length(text: str) -> int:
    """Length as Telegram counts it, in UTF-16 code units: an emoji is two."""
    return len(text.encode("utf-16-le")) // 2


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…" if limit > 0 else ""


def _escape(text: str) -> str:
    """Telegram's HTML needs `&`, `<` and `>` escaped outside of tags."""
    return html.escape(text, quote=False)


def _site_line(site: SiteRow) -> str:
    detail = [
        site.uptime,
        f"{site.downtime} down" if site.downtime != "none" else "no downtime",
    ]
    if site.incidents:
        detail.append(f"{site.incidents} incident{'' if site.incidents == 1 else 's'}")
    if site.response != "—":
        detail.append(f"{site.response} avg")
    name = _escape(_clip(site.name, 60))
    return f"{_TONE_DOT[site.tone]} <b>{name}</b> — {_escape(' · '.join(detail))}"


def _fact_line(label: str, value: str) -> str:
    return f"<b>{_escape(label)}:</b> {_escape(_clip(value, MAX_FACT_VALUE)) or '—'}"


def _assemble(
    message: Message, body: str, sites: list[SiteRow], facts: list[tuple[str, str]]
) -> str:
    title = f"{_EMOJI[message.kind]} <b>{_escape(message.title)}</b>"
    if message.kind is NotificationKind.MONTHLY_REPORT:
        title += f"\n<i>{_escape(message.kicker)}</i>"
    blocks = [title]
    if body:
        blocks.append(_escape(body))
    if message.stats:
        blocks.append(
            "\n".join(
                f"<b>{_escape(stat.label)}:</b> {_escape(stat.value)}" for stat in message.stats
            )
        )
    if sites:
        lines = [_site_line(site) for site in sites]
        omitted = message.sites_omitted + len(message.sites) - len(sites)
        if omitted:
            lines.append(f"<i>…and {omitted} more website(s), not shown.</i>")
        blocks.append("\n".join(lines))
    if facts:
        blocks.append("\n".join(_fact_line(label, value) for label, value in facts))
    if message.note:
        blocks.append(f"<i>{_escape(message.note)}</i>")
    if message.link:
        label, url = message.link
        blocks.append(f'<a href="{html.escape(url)}">{_escape(label)}</a>')
    return "\n\n".join(blocks)


def build_text(message: Message) -> str:
    """The HTML text for any message, trimmed to fit in one Telegram message."""
    body = message.body.strip()
    sites = message.sites[:MAX_SITES]
    facts = message.facts[:MAX_FACTS]
    while True:
        text = _assemble(message, body, sites, facts)
        overflow = _length(text) - MAX_LENGTH
        if overflow <= 0:
            return text
        # Least important first: the trailing facts, then the report's
        # best-performing sites (they are ranked worst first), then the body.
        if facts:
            facts = facts[:-1]
        elif len(sites) > 1:
            sites = sites[:-1]
        elif body:
            body = _clip(body, len(body) - overflow - 1)
        else:
            return text


def build_payload(message: Message) -> dict:
    """Everything `sendMessage` needs for the message, minus the chat."""
    return {
        "text": build_text(message),
        "parse_mode": "HTML",
        # The dashboard link would otherwise unfurl into a large preview card.
        "link_preview_options": {"is_disabled": True},
    }


_LINK = re.compile(r'<a href="([^"]*)">(.*?)</a>')
_TAG = re.compile(r"</?[a-z]+>")


def plain_text(text: str) -> str:
    """`build_text`'s HTML as plain text, links spelled out."""
    text = _LINK.sub(lambda m: f"{m.group(2)}: {m.group(1)}", text)
    return html.unescape(_TAG.sub("", text))


def _is_formatting_error(body: dict) -> bool:
    description = str(body.get("description", "")).lower()
    return body.get("error_code") == 400 and "entities" in description


def _has_fallback() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)


class TelegramAlerter(Alerter):
    name = "telegram"

    async def is_configured(self, event: Notification) -> bool:
        return bool(event.telegram) or _has_fallback()

    async def send(self, event: Notification, message: Message) -> bool:
        if event.telegram is not None:
            target = event.telegram
            sent = await self._send_message(
                event, target.bot_token, target.chat_id, message, reply_to=target.reply_to
            )
            if sent is None:
                return False
            event.telegram_message_id = sent.get("message_id")
            logger.info("Sent %s to Telegram chat %s", event.describe(), target.chat_id)
            return True
        if _has_fallback():
            sent = await self._send_message(
                event, settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID, message
            )
            if sent is None:
                return False
            logger.info("Sent %s to the fallback Telegram chat", event.describe())
            return True
        return False

    async def _send_message(
        self,
        event: Notification,
        token: str,
        chat_id: str,
        message: Message,
        *,
        reply_to: int | None = None,
    ) -> dict | None:
        """Send one message: Telegram's copy of it, or None if it did not go out."""
        payload = {"chat_id": chat_id, **build_payload(message)}
        if reply_to is not None:
            # If the message being replied to was deleted, send it anyway.
            payload["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        body = await self._call(event, token, payload)
        if body is not None and not body.get("ok") and _is_formatting_error(body):
            # An alert without formatting beats no alert.
            logger.warning(
                "Telegram refused the formatting of %s (%s); sending it as plain text",
                event.describe(),
                _redact(explain(body), token),
            )
            payload.pop("parse_mode")
            payload["text"] = plain_text(payload["text"])
            body = await self._call(event, token, payload)
        if body is None:
            return None

        if not body.get("ok"):
            logger.error(
                "Telegram refused %s in chat %s: %s",
                event.describe(),
                chat_id,
                _redact(explain(body), token),
            )
            return None
        return body.get("result") or {}

    async def _call(self, event: Notification, token: str, payload: dict) -> dict | None:
        """POST `sendMessage`: Telegram's answer, or None if there was none."""
        url = f"{settings.TELEGRAM_API_URL.rstrip('/')}/bot{token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=settings.TELEGRAM_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=payload)
        except Exception as exc:
            # Not logger.exception: the message or traceback may carry the URL.
            logger.error(
                "Telegram request failed for %s: %s",
                event.describe(),
                _redact(f"{type(exc).__name__}: {exc}", token),
            )
            return None
        try:
            # Telegram explains a refusal in the body, whatever the status.
            return response.json()
        except ValueError:
            logger.error(
                "Telegram answered %s with HTTP %s and no JSON body",
                event.describe(),
                response.status_code,
            )
            return None
