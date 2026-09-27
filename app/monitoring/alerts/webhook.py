"""Generic JSON webhook alerts.

Off unless ALERT_WEBHOOK_URL is set. Posts a machine-readable payload, so it
can drive PagerDuty bridges, chat bots, or your own automation.

Unlike email, Slack and Telegram it has no per-project switch: it is a global firehose
that an operator wires up once, and it receives every kind of notification.
"""

import logging

import httpx

from app.core.config import settings
from app.monitoring.alerts.base import Alerter, Message, Notification

logger = logging.getLogger(__name__)


class WebhookAlerter(Alerter):
    name = "webhook"

    async def is_configured(self, event: Notification) -> bool:
        return bool(settings.ALERT_WEBHOOK_URL)

    async def send(self, event: Notification, message: Message) -> bool:
        if not await self.is_configured(event):
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    settings.ALERT_WEBHOOK_URL, json=event.payload(message.subject)
                )
                response.raise_for_status()
        except Exception:
            logger.exception("Failed to post webhook for %s", event.describe())
            return False
        logger.info("Posted webhook for %s", event.describe())
        return True
