"""Cap how much request body the API will read.

Starlette reads a JSON body in full before Pydantic sees it, so without a cap
an unauthenticated `POST /auth/login` can be fed gigabytes. Caddy enforces the
same limit in front (`request_body` in frontend/Caddyfile); this covers direct
access to the API port.
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Far beyond any real request: the largest is a Docker agent's gzip'd push.
#: Keep it equal to `request_body max_size` in frontend/Caddyfile.
MAX_REQUEST_BODY_BYTES = 1 << 20

_TOO_LARGE = json.dumps({"detail": "Request body too large."}).encode()


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Answer 413 to a request body over `limit` bytes, whether it declares its
    size in `Content-Length` or streams it chunked: the declared size is refused
    before any of the body is read, and a streamed one is cut off as it passes
    the limit."""

    def __init__(self, app: ASGIApp, limit: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0  # the server rejects a malformed one itself
                if declared > self.limit:
                    await self._refuse(send)
                    return

        received = 0
        started = False

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _BodyTooLarge:
            # Too late to change an answer already begun; the connection is
            # dropped as for any failure then.
            if started:
                raise
            await self._refuse(send)

    @staticmethod
    async def _refuse(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_TOO_LARGE)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _TOO_LARGE})
