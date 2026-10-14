"""A redirect to a very long URL is recorded, with the final URL cut to the
column's length. See `MAX_FINAL_URL` in app/monitoring/websites/checker.py."""

from app.monitoring.websites.checker import MAX_FINAL_URL, check_website
from app.monitoring.websites.models import CheckType, WebsiteCheck
from tests.monitoring.conftest import make_website


async def test_final_url_of_a_long_redirect_fits_its_column(server, client):
    site = make_website(server.url("/long-redirect"), check_type=CheckType.HTTP)
    result = await check_website(site, client=client)
    assert result.is_up is True, result.error
    assert result.redirected is True
    assert result.final_url is not None
    assert len(result.final_url) == MAX_FINAL_URL
    assert MAX_FINAL_URL == WebsiteCheck.final_url.type.length
