"""Who a notification goes to. Pure functions over already-loaded objects."""

from collections.abc import Iterable

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.monitoring.alerts.base import SlackTarget, TelegramTarget
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


def slack_target(
    project: Project | None,
    channel_id: str | None = None,
    bot_token: str | None = None,
) -> SlackTarget | None:
    """The channel to post to, with the token decrypted.

    `channel_id` and `bot_token` are a website's own settings. A site with its
    own (encrypted) `bot_token` posts with it to its own channel, and the
    project's mute switch does not apply — it is not the project's Slack.
    Otherwise the token comes from the project, and `channel_id` overrides the
    project's default channel. Returns None when nothing is set up, Slack is
    muted, or the stored token cannot be decrypted.
    """
    if bot_token:
        channel, ciphertext = channel_id, bot_token
    elif project is None or not project.slack_enabled:
        return None
    else:
        channel = channel_id or project.slack_channel_id
        ciphertext = project.slack_bot_token
    if not channel:
        return None

    token = decrypt_secret(ciphertext)
    if not token:
        return None
    return SlackTarget(bot_token=token, channel_id=channel)


def telegram_target(
    project: Project | None,
    chat_id: str | None = None,
    bot_token: str | None = None,
) -> TelegramTarget | None:
    """The chat to send to, with the token decrypted.

    Resolved exactly like `slack_target`: a site's own (encrypted)
    `bot_token` sends to its own `chat_id` and ignores the project's mute
    switch; otherwise the project's bot is used and `chat_id` overrides the
    project's default chat. Returns None when nothing is set up, Telegram is
    muted, or the stored token cannot be decrypted.
    """
    if bot_token:
        chat, ciphertext = chat_id, bot_token
    elif project is None or not project.telegram_enabled:
        return None
    else:
        chat = chat_id or project.telegram_chat_id
        ciphertext = project.telegram_bot_token
    if not chat:
        return None

    token = decrypt_secret(ciphertext)
    if not token:
        return None
    return TelegramTarget(bot_token=token, chat_id=chat)
