"""Exception handlers registered on the FastAPI app."""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# FastAPI's default 422 body echoes the offending input back to the client,
# which for a signup payload means the plaintext password. Redact these keys.
SENSITIVE_FIELDS = frozenset(
    {"password", "confirm_password", "current_password", "new_password", "token"}
)
REDACTED = "***"

logger = logging.getLogger(__name__)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if key in SENSITIVE_FIELDS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Where and why only: the input itself may be a password.
    logger.warning(
        "Rejected %s %s: %s",
        request.method,
        request.url.path,
        "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg')}"
            for error in exc.errors()
        ),
    )
    errors = []
    for error in exc.errors():
        cleaned = dict(error)
        cleaned.pop("url", None)
        if "input" in cleaned:
            loc = cleaned.get("loc") or ()
            cleaned["input"] = (
                REDACTED if loc and loc[-1] in SENSITIVE_FIELDS
                else redact(cleaned["input"])
            )
        errors.append(cleaned)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder({"detail": errors}),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
