"""HTTP checks read a bounded slice of a response, however big it claims to be
or inflates to. See `read_capped` in app/monitoring/websites/checker.py."""

import asyncio
import tracemalloc

import httpx
import pytest

from app.monitoring.websites import checker
from app.monitoring.websites.checker import MAX_SEARCHED_BYTES, CappedBody
from tests.monitoring.conftest import PAGE, make_website

#: Far under what the bomb inflates to; well over what a capped read holds.
MEMORY_BUDGET = 20 * 1024 * 1024


async def read(client: httpx.AsyncClient, url: str, limit: int = MAX_SEARCHED_BYTES):
    async with client.stream("GET", url) as response:
        return response, await checker.read_capped(response, limit)


class TestReadCapped:
    @pytest.mark.parametrize("path", ["/page", "/gzip", "/x-gzip", "/deflate", "/raw-deflate"])
    async def test_small_body_is_read_whole_and_decoded(self, server, client, path):
        _, body = await read(client, server.url(path))
        assert body.data == PAGE
        assert body.truncated is False

    async def test_empty_body(self, server, client):
        _, body = await read(client, server.url("/empty"))
        assert body == CappedBody(b"", truncated=False)

    async def test_large_body_stops_at_the_limit(self, server, client):
        _, body = await read(client, server.url("/large"))
        assert len(body.data) == MAX_SEARCHED_BYTES
        assert body.truncated is True

    async def test_body_of_exactly_the_limit_is_not_truncated(self, server, client):
        _, body = await read(client, server.url("/large"), limit=3_000_000)
        assert len(body.data) == 3_000_000
        assert body.truncated is False

    async def test_endless_body_stops_and_the_connection_is_dropped(self, server, client):
        _, body = await read(client, server.url("/endless"))
        assert len(body.data) == MAX_SEARCHED_BYTES
        assert body.truncated is True
        # Leaving the stream closes the connection, so the server's writes fail.
        await asyncio.wait_for(server.endless_closed.wait(), timeout=5)
        assert server.endless_sent < 50 * MAX_SEARCHED_BYTES

    async def test_decompression_bomb_is_cut_off_without_inflating_it(self, server, client):
        tracemalloc.start()
        try:
            _, body = await read(client, server.url("/bomb"))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert len(body.data) == MAX_SEARCHED_BYTES
        assert body.truncated is True
        assert body.data == b"a" * MAX_SEARCHED_BYTES
        assert peak < MEMORY_BUDGET

    async def test_corrupt_gzip_is_a_decoding_error(self, server, client):
        with pytest.raises(httpx.DecodingError):
            await read(client, server.url("/corrupt-gzip"))


class TestBodyLength:
    async def test_whole_body_is_its_own_length(self, server, client):
        response, body = await read(client, server.url("/page"))
        assert checker.body_length(response, body) == len(PAGE)

    async def test_decoded_length_not_the_compressed_header(self, server, client):
        response, body = await read(client, server.url("/gzip"))
        assert checker.body_length(response, body) == len(PAGE)

    async def test_truncated_body_reports_the_declared_length(self, server, client):
        response, body = await read(client, server.url("/large"))
        assert body.truncated
        assert checker.body_length(response, body) == 3_000_000

    async def test_truncated_compressed_body_reports_what_was_read(self, server, client):
        response, body = await read(client, server.url("/bomb"))
        # Content-Length counts the compressed bytes: fewer than were read.
        assert checker.body_length(response, body) == MAX_SEARCHED_BYTES

    async def test_truncated_body_without_a_header_reports_what_was_read(self, server, client):
        response, body = await read(client, server.url("/endless"))
        assert checker.body_length(response, body) == MAX_SEARCHED_BYTES


