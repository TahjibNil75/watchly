"""Slack alerts.

Each project carries its own bot token and channel id (set through the API and
stored encrypted), so every client can have its own private channel. A site may
override the channel while reusing the project's token.

If a project has no Slack of its own, the global `SLACK_WEBHOOK_URL` is used as
a firehose fallback when one is configured.
"""

import logging

import httpx

from app.core.config import settings
from app.monitoring.alerts.base import Alerter, AlertEvent, AlertKind, SlackTarget

logger = logging.getLogger(__name__)

_EMOJI = {
    AlertKind.DOWN: ":rotating_light:",
    AlertKind.STILL_DOWN: ":warning:",
    AlertKind.RECOVERED: ":white_check_mark:",
}

#: Slack error codes worth explaining rather than echoing raw.
_HINTS = {
    "not_in_channel": (
        "the bot is not a member of that channel — run `/invite @YourBot` in it"
    ),
    "channel_not_found": (
        "no such channel id, or the bot cannot see it (private channels need "
        "the bot invited and the groups:write scope)"
    ),
    "invalid_auth": "the bot token was rejected — check it starts with `xoxb-`",
    "token_revoked": "the bot token has been revoked; issue a new one",
    "account_inactive": "the Slack app or workspace is disabled",
    "missing_scope": "the bot is missing chat:write — reinstall the app",
    "is_archived": "that channel is archived",
    "ratelimited": "Slack is rate limiting this workspace; the alert was dropped",
}


def explain(error: str) -> str:
    """Turn a Slack error code into something actionable."""
    hint = _HINTS.get(error)
    return f"{error} — {hint}" if hint else error


def build_blocks(event: AlertEvent) -> list[dict]:
    """Block Kit payload. Slack allows at most 10 fields in a section."""
    facts = event.facts()
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{_EMOJI[event.kind]} {event.subject}"[:150],
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*{label}*\n{value}"[:2000]}
                for label, value in facts[:10]
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": event.result.summary}],
        },
    ]


class SlackAlerter(Alerter):
    name = "slack"

    async def is_configured(self, event: AlertEvent) -> bool:
        return bool(event.slack) or bool(settings.SLACK_WEBHOOK_URL)

    async def send(self, event: AlertEvent) -> bool:
        if event.slack is not None:
            return await self._post_via_bot(event, event.slack)
        if settings.SLACK_WEBHOOK_URL:
            return await self._post_via_webhook(event)
        return False

    async def _post_via_bot(self, event: AlertEvent, target: SlackTarget) -> bool:
        payload = {
            "channel": target.channel_id,
            "text": f"{_EMOJI[event.kind]} {event.subject}",  # notification/fallback
            "blocks": build_blocks(event),
        }
        try:
            async with httpx.AsyncClient(timeout=settings.SLACK_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    settings.SLACK_API_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {target.bot_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                )
                response.raise_for_status()
                # Slack answers 200 even when it refuses the message, so the
                # body is the only thing that says whether it worked.
                body = response.json()
        except Exception:
            logger.exception("Slack request failed for %s", event.website.url)
            return False

        if not body.get("ok"):
            logger.error(
                "Slack refused the alert for %s in channel %s: %s",
                event.website.url,
                target.channel_id,
                explain(body.get("error", "unknown_error")),
            )
            return False

        logger.info(
            "Posted %s alert for %s to Slack channel %s",
            event.kind.value,
            event.website.url,
            target.channel_id,
        )
        return True

    async def _post_via_webhook(self, event: AlertEvent) -> bool:
        payload = {
            "text": f"{_EMOJI[event.kind]} {event.subject}",
            "blocks": build_blocks(event),
        }
        try:
            async with httpx.AsyncClient(timeout=settings.SLACK_TIMEOUT_SECONDS) as client:
                response = await client.post(settings.SLACK_WEBHOOK_URL, json=payload)
                response.raise_for_status()
        except Exception:
            logger.exception("Slack webhook failed for %s", event.website.url)
            return False
        logger.info(
            "Posted %s alert for %s to the fallback Slack webhook",
            event.kind.value,
            event.website.url,
        )
        return True
