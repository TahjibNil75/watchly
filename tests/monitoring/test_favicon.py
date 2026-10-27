"""A site's favicon: which icon a page names, what counts as an icon, and the
cache. See app/monitoring/websites/favicon.py."""

import httpx
import pytest

from app.monitoring.websites import favicon
from app.monitoring.websites.models import CheckType

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
ICO = b"\x00\x00\x01\x00" + b"\x00" * 32
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
#: The test server answers one request per connection, so no reusing it.
CLOSE = {"Connection": "close"}
HTML = {"Content-Type": "text/html", **CLOSE}


@pytest.fixture(autouse=True)
def plain_client(monkeypatch):
    """`new_client()` refuses the loopback address the test server is on."""
    favicon._cache.clear()
    monkeypatch.setattr(favicon, "new_client", lambda: httpx.AsyncClient(follow_redirects=True))
    yield
    favicon._cache.clear()


def page(*links: str) -> tuple[dict[str, str], bytes]:
    return HTML, f"<html><head>{''.join(links)}</head><body>hi</body></html>".encode()


class TestSniff:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (PNG, "image/png"),
            (ICO, "image/x-icon"),
            (b"GIF89a" + b"\x00" * 8, "image/gif"),
            (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
            (SVG, "image/svg+xml"),
            (b"\xef\xbb\xbf  <svg></svg>", "image/svg+xml"),
            (b"<html><body>not found</body></html>", None),
            (b"<?xml version='1.0'?><rss></rss>", None),
            (b"", None),
        ],
    )
    def test_judged_by_bytes(self, data, expected):
        assert favicon.sniff(data) == expected


class TestIconUrls:
    def test_resolved_against_the_page_and_ranked(self):
        html = (
            b'<link rel="apple-touch-icon" href="/touch.png">'
            b'<link rel="shortcut icon" href="img/fav.ico">'
        )
        assert favicon.icon_urls(html, "https://example.com/app/") == [
            "https://example.com/app/img/fav.ico",
            "https://example.com/touch.png",
        ]

    def test_base_href_is_honoured(self):
        html = b'<base href="https://cdn.example.net/x/"><link rel="icon" href="a.png">'
        assert favicon.icon_urls(html, "https://example.com/") == ["https://cdn.example.net/x/a.png"]

    def test_data_uris_and_other_links_are_left_out(self):
        html = (
            b'<link rel="icon" href="data:image/png;base64,AAAA">'
            b'<link rel="stylesheet" href="/site.css">'
            b'<link rel="icon" href="javascript:alert(1)">'
        )
        assert favicon.icon_urls(html, "https://example.com/") == []

    def test_broken_html_does_not_raise(self):
        assert favicon.icon_urls(b"<link rel=icon href=/a.png <<<", "https://example.com/") is not None


class TestFind:
    async def test_icon_named_by_the_page(self, server):
        server.routes["/"] = page('<link rel="icon" href="/brand.png">')
        server.routes["/brand.png"] = (CLOSE, PNG)
        icon = await favicon._find(server.url("/"))
        assert icon == favicon.Favicon(PNG, "image/png")

    async def test_falls_back_to_favicon_ico(self, server):
        server.routes["/"] = page()
        server.routes["/favicon.ico"] = (CLOSE, ICO)
        icon = await favicon._find(server.url("/"))
        assert icon == favicon.Favicon(ICO, "image/x-icon")

    async def test_type_comes_from_the_bytes_not_the_header(self, server):
        server.routes["/"] = page('<link rel="icon" href="/brand.png">')
        server.routes["/brand.png"] = (HTML, PNG)
        icon = await favicon._find(server.url("/"))
        assert icon.content_type == "image/png"

    async def test_a_page_that_is_not_an_image_is_no_icon(self, server):
        # The usual "not found" page served with a 200 at /favicon.ico.
        server.routes["/"] = page()
        server.routes["/favicon.ico"] = (HTML, b"<html>Welcome</html>")
        assert await favicon._find(server.url("/")) is None

    async def test_oversized_icon_is_skipped_for_the_next_candidate(self, server):
        server.routes["/"] = page('<link rel="icon" href="/huge.png">')
        server.routes["/huge.png"] = (CLOSE, PNG + b"\x00" * favicon.MAX_ICON_BYTES)
        server.routes["/favicon.ico"] = (CLOSE, ICO)
        icon = await favicon._find(server.url("/"))
        assert icon == favicon.Favicon(ICO, "image/x-icon")

    async def test_svg_is_kept(self, server):
        server.routes["/"] = page('<link rel="icon" type="image/svg+xml" href="/i.svg">')
        server.routes["/i.svg"] = (CLOSE, SVG)
        icon = await favicon._find(server.url("/"))
        assert icon == favicon.Favicon(SVG, "image/svg+xml")

    async def test_unreachable_site_has_no_icon(self):
        assert await favicon._find("http://127.0.0.1:9/") is None


class TestFaviconFor:
    async def test_only_http_checks_have_one(self, server):
        for kind in (CheckType.PING, CheckType.DNS, CheckType.MAIL):
            assert await favicon.favicon_for(server.url("/"), kind) is None
        assert favicon._cache == {}

    async def test_found_icon_is_cached_per_origin(self, server, monkeypatch):
        server.routes["/"] = page()
        server.routes["/other"] = page()
        server.routes["/favicon.ico"] = (CLOSE, ICO)
        first = await favicon.favicon_for(server.url("/"), CheckType.HTTP)
        assert first == favicon.Favicon(ICO, "image/x-icon")

        async def boom(url):
            raise AssertionError("fetched again")

        monkeypatch.setattr(favicon, "_find", boom)
        # Another page of the same origin shares the entry.
        assert await favicon.favicon_for(server.url("/other"), CheckType.HTTP) == first

    async def test_miss_is_cached_too(self, server, monkeypatch):
        server.routes["/"] = page()
        server.routes["/favicon.ico"] = (HTML, b"<html></html>")
        assert await favicon.favicon_for(server.url("/"), CheckType.HTTP) is None
        calls = []

        async def spy(url):
            calls.append(url)

        monkeypatch.setattr(favicon, "_find", spy)
        assert await favicon.favicon_for(server.url("/"), CheckType.HTTP) is None
        assert calls == []

    async def test_expired_entry_is_fetched_again(self, server):
        server.routes["/"] = page()
        server.routes["/favicon.ico"] = (CLOSE, ICO)
        await favicon.favicon_for(server.url("/"), CheckType.HTTP)
        key = favicon._cache_key(server.url("/"))
        favicon._cache[key] = (0.0, None)
        assert await favicon.favicon_for(server.url("/"), CheckType.HTTP) is not None

    async def test_cache_is_bounded(self, monkeypatch):
        monkeypatch.setattr(favicon, "MAX_CACHED", 3)

        async def none(url):
            return None

        monkeypatch.setattr(favicon, "_find", none)
        for n in range(6):
            await favicon.favicon_for(f"http://site{n}.example/", CheckType.HTTP)
        assert list(favicon._cache) == [f"http://site{n}.example" for n in (3, 4, 5)]
