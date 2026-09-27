"""Per-client rate limits on the endpoints anyone can call without signing in.

MAX_FAILED_LOGIN_ATTEMPTS stops anyone guessing one account's password, but
not one client trying a few common passwords on every account, mailing
temporary passwords to a list of addresses, or opening accounts in bulk. Each
RATE_LIMIT_* setting caps what one client may send to a group of endpoints;
past it they answer 429 with `Retry-After`.

A client is an IPv4 address, or an IPv6 /64: one host is usually handed a
whole /64, so counting its addresses one by one would be no limit at all.

The counts live in Postgres, so every worker of `uvicorn --workers N` shares
them, and each request bumps its count in one upsert before it is handled, so
requests sent in parallel cannot slip past the limit together.
"""

import ipaddress
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RateLimit, settings
from app.db.models.rate_limit import RateLimitBucket
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

#: For the `responses` of a rate-limited route.
RATE_LIMITED: dict[int | str, dict[str, Any]] = {
    429: {"description": "Too many requests from this client; retry after `Retry-After` seconds"}
}


def client_key(request: Request) -> str:
    """Who `request` counts against: its address, or its /64 for IPv6.

    Behind a proxy this is the address uvicorn took from X-Forwarded-For, which
    it does only for the proxies in FORWARDED_ALLOW_IPS.
    """
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host[:64] or "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


def rate_limit(scope: str) -> Any:
    """A route dependency counting each request against the client's
    `RATE_LIMIT_<SCOPE>`. Routes given the same `scope` share one count."""
    setting = f"RATE_LIMIT_{scope.upper()}"
    if not isinstance(getattr(settings, setting, None), RateLimit):
        raise ValueError(f"There is no {setting} setting.")

    async def check(request: Request) -> None:
        if settings.RATE_LIMIT_ENABLED:
            await _count(scope, client_key(request), getattr(settings, setting))

    return Depends(check)


async def _count(scope: str, client: str, limit: RateLimit) -> None:
    """Count one request, and raise 429 if it is over `limit`.

    Committed in a session of its own, so it counts even when the request
    goes on to fail and roll back.
    """
    now = datetime.now(UTC)
    new = insert(RateLimitBucket).values(
        scope=scope,
        client=client,
        hits=1,
        resets_at=now + timedelta(seconds=limit.seconds),
    )
    # The row is locked for the update, so parallel requests queue up here.
    closed = RateLimitBucket.resets_at <= now
    upsert = new.on_conflict_do_update(
        index_elements=[RateLimitBucket.scope, RateLimitBucket.client],
        set_={
            "hits": case((closed, 1), else_=RateLimitBucket.hits + 1),
            "resets_at": case(
                (closed, new.excluded.resets_at), else_=RateLimitBucket.resets_at
            ),
        },
    ).returning(RateLimitBucket.hits, RateLimitBucket.resets_at)
    async with AsyncSessionLocal() as session:
        hits, resets_at = (await session.execute(upsert)).one()
        await session.commit()

    if hits <= limit.hits:
        return
    if hits == limit.hits + 1:
        # Once per window, however hard the client keeps trying.
        logger.warning(
            "Rate limit %s hit by %s: over %d requests in %ds.",
            scope,
            client,
            limit.hits,
            limit.seconds,
        )
    seconds = max(1, math.ceil((resets_at - now).total_seconds()))
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Too many requests from your network. Try again in {_wait(seconds)}.",
        headers={"Retry-After": str(seconds)},
    )


def _wait(seconds: int) -> str:
    if seconds < 60:
        count, unit = seconds, "second"
    elif seconds < 7200:
        count, unit = math.ceil(seconds / 60), "minute"
    else:
        count, unit = math.ceil(seconds / 3600), "hour"
    return f"{count} {unit}{'' if count == 1 else 's'}"


async def purge_closed_windows(session: AsyncSession) -> int:
    """Delete every count whose window has closed; the client's next request
    would start from zero anyway. Called on every tick; returns how many were
    deleted."""
    result = await session.execute(
        delete(RateLimitBucket).where(RateLimitBucket.resets_at <= datetime.now(UTC))
    )
    await session.commit()
    return result.rowcount or 0