class TestContentProblem:
    def body(self, text: str) -> CappedBody:
        return CappedBody(text.encode(), truncated=False)

    def test_no_rules_no_problem(self):
        site = make_website("http://x")
        assert checker.content_problem(site, self.body("anything"), None) is None

    def test_must_contain_found(self):
        site = make_website("http://x", must_contain="ok")
        assert checker.content_problem(site, self.body("all ok"), None) is None

    def test_must_contain_missing(self):
        site = make_website("http://x", must_contain="ok")
        problem = checker.content_problem(site, self.body("nope"), None)
        assert problem and problem[0] == "content_missing"

    def test_must_not_contain_present(self):
        site = make_website("http://x", must_not_contain="error")
        problem = checker.content_problem(site, self.body("an error"), None)
        assert problem and problem[0] == "content_forbidden"

    def test_honours_the_response_encoding(self):
        site = make_website("http://x", must_contain="café")
        body = CappedBody("café".encode("latin-1"), truncated=False)
        assert checker.content_problem(site, body, "latin-1") is None


class TestHttpProbe:
    async def probe(self, client, url, **fields):
        return await checker._http_probe(make_website(url, **fields), client)

    async def test_ok_page(self, server, client):
        result = await self.probe(client, server.url("/page"), must_contain="hello")
        assert result.is_up
        assert result.status_code == 200
        assert result.content_length == len(PAGE)
        assert result.ip_address == "127.0.0.1"

    async def test_gzip_page_content_rules_see_the_decoded_text(self, server, client):
        result = await self.probe(client, server.url("/gzip"), must_contain="hello world")
        assert result.is_up
        assert result.content_length == len(PAGE)

    async def test_missing_content_is_down(self, server, client):
        result = await self.probe(client, server.url("/page"), must_contain="absent")
        assert not result.is_up
        assert result.error_type == "content_missing"

    async def test_forbidden_content_is_down(self, server, client):
        result = await self.probe(client, server.url("/page"), must_not_contain="hello")
        assert not result.is_up
        assert result.error_type == "content_forbidden"

    async def test_wrong_status_is_down(self, server, client):
        result = await self.probe(client, server.url("/page"), expected_status=204)
        assert not result.is_up
        assert result.error_type == "unexpected_status"

    async def test_endless_body_finishes_with_a_bounded_length(self, server, client):
        result = await self.probe(client, server.url("/endless"))
        assert result.is_up
        assert result.content_length == MAX_SEARCHED_BYTES

    async def test_large_body_records_the_declared_length(self, server, client):
        result = await self.probe(client, server.url("/large"))
        assert result.is_up
        assert result.content_length == 3_000_000

    async def test_bomb_stays_within_the_memory_budget(self, server, client):
        tracemalloc.start()
        try:
            result = await self.probe(client, server.url("/bomb"), must_contain="aaa")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert result.is_up
        assert result.content_length == MAX_SEARCHED_BYTES
        assert peak < MEMORY_BUDGET

    async def test_corrupt_body_is_down_not_raised(self, server, client):
        result = await self.probe(client, server.url("/corrupt-gzip"))
        assert not result.is_up
        assert result.error

    async def test_unreachable_site_is_down(self, client):
        result = await self.probe(client, "http://127.0.0.1:1/", timeout_seconds=2)
        assert not result.is_up
        assert result.error_type


class TestTimedRequest:
    async def test_body_and_response_survive_the_stream_closing(self, server, client):
        outcome = await checker.timed_request(client, "GET", server.url("/gzip"), timeout=5)
        assert outcome.error_type is None
        assert outcome.response.status_code == 200
        assert outcome.body.data == PAGE
        assert outcome.address == "127.0.0.1"

    async def test_endless_body_is_capped(self, server, client):
        outcome = await checker.timed_request(client, "GET", server.url("/endless"), timeout=5)
        assert len(outcome.body.data) == MAX_SEARCHED_BYTES
        assert outcome.body.truncated

    async def test_failure_comes_back_classified(self, client):
        outcome = await checker.timed_request(client, "GET", "http://127.0.0.1:1/", timeout=2)
        assert outcome.response is None
        assert outcome.error_type == "connect_refused"
        assert outcome.body.data == b""
