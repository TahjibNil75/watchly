"""Which country a request comes from, and keeping some countries out.

Watchly has no GeoIP database of its own. Whatever sits in front of it (AWS
WAF, CloudFront, Cloudflare...) looks the client's address up and passes the
country on in the header named by COUNTRY_HEADER; this module reads that. With
COUNTRY_HEADER unset nothing here does anything.

The header is only as trustworthy as the proxy that sets it: a client that can
reach Watchly directly could send any country it likes. The proxy must
overwrite the header, and the API must not be reachable around the proxy.

A country is also the *address's* country: someone on a VPN shows up where the
VPN ends.
"""

import re
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.core.config import settings

_CODE = re.compile(r"[A-Z]{2}")
#: What proxies send when the address is not in their database.
_UNKNOWN = {"XX", "ZZ"}

#: These stay reachable from anywhere, so a load balancer's health check is not
#: refused for its address.
_OPEN_PATHS = {"/health"}


def country_of(request: Request) -> str | None:
    """The two-letter country code (`NP`) the proxy reported for `request`, or
    None when COUNTRY_HEADER is unset, the header is missing, or its value is
    not a country (`XX`, `T1`, junk)."""
    if not settings.COUNTRY_HEADER:
        return None
    code = request.headers.get(settings.COUNTRY_HEADER, "").strip().upper()
    if _CODE.fullmatch(code) and code not in _UNKNOWN:
        return code
    return None


def is_blocked(country: str | None) -> bool:
    """Whether COUNTRY_ALLOW / COUNTRY_DENY keep `country` out. An unknown
    country is never blocked."""
    if country is None:
        return False
    if settings.COUNTRY_ALLOW:
        return country not in settings.COUNTRY_ALLOW
    return country in settings.COUNTRY_DENY


async def block_countries(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Middleware: answer 403 to a request from a country that is not allowed."""
    if (settings.COUNTRY_ALLOW or settings.COUNTRY_DENY) and request.url.path not in _OPEN_PATHS:
        country = country_of(request)
        if is_blocked(country):
            return JSONResponse(
                {"detail": "This service is not available in your country."},
                status_code=403,
            )
    return await call_next(request)
