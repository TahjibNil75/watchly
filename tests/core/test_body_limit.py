"""Request bodies over the cap are refused with 413 before they are read in
full. See app/core/body_limit.py."""

import httpx
from fastapi import FastAPI, Request

from app.core.body_limit import MAX_REQUEST_BODY_BYTES, BodyLimitMiddleware

LIMIT = 1000

app = FastAPI()
app.add_middleware(BodyLimitMiddleware, limit=LIMIT)


@app.post("/echo")
async def echo(request: Request) -> dict[str, int]:
    return {"size": len(await request.body())}


def client(target=app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=target), base_url="http://t")


async def post(content) -> httpx.Response:
    async with client() as c:
        return await c.post("/echo", content=content)


async def test_body_at_the_limit_passes():
    response = await post(b"x" * LIMIT)
    assert response.status_code == 200 and response.json() == {"size": LIMIT}


async def test_declared_oversize_is_refused():
    response = await post(b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large."}


async def test_streamed_oversize_without_content_length_is_refused():
    async def chunks():
        for _ in range(10):
            yield b"x" * 200

    response = await post(chunks())
    assert response.status_code == 413


async def test_streamed_body_within_the_limit_passes():
    async def chunks():
        for _ in range(5):
            yield b"x" * 200

    response = await post(chunks())
    assert response.status_code == 200 and response.json() == {"size": LIMIT}


async def test_streamed_oversize_stops_reading_at_the_limit():
    pulled = 0

    async def endless():
        nonlocal pulled
        while pulled < 10_000_000:
            pulled += 100
            yield b"x" * 100

    response = await post(endless())
    assert response.status_code == 413
    assert pulled < 100_000


async def test_real_app_refuses_an_oversized_login():
    from app.main import app as real_app

    async with client(real_app) as c:
        response = await c.post(
            "/api/v1/auth/login", content=b"x" * (MAX_REQUEST_BODY_BYTES + 1)
        )
    assert response.status_code == 413
