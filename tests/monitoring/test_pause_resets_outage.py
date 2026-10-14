"""Pausing or resuming a site ends the outage it was in, so the first check
after a resume does not announce a recovery from downtime nobody watched.

No database: the site is a transient row, as in test_website_update_nulls."""

from datetime import datetime, timedelta, timezone

from app.monitoring.websites.models import WebsiteStatus
from app.monitoring.websites.schemas import WebsiteUpdate

from .test_website_update_nulls import _service, _website


def _down_site():
    website = _website()
    website.status = WebsiteStatus.DOWN
    website.down_since = datetime.now(timezone.utc) - timedelta(days=30)
    website.down_alerts_sent = 3
    website.consecutive_failures = 9
    website.slack_thread_ts = "1700000000.000100"
    website.slack_thread_channel = "C123"
    website.telegram_thread_message_id = 42
    website.telegram_thread_chat = "-100"
    return website


def _assert_outage_cleared(website):
    assert website.status is WebsiteStatus.UNKNOWN
    assert website.down_since is None
    assert website.down_alerts_sent == 0
    assert website.consecutive_failures == 0
    assert website.slack_thread_ts is None
    assert website.slack_thread_channel is None
    assert website.telegram_thread_message_id is None
    assert website.telegram_thread_chat is None


async def test_pausing_a_down_site_clears_the_outage():
    website = _down_site()

    await _service(website).update(1, WebsiteUpdate(is_enabled=False))

    assert website.is_enabled is False
    _assert_outage_cleared(website)


async def test_resuming_clears_an_outage_left_by_a_manual_check():
    website = _down_site()
    website.is_enabled = False

    await _service(website).update(1, WebsiteUpdate(is_enabled=True))

    assert website.is_enabled is True
    _assert_outage_cleared(website)


async def test_saving_without_a_toggle_keeps_the_outage():
    website = _down_site()
    started = website.down_since

    await _service(website).update(1, WebsiteUpdate(name="Other"))
    await _service(website).update(1, WebsiteUpdate(is_enabled=True))

    assert website.status is WebsiteStatus.DOWN
    assert website.down_since == started
    assert website.down_alerts_sent == 3
    assert website.slack_thread_ts == "1700000000.000100"
