import hashlib
import secrets

import bcrypt

# bcrypt only consumes the first 72 bytes of the password and raises on longer
# input, so normalise before hashing *and* before verifying.
BCRYPT_MAX_BYTES = 72


def _encode(password: str) -> bytes:
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def generate_hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt and return it as a string."""
    return bcrypt.hashpw(_encode(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(_encode(plain_password), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed / non-bcrypt hash in the column.
        return False


#: No 0/O or 1/l/I: a temporary password is read off an email and typed in.
_TEMP_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_temporary_password() -> str:
    """Twelve random characters in three groups, e.g. `Kp7x-Qm3v-Tz9w`: about
    69 bits, and easy to copy by eye."""
    return "-".join(
        "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(4)) for _ in range(3)
    )


def new_link_token() -> str:
    """A token for an emailed link: 256 random bits, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """The digest stored in place of an emailed token, so a database read cannot
    be turned into a working link. The token is 256 random bits, so a fast hash
    is enough — there is nothing to brute-force."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
