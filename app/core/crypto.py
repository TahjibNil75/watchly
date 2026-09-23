"""Symmetric encryption for secrets stored in the database.

Slack bot tokens are supplied through the API and have to be kept, but a
plaintext `xoxb-...` in a table is a token in every database dump, replica and
backup. They are encrypted here instead, and never returned by the API.

The key comes from `SLACK_TOKEN_ENCRYPTION_KEY` if set, otherwise it is derived
from `SECRET_KEY`. **Rotating either one makes existing ciphertexts
unreadable** — `decrypt_secret` returns None rather than raising, the affected
integration simply stops working, and the token has to be re-entered.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)


def _fernet() -> Fernet:
    material = settings.SLACK_TOKEN_ENCRYPTION_KEY or settings.SECRET_KEY
    # Fernet wants 32 url-safe base64 bytes; hash whatever we were given so any
    # length of configured secret works.
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str | None) -> str | None:
    """Return the plaintext, or None if it cannot be read.

    Never raises: an unreadable token must not take down the monitoring loop.
    """
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        logger.error(
            "Could not decrypt a stored secret — SECRET_KEY or "
            "SLACK_TOKEN_ENCRYPTION_KEY has probably changed. Re-enter it."
        )
        return None


def mask_secret(plaintext: str | None, keep: int = 4) -> str | None:
    """`xoxb-1234-5678-abcdefgh` -> `xoxb-…efgh`, for showing what is stored."""
    if not plaintext:
        return None
    prefix = plaintext[:5] if plaintext.startswith("xox") else ""
    return f"{prefix}…{plaintext[-keep:]}" if len(plaintext) > keep else "…"
