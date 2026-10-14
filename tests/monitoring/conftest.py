"""A throwaway HTTP server on a real socket. A mock transport would not do:
httpx reads its body up front, so it cannot show what a stream does with an
endless or hugely inflating one."""

import asyncio
import gzip
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest_asyncio

from app.monitoring.websites.models import Website

PAGE = b"hello world " * 1000


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


@dataclass
class Server:
    base: str = ""
    #: Bytes the `/endless` route wrote before the client hung up.
    endless_sent: int = 0
    #: Set once the client has closed the `/endless` connection.
    endless_closed: asyncio.Event = field(default_factory=asyncio.Event)
    #: path -> (extra headers, body); `Content-Length` is added.
    routes: dict[str, tuple[dict[str, str], bytes]] = field(default_factory=dict)

    def url(self, path: str) -> str:
        return f"{self.base}{path}"


@pytest_asyncio.fixture
async def server() -> AsyncIterator[Server]:
    state = Server()
    state.routes = {
        "/page": ({}, PAGE),
        "/gzip": ({"Content-Encoding": "gzip"}, gzip.compress(PAGE)),
        "/x-gzip": ({"Content-Encoding": "x-gzip"}, gzip.compress(PAGE)),
        "/deflate": ({"Content-Encoding": "deflate"}, zlib.compress(PAGE)),
        "/raw-deflate": ({"Content-Encoding": "deflate"}, _raw_deflate(PAGE)),
        "/corrupt-gzip": ({"Content-Encoding": "gzip"}, b"\x1f\x8bnot gzip at all"),
        # ~100 MB inflated, a few hundred KB on the wire.
        "/bomb": ({"Content-Encoding": "gzip"}, gzip.compress(b"a" * (100 * 1024 * 1024), 9)),
        # 3 MB, over the cap, with its length declared.
        "/large": ({}, b"y" * 3_000_000),
        "/empty": ({}, b""),
        "/landing": ({}, PAGE),
    }

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        path = ""
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            path = head.split()[1].decode()
            if path == "/endless":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n")
                while True:
                    writer.write(b"x" * 65536)
                    state.endless_sent += 65536
                    await writer.drain()
            if path == "/long-redirect":
                # Answers with a Location far past what `final_url` can hold.
                location = f"/landing?pad={'x' * 3000}"
                writer.write(
                    f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
                )
                await writer.drain()
                await reader.read()
                return
            headers, body = state.routes[path.split("?")[0]]
            lines = [b"HTTP/1.1 200 OK", b"Content-Length: %d" % len(body)]
            lines += [f"{k}: {v}".encode() for k, v in headers.items()]
            writer.write(b"\r\n".join(lines) + b"\r\n\r\n" + body)
            await writer.drain()
            # Stay connected until the client hangs up, as a real server
            # does: its peer address is read off the open connection.
            await reader.read()
        except (ConnectionError, asyncio.IncompleteReadError):
            if path == "/endless":
                state.endless_closed.set()
        finally:
            writer.close()

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    state.base = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    try:
        yield state
    finally:
        listener.close()
        await listener.wait_closed()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    # A plain client: `new_client()` refuses loopback addresses.
    async with httpx.AsyncClient() as http:
        yield http


def make_website(url: str, **fields) -> Website:
    defaults = dict(
        url=url,
        method="GET",
        expected_status=200,
        timeout_seconds=10,
        retries_on_failure=0,
        request_headers=[],
    )
    return Website(**{**defaults, **fields})
