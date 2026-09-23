"""Generic JSON webhook alerts.

Off unless ALERT_WEBHOOK_URL is set. Posts a machine-readable payload, so it
can drive Telegram bots, PagerDuty bridges, or your own automation.
"""

import logging

import httpx

from app.core.config import settings
from app.monitoring.alerts.base import Alerter, AlertEvent

logger = logging.getLogger(__name__)


class WebhookAlerter(Alerter):
    name = "webhook"

    async def is_configured(self, event: AlertEvent) -> bool:
        return bool(settings.ALERT_WEBHOOK_URL)

    def build_payload(self, event: AlertEvent) -> dict:
        result = event.result
        return {
            "event": event.kind.value,
            "subject": event.subject,
            "summary": result.summary,
            "website": {
                "id": event.website.id,
                "name": event.website.name,
                "url": event.website.url,
                "expected_status": event.website.expected_status,
                "check_interval_seconds": event.website.check_interval_seconds,
            },
            "check": {
                "checked_at": result.checked_at.isoformat(),
                "is_up": result.is_up,
                "status_code": result.status_code,
                "reason": result.reason,
                "response_time_ms": result.response_time_ms,
                "error": result.error,
                "error_type": result.error_type,
                "final_url": result.final_url,
                "redirected": result.redirected,
                "content_length": result.content_length,
                "headers": result.headers,
            },
            "outage": {
                "downtime_seconds": round(event.downtime_seconds),
                "downtime_human": event.downtime,
                "consecutive_failures": event.website.consecutive_failures,
                "attempt": event.attempt,
                "max_attempts": event.max_attempts,
            },
        }

    async def send(self, event: AlertEvent) -> bool:
        if not await self.is_configured(event):
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    settings.ALERT_WEBHOOK_URL, json=self.build_payload(event)
                )
                response.raise_for_status()
        except Exception:
            logger.exception("Failed to post webhook alert for %s", event.website.url)
            return False
        logger.info("Posted %s webhook for %s", event.kind.value, event.website.url)
        return True
