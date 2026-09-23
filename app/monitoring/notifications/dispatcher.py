"""Sending one notification: look up its settings, render it, fan out to channels."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.monitoring.alerts.base import Alerter, Message, Notification
from app.monitoring.alerts.email import EmailAlerter
from app.monitoring.alerts.slack import SlackAlerter
from app.monitoring.alerts.webhook import WebhookAlerter
from app.monitoring.notifications.service import (
    EffectiveSetting,
    NotificationSettingsService,
    default_setting,
)
from app.monitoring.notifications.templating import compose_message

logger = logging.getLogger(__name__)


def default_alerters() -> list[Alerter]:
    """Every channel; each one no-ops when it is not configured."""
    return [EmailAlerter(), SlackAlerter(), WebhookAlerter()]


class Notifier:
    """Renders a notification with its project's settings and sends it."""

    def __init__(self, session: AsyncSession, alerters: list[Alerter] | None = None) -> None:
        self.session = session
        self.alerters = default_alerters() if alerters is None else alerters

    async def settings_for(self, event: Notification) -> EffectiveSetting:
        """The event's effective settings. A failed lookup must not swallow the
        notification, so it falls back to the built-in behaviour."""
        try:
            return await NotificationSettingsService(self.session).effective(
                event.kind, event.project_id
            )
        except Exception:
            logger.exception(
                "Could not load notification settings for %s; using defaults.", event.describe()
            )
            await self.session.rollback()
            return default_setting(event.kind)

    @staticmethod
    def render(event: Notification, setting: EffectiveSetting) -> Message:
        """Compose with the configured templates; if they somehow fail, with the
        built-in ones — an alert in the default wording beats no alert."""
        try:
            return compose_message(event, setting.subject, setting.body)
        except Exception:
            logger.exception("Template failed for %s; using the default wording.", event.describe())
            fallback = default_setting(event.kind)
            return compose_message(event, fallback.subject, fallback.body)

    async def dispatch(self, event: Notification) -> tuple[str, ...]:
        """Send `event` on every channel that is both configured and switched on
        for its project. Returns the channels that delivered. Never raises."""
        if not settings.ALERTS_ENABLED:
            logger.info("ALERTS_ENABLED is false; suppressing %s", event.describe())
            return ()

        setting = await self.settings_for(event)
        switches = {"email": setting.email_enabled, "slack": setting.slack_enabled}
        # Channels without a switch (the webhook) are always active.
        active = [a for a in self.alerters if switches.get(a.name, True)]
        if not active:
            logger.info("%s is switched off for its project; not sent.", event.describe())
            return ()

        message = self.render(event, setting)
        delivered: list[str] = []
        for alerter in active:
            try:
                if not await alerter.is_configured(event):
                    continue
                if await alerter.send(event, message):
                    delivered.append(alerter.name)
            except Exception:
                # A channel blowing up must not stop the others, or the loop.
                logger.exception("Alerter %r failed for %s", alerter.name, event.describe())
        if not delivered:
            logger.warning("No channel delivered: %s", event.describe())
        return tuple(delivered)
