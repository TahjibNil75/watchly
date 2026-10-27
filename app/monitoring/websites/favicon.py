"""A site's favicon, fetched by Watchly and served from its own origin.

The browser never talks to the monitored site or to a favicon service: that
would show every hostname to a third party, and could not reach a site on a
private network. Instead the icon is fetched here, through the same
egress-guarded client as a check, so it can reach exactly the addresses a
check may and no others.

The page is read for `<link rel="icon">` (then `apple-touch-icon`), and
`/favicon.ico` is tried last. Whatever comes back is judged by its first bytes,
not by the `Content-Type` the site claims, and only a PNG, ICO, GIF, JPEG,
WebP or SVG up to MAX_ICON_BYTES is kept. Results, misses included, are cached
per origin in memory, so a long list costs one fetch per site per day.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from app.monitoring.websites.checker import USER_AGENT, new_client, read_capped
from app.monitoring.websites.models import CheckType

logger = logging.getLogger(__name__)

#: Seconds each request (the page, then an icon) may take.
FETCH_TIMEOUT = 5.0
#: How much of the page is searched for icon links; they sit in `<head>`.
MAX_PAGE_BYTES = 256 * 1024
#: Largest icon kept. Favicons are a few KB; this only stops a page's hero image.
MAX_ICON_BYTES = 128 * 1024
#: Icons tried per site, after the page's own links are ranked.
MAX_CANDIDATES = 4
#: Seconds a found icon is kept, and a miss, so a site that is down is retried sooner.
HIT_TTL = 24 * 60 * 60
MISS_TTL = 60 * 60
#: Origins remembered; the oldest go first.
MAX_CACHED = 1000
#: Fetches running at once, so a cold list of a hundred sites is not a hundred connections.
MAX_PARALLEL = 8


@dataclass(frozen=True, slots=True)
class Favicon:
    data: bytes
    #: Judged from `data`, never taken from the site.
    content_type: str


class _IconLinks(HTMLParser):
    """The `<link>` tags that name an icon, as `(rank, href)`: lower is better."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[int, str]] = []
        self._base: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "base" and self._base is None and values.get("href"):
            self._base = values["href"].strip()
        if tag != "link" or not values.get("href", "").strip():
            return
        rels = values.get("rel", "").lower().split()
        if "icon" in rels:
            rank = 0
        elif "apple-touch-icon" in rels or "apple-touch-icon-precomposed" in rels:
            rank = 1
        else:
            return
        self.found.append((rank, values["href"].strip()))

    @property
    def base(self) -> str | None:
        return self._base


def icon_urls(page: bytes, page_url: str) -> list[str]:
    """The icon URLs a page names, best first, resolved against the page; the
    ones that are not http(s) (data: URIs, say) are left out."""
    parser = _IconLinks()
    try:
        parser.feed(page.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:  # noqa: BLE001 - a page the parser chokes on just names no icon
        pass
    base = urljoin(page_url, parser.base) if parser.base else page_url
    urls: list[str] = []
    for _, href in sorted(parser.found, key=lambda found: found[0]):
        url = urljoin(base, href)
        if urlsplit(url).scheme in ("http", "https") and url not in urls:
            urls.append(url)
    return urls


def sniff(data: bytes) -> str | None:
    """The image type `data` is, from its first bytes, or None for anything
    that is not an icon format we serve."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in data[:1024].lower():
        return "image/svg+xml"
    return None


async def _get(client: httpx.AsyncClient, url: str, limit: int) -> tuple[httpx.Response, bytes] | None:
    """A GET that reads at most `limit` bytes; None on any failure or non-2xx.
    Only the user agent is sent, never a site's configured headers: those may
    hold credentials meant for its check."""
    try:
        async with client.stream(
            "GET", url, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT}
        ) as response:
            if not response.is_success:
                return None
            body = await read_capped(response, limit)
            return response, body.data
    except Exception as exc:  # noqa: BLE001 - no icon is the whole failure mode
        logger.debug("Favicon fetch of %s failed: %s", url, exc)
        return None


async def _find(url: str) -> Favicon | None:
    candidates: list[str] = []
    origin = urlsplit(url)
    fallback = f"{origin.scheme}://{origin.netloc}/favicon.ico"
    async with new_client() as client:
        page = await _get(client, url, MAX_PAGE_BYTES)
        if page is not None:
            response, body = page
            if "html" in response.headers.get("content-type", "html").lower():
                candidates = icon_urls(body, str(response.url))
            # A redirect may have landed on another host, whose icon is the site's.
            final = response.url
            fallback = f"{final.scheme}://{final.netloc.decode()}/favicon.ico"
        if fallback not in candidates:
            candidates.append(fallback)
        for candidate in candidates[:MAX_CANDIDATES]:
            icon = await _get(client, candidate, MAX_ICON_BYTES + 1)
            if icon is None:
                continue
            data = icon[1]
            if not data or len(data) > MAX_ICON_BYTES:
                continue
            content_type = sniff(data)
            if content_type is not None:
                return Favicon(data, content_type)
    return None


_cache: dict[str, tuple[float, Favicon | None]] = {}
_inflight: dict[str, asyncio.Task[Favicon | None]] = {}
_slots: asyncio.Semaphore | None = None


def _cache_key(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


async def _fetch_limited(url: str) -> Favicon | None:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(MAX_PARALLEL)
    async with _slots:
        try:
            return await _find(url)
        except Exception:  # noqa: BLE001 - a failed lookup is a miss, not a 500
            logger.warning("Favicon lookup for %s failed", url, exc_info=True)
            return None


async def favicon_for(url: str, check_type: CheckType) -> Favicon | None:
    """The icon of the site at `url`, from the cache or fetched now; None when
    it has none, or is not an HTTP check (a ping, DNS or mail check has no
    web page). Takes values, not a `Website`, so a caller can let go of its
    database connection before the slow part."""
    if check_type != CheckType.HTTP:
        return None
    key = _cache_key(url)
    cached = _cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_fetch_limited(url))
        _inflight[key] = task

        def done(finished: asyncio.Task[Favicon | None]) -> None:
            _inflight.pop(key, None)
            if finished.cancelled():
                return
            icon = finished.result()
            if len(_cache) >= MAX_CACHED:
                _cache.pop(next(iter(_cache)))
            _cache.pop(key, None)
            _cache[key] = (time.monotonic() + (HIT_TTL if icon else MISS_TTL), icon)

        task.add_done_callback(done)
    # Shielded: one caller going away must not cancel the fetch the others wait on.
    return await asyncio.shield(task)
