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
