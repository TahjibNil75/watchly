"""PATCH with an explicit null for a NOT NULL column leaves the field alone
(`method: null` used to raise AttributeError in the validator), and only the
unique-target constraint is reported as a duplicate site.

There is no database in these tests: the site is a transient row and the
session a stand-in whose commit can fail."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.monitoring.websites.models import CheckType, Website
from app.monitoring.websites.schemas import WebsiteUpdate
from app.monitoring.websites.service import DuplicateWebsiteError, WebsiteService

NOT_NULL = [
    "name",
    "url",
    "method",
    "expected_status",
    "timeout_seconds",
    "check_interval_seconds",
    "max_down_alerts",
    "is_enabled",
    "alert_emails",
]


class Session:
    def __init__(self, commit_error: Exception | None = None) -> None:
        self.commit_error = commit_error
        self.rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True

    async def refresh(self, website) -> None:
        pass


def _website() -> Website:
    return Website(
        name="Shop",
        url="https://example.com/",
        check_type=CheckType.HTTP,
        dns_record_type=None,
        method="GET",
        expected_status=200,
        timeout_seconds=10,
        check_interval_seconds=300,
        max_down_alerts=4,
        is_enabled=True,
        alert_emails=["ops@example.com"],
        request_headers=[],
    )


def _service(website: Website, commit_error: Exception | None = None) -> WebsiteService:
    session = Session(commit_error)
    service = WebsiteService(session)

    async def get(website_id):
        return website

    async def commit(site, **kwargs):
        if session.commit_error:
            raise session.commit_error

    service.get = get
    service._commit_if_alertable = commit
    return service


@pytest.mark.parametrize("field", NOT_NULL)
async def test_explicit_null_is_ignored(field):
    website = _website()
    before = getattr(website, field)

    result = await _service(website).update(1, WebsiteUpdate(**{field: None}))

    assert result is website
    assert getattr(website, field) == before


async def test_method_is_upper_cased():
    website = _website()

    await _service(website).update(1, WebsiteUpdate(method="post"))

    assert website.method == "POST"


async def test_unique_violation_is_a_duplicate():
    error = IntegrityError(
        "UPDATE websites", {}, Exception('violates unique constraint "uq_websites_target"')
    )

    with pytest.raises(DuplicateWebsiteError):
        await _service(_website(), error).update(1, WebsiteUpdate(name="Other"))


async def test_other_integrity_error_is_not_a_duplicate():
    error = IntegrityError(
        "UPDATE websites", {}, Exception('null value in column "name" violates not-null')
    )

    with pytest.raises(IntegrityError):
        await _service(_website(), error).update(1, WebsiteUpdate(name="Other"))
