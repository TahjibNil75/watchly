"""Password hashing and the random secrets in app/core/security.py. No database."""

import re

import pytest

from app.core.security import (
    generate_hash_password,
    generate_temporary_password,
    hash_token,
    new_link_token,
    verify_password,
)


def test_a_hashed_password_verifies():
    password_hash = generate_hash_password("Correct-Horse-9")

    assert password_hash != "Correct-Horse-9"
    assert verify_password("Correct-Horse-9", password_hash)


@pytest.mark.parametrize("attempt", ["correct-horse-9", "Correct-Horse-", "", " Correct-Horse-9"])
def test_any_other_password_does_not_verify(attempt):
    assert not verify_password(attempt, generate_hash_password("Correct-Horse-9"))


def test_hashes_are_salted():
    first, second = generate_hash_password("same"), generate_hash_password("same")

    assert first != second
    assert verify_password("same", first) and verify_password("same", second)


def test_passwords_past_bcrypts_72_bytes_hash_and_verify_instead_of_raising():
    long = "x" * 200

    assert verify_password(long, generate_hash_password(long))


def test_only_the_first_72_bytes_of_a_password_count():
    # bcrypt's own limit, made explicit: the rest of a longer password is ignored.
    password_hash = generate_hash_password("a" * 72 + "tail-one")

    assert verify_password("a" * 72 + "tail-two", password_hash)


@pytest.mark.parametrize("stored", ["", "not-a-bcrypt-hash", "$2b$04$tooshort"])
def test_a_malformed_stored_hash_fails_verification_rather_than_raising(stored):
    assert verify_password("anything", stored) is False


def test_temporary_passwords_are_three_groups_of_four_unambiguous_characters():
    for _ in range(200):
        password = generate_temporary_password()

        assert re.fullmatch(r"[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}", password)
        # Read off an email and typed in, so none of the look-alikes.
        assert not set(password) & set("0O1lI")


def test_temporary_passwords_do_not_repeat():
    assert len({generate_temporary_password() for _ in range(500)}) == 500


def test_link_tokens_are_url_safe_and_unique():
    tokens = {new_link_token() for _ in range(500)}

    assert len(tokens) == 500
    for token in tokens:
        # 32 random bytes, base64url without padding.
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)


def test_token_hash_is_a_stable_sha256_hex_digest():
    token = new_link_token()

    digest = hash_token(token)

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == hash_token(token)
    assert digest != hash_token(new_link_token())
    assert token not in digest
