"""Slack: drawing a `Message` as Block Kit, and posting it.

Each project carries its own bot token and channel id (set through the API and
stored encrypted), so every client can have its own private channel. A site may
override the channel while reusing the project's token.

If a project has no Slack of its own, the global `SLACK_WEBHOOK_URL` is used as
a firehose fallback when one is configured.

A bot post can reply in a thread (`SlackTarget.thread_ts`), which is how one
outage stays one thread; the webhook cannot, and always posts to the channel.

Slack's limits, which the builders below stay inside: a header is at most 150
characters, a section's text 3000, a field 2000, a section has at most 10
fields, and a message at most 50 blocks. A field or section with empty text is
rejected outright, so nothing here emits one.

Each message opens with the Watchly logo, which Slack fetches from
ALERT_LOGO_URL. Slack refuses a whole message whose image it cannot download,
so a refused one is sent again without the logo: an alert is never lost to it.
"""

import logging

import httpx

from app.core.config import settings
from app.monitoring.alerts.base import (
    Alerter,
    Message,
    Notification,
    NotificationKind,
    SiteRow,
    SlackTarget,
    logo_url,
    Tone,
    escape_mrkdwn,
)

logger = logging.getLogger(__name__)

_EMOJI = {
    NotificationKind.DOWN: ":rotating_light:",
    NotificationKind.STILL_DOWN: ":warning:",
    NotificationKind.RECOVERED: ":white_check_mark:",
    NotificationKind.SSL_EXPIRING: ":lock:",
    NotificationKind.DOMAIN_EXPIRING: ":calendar:",
    NotificationKind.NAMESERVERS_CHANGED: ":satellite_antenna:",
    NotificationKind.SLOW_RESPONSE: ":hourglass_flowing_sand:",
    NotificationKind.PACKET_LOSS: ":signal_strength:",
    NotificationKind.DNS_CHANGED: ":globe_with_meridians:",
    NotificationKind.MONTHLY_REPORT: ":bar_chart:",
    NotificationKind.INFRA_DOWN: ":rotating_light:",
    NotificationKind.INFRA_STILL_DOWN: ":warning:",
    NotificationKind.INFRA_RECOVERED: ":white_check_mark:",
    NotificationKind.INFRA_DEGRADED: ":large_yellow_circle:",
    NotificationKind.VPC_UNREACHABLE: ":no_entry:",
    NotificationKind.VPC_RECOVERED: ":link:",
    NotificationKind.ASG_SCALED_OUT: ":arrow_up:",
    NotificationKind.ASG_SCALED_IN: ":arrow_down:",
    NotificationKind.DEPLOY_STARTED: ":rocket:",
    NotificationKind.DEPLOY_FINISHED: ":checkered_flag:",
    NotificationKind.ACCOUNT_CAPACITY: ":straight_ruler:",
}

_TONE_DOT = {
    Tone.SUCCESS: ":large_green_circle:",
    Tone.WARNING: ":large_yellow_circle:",
    Tone.CRITICAL: ":red_circle:",
    Tone.INFO: ":large_blue_circle:",
}

#: Fact rows drawn per message. Two sections of ten fields is plenty; the
#: HTTP headers that trail an outage's facts are the first thing to go.
MAX_FACTS = 20
#: Websites listed in a report; the rest are counted.
MAX_SITES = 30
SITES_PER_SECTION = 10

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
    "invalid_blocks": (
        "Slack rejected the message layout — this is a Watchly bug, please report it"
    ),
}


def explain(error: str) -> str:
    """Turn a Slack error code into something actionable."""
    hint = _HINTS.get(error)
    return f"{error} — {hint}" if hint else error


def _mrkdwn(text: str, limit: int = 3000) -> dict:
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    return {"type": "mrkdwn", "text": text}


def _site_line(site: SiteRow) -> str:
    detail = [
        site.uptime,
        f"{site.downtime} down" if site.downtime != "none" else "no downtime",
    ]
    if site.incidents:
        detail.append(f"{site.incidents} incident{'' if site.incidents == 1 else 's'}")
    if site.response != "—":
        detail.append(f"{site.response} avg")
    name = escape_mrkdwn(site.name if len(site.name) <= 60 else site.name[:59] + "…")
    return f"{_TONE_DOT[site.tone]} *{name}* — {' · '.join(detail)}"


def _facts_sections(facts: list[tuple[str, str]]) -> list[dict]:
    fields = [
        _mrkdwn(f"*{escape_mrkdwn(label)}*\n{escape_mrkdwn(value) or '—'}", 2000)
        for label, value in facts[:MAX_FACTS]
    ]
    return [
        {"type": "section", "fields": fields[i : i + 10]}
        for i in range(0, len(fields), 10)
    ]


def _brand(logo: str) -> dict:
    """The logo and the name, above the header."""
    return {
        "type": "context",
        "elements": [
            {"type": "image", "image_url": logo, "alt_text": "Watchly"},
            _mrkdwn("*Watchly*"),
        ],
    }


