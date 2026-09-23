"""Who a notification goes to. Pure functions over already-loaded objects."""

from collections.abc import Iterable

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.monitoring.alerts.base import SlackTarget
from app.monitoring.projects.models import Project


def dedupe_emails(*groups: Iterable[str]) -> tuple[str, ...]:
    """Every address once, first-seen order, compared case-insensitively."""
    seen: dict[str, None] = {}
    for group in groups:
        for email in group:
            cleaned = email.strip()
            if cleaned:
                seen.setdefault(cleaned.lower(), None)
    return tuple(seen)


def project_recipients(project: Project) -> tuple[str, ...]:
    """Who gets a project-wide message such as the monthly report: the members
    and extra addresses, plus the operators in ALERT_DEFAULT_EMAILS. Suspended
    members are already excluded by `Project.member_emails`."""
    return dedupe_emails(project.recipient_emails, settings.ALERT_DEFAULT_EMAILS)


def slack_target(project: Project | None, channel_id: str | None = None) -> SlackTarget | None:
    """The channel to post to, with the token decrypted.

    The bot token always comes from the project; `channel_id` (a website's own
    channel) overrides the project's default. Returns None when the project has
    no Slack set up, or the stored token cannot be decrypted.
    """
    if project is None or not project.slack_enabled:
        return None

    channel = channel_id or project.slack_channel_id
    if not channel:
        return None

    token = decrypt_secret(project.slack_bot_token)
    if not token:
        return None
    return SlackTarget(bot_token=token, channel_id=channel)
