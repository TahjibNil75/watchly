"""Access-token JWTs (app/utils/jwt.py). No database."""

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.utils.jwt import TokenError, create_access_token, decode_access_token


def _encode(claims: dict, key: str | None = None, algorithm: str = "HS256") -> str:
    return jwt.encode(claims, key if key is not None else settings.SECRET_KEY, algorithm=algorithm)


def _claims(**overrides) -> dict:
    return {
        "sub": "7",
        "type": "access",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        **overrides,
    }


def test_round_trip_keeps_subject_type_and_extra_claims():
    token = create_access_token("7", {"username": "alice", "role": "Viewer", "sv": 3})

    claims = decode_access_token(token)

    assert claims["sub"] == "7"
    assert claims["type"] == "access"
    assert claims["username"] == "alice"
    assert claims["sv"] == 3
    assert claims["jti"]


def test_lifetime_defaults_to_access_token_expire_minutes():
    claims = decode_access_token(create_access_token("7"))

    assert claims["exp"] - claims["iat"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


def test_every_token_gets_its_own_id():
    assert decode_access_token(create_access_token("7"))["jti"] != decode_access_token(
        create_access_token("7")
    )["jti"]


def test_extra_claims_cannot_override_subject_or_type():
    token = create_access_token("7", {"sub": "1", "type": "refresh"})

    claims = decode_access_token(token)

    assert claims["sub"] == "7"
    assert claims["type"] == "access"


def test_expired_token_is_refused():
    token = create_access_token("7", expires_delta=timedelta(seconds=-1))

    with pytest.raises(TokenError, match="expired"):
        decode_access_token(token)


def test_token_signed_with_another_key_is_refused():
    token = _encode(_claims(), key="another-secret-key-0123456789abcdef0123456789")

    with pytest.raises(TokenError):
        decode_access_token(token)


def test_unsigned_token_is_refused():
    token = jwt.encode(_claims(), None, algorithm="none")

    with pytest.raises(TokenError):
        decode_access_token(token)


def test_tampered_payload_is_refused():
    header, _, signature = create_access_token("7", {"role": "Viewer"}).split(".")
    forged = base64.urlsafe_b64encode(
        json.dumps(
            {"sub": "1", "type": "access", "role": "Admin", "exp": 4_102_444_800}
        ).encode()
    ).rstrip(b"=").decode()

    with pytest.raises(TokenError):
        decode_access_token(f"{header}.{forged}.{signature}")


def test_a_refresh_type_token_is_not_an_access_token():
    # Signed with the right key: e.g. a refresh JWT from before refresh tokens
    # moved server-side.
    token = _encode(_claims(type="refresh"))

    with pytest.raises(TokenError, match="Expected a access token"):
        decode_access_token(token)


@pytest.mark.parametrize("missing", ["sub", "type", "exp"])
def test_token_missing_a_required_claim_is_refused(missing):
    claims = _claims()
    del claims[missing]

    with pytest.raises(TokenError):
        decode_access_token(_encode(claims))


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c"])
def test_garbage_is_refused(token):
    with pytest.raises(TokenError):
        decode_access_token(token)
