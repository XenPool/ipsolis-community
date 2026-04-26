"""Password hashing — stdlib-only, no external deps.

Uses PBKDF2-HMAC-SHA256 at OWASP 2023's minimum recommended iteration
count (600 000). The hash is stored as a self-describing string:

    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

This format is parseable by any reasonable Python code and survives
algorithm upgrades (a future scrypt / argon2id migration just adds a
new prefix and ``verify_password`` dispatches by it).

Why not bcrypt / passlib / argon2? Avoids a build dependency on
binary wheels, keeps the API container slim, and PBKDF2-SHA256 at
600k rounds is well above current OWASP guidance for password
storage. When we move to argon2id later, the migration is a single
verify-then-rehash on next login.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

_PREFIX = "pbkdf2_sha256"
_ITERATIONS = 600_000
_SALT_BYTES = 16


def hash_password(plain: str) -> str:
    """Return a PBKDF2-SHA256 hash of ``plain`` with a fresh random salt."""
    if not plain:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt, _ITERATIONS,
    )
    return f"{_PREFIX}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time check ``plain`` against a stored hash. Never raises.

    Unrecognised / malformed hashes return False so a corrupt row in
    ``admin_users`` can't be mistaken for a valid one. ``hmac.compare_digest``
    avoids the timing leak that ``==`` on bytes would introduce.
    """
    if not plain or not hashed:
        return False
    try:
        scheme, iters_str, salt_hex, hash_hex = hashed.split("$", 3)
    except ValueError:
        return False
    if scheme != _PREFIX:
        return False
    try:
        iters = int(iters_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (TypeError, ValueError):
        return False
    if iters < 1 or not salt or not expected:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt, iters,
    )
    return hmac.compare_digest(candidate, expected)