def build_blocks(message: Message, logo: str | None = None) -> list[dict]:
    """The Block Kit payload for any message; `logo` is the logo's URL."""
    emoji = _EMOJI[message.kind]
    blocks: list[dict] = [_brand(logo)] if logo else []
    blocks += [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {message.title}"[:150],
                "emoji": True,
            },
        }
    ]
    if message.kind is NotificationKind.MONTHLY_REPORT:
        blocks.append(
            {"type": "context", "elements": [_mrkdwn(escape_mrkdwn(message.kicker))]}
        )
    if message.slack_body.strip():
        blocks.append({"type": "section", "text": _mrkdwn(message.slack_body.strip())})

    if message.stats:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    _mrkdwn(f"*{escape_mrkdwn(stat.label)}*\n{escape_mrkdwn(stat.value)}", 2000)
                    for stat in message.stats
                ],
            }
        )
    if message.sites:
        blocks.append({"type": "divider"})
        shown = message.sites[:MAX_SITES]
        for i in range(0, len(shown), SITES_PER_SECTION):
            lines = "\n".join(_site_line(s) for s in shown[i : i + SITES_PER_SECTION])
            blocks.append({"type": "section", "text": _mrkdwn(lines)})
        omitted = message.sites_omitted + len(message.sites) - len(shown)
        if omitted:
            blocks.append(
                {
                    "type": "context",
                    "elements": [_mrkdwn(f"…and {omitted} more website(s), not shown.")],
                }
            )

    blocks.extend(_facts_sections(message.facts))

    if message.note:
        blocks.append({"type": "context", "elements": [_mrkdwn(escape_mrkdwn(message.note))]})
    if message.link:
        label, url = message.link
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": label[:75]},
                        "url": url,
                    }
                ],
            }
        )
    return blocks


def build_payload(message: Message, logo: str | None = None) -> dict:
    """Everything Slack needs for the message, minus the channel."""
    return {
        # Shown in notifications and by clients that cannot draw blocks.
        "text": f"{_EMOJI[message.kind]} {escape_mrkdwn(message.subject)}",
        "blocks": build_blocks(message, logo),
    }


def _refused_image(error: str) -> bool:
    """Whether Slack refused a message over its blocks, which is how it says it
    could not download an image in them."""
    return "invalid_blocks" in error


class SlackAlerter(Alerter):
    name = "slack"

    async def is_configured(self, event: Notification) -> bool:
        return bool(event.slack) or bool(settings.SLACK_WEBHOOK_URL)

    async def send(self, event: Notification, message: Message) -> bool:
        if event.slack is not None:
            return await self._post_via_bot(event, event.slack, message)
        if settings.SLACK_WEBHOOK_URL:
            return await self._post_via_webhook(event, message)
        return False

    async def _post_via_bot(
        self, event: Notification, target: SlackTarget, message: Message
    ) -> bool:
        logo = logo_url()
        ok, error = await self._bot_request(event, target, message, logo)
        if not ok and logo and error and _refused_image(error):
            logger.warning(
                "Slack could not use the logo at %s (%s); sending %s without it.",
                logo,
                error,
                event.describe(),
            )
            ok, error = await self._bot_request(event, target, message, None)
        if not ok:
            if error:
                logger.error(
                    "Slack refused %s in channel %s: %s",
                    event.describe(),
                    target.channel_id,
                    explain(error),
                )
            return False
        logger.info("Posted %s to Slack channel %s", event.describe(), target.channel_id)
        return True

    async def _bot_request(
        self,
        event: Notification,
        target: SlackTarget,
        message: Message,
        logo: str | None,
    ) -> tuple[bool, str | None]:
        """(posted, Slack's error). The error is None when the request itself
        failed, which is logged here."""
        payload = {"channel": target.channel_id, **build_payload(message, logo)}
        if target.thread_ts:
            payload["thread_ts"] = target.thread_ts
            if target.broadcast:
                payload["reply_broadcast"] = True
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
            logger.exception("Slack request failed for %s", event.describe())
            return False, None

        if not body.get("ok"):
            return False, body.get("error", "unknown_error")
        event.slack_ts = body.get("ts")
        return True, None

    async def _post_via_webhook(self, event: Notification, message: Message) -> bool:
        logo = logo_url()
        try:
            async with httpx.AsyncClient(timeout=settings.SLACK_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    settings.SLACK_WEBHOOK_URL, json=build_payload(message, logo)
                )
                if logo and response.status_code == 400 and _refused_image(response.text):
                    logger.warning(
                        "Slack could not use the logo at %s; sending %s without it.",
                        logo,
                        event.describe(),
                    )
                    response = await client.post(
                        settings.SLACK_WEBHOOK_URL, json=build_payload(message)
                    )
                response.raise_for_status()
        except Exception:
            logger.exception("Slack webhook failed for %s", event.describe())
            return False
        logger.info("Posted %s to the fallback Slack webhook", event.describe())
        return True
