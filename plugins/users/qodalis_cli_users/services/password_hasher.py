"""PBKDF2-HMAC-SHA256 password hashing.

Encoded format (shared by the node, python and .NET servers)::

    pbkdf2$100000$<saltB64>$<hashB64>

16-byte random salt, 32-byte derived key, constant-time comparison.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ITERATIONS = 100_000
SALT_BYTES = 16
KEY_BYTES = 32
PREFIX = "pbkdf2"


def _derive(password: str, salt: bytes, iterations: int, key_len: int = KEY_BYTES) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, key_len)


def hash_password(password: str) -> tuple[str, str]:
    """Hash ``password``; returns ``(encoded_hash, salt_b64)``."""
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, ITERATIONS)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    encoded = f"{PREFIX}${ITERATIONS}${salt_b64}${base64.b64encode(key).decode('ascii')}"
    return encoded, salt_b64


def verify_password(password: str, encoded: str, legacy_salt: str = "") -> bool:
    """Verify ``password`` against an encoded hash (constant-time).

    Hashes written by earlier plugin versions (hex ``sha256(salt + password)``
    with a separate salt) are still accepted so existing persisted stores keep
    working; :func:`needs_rehash` reports them so callers can upgrade them.
    """
    if not encoded:
        return False
    parts = encoded.split("$")
    if len(parts) == 4 and parts[0] == PREFIX:
        try:
            iterations = int(parts[1])
            salt = base64.b64decode(parts[2])
            expected = base64.b64decode(parts[3])
        except (ValueError, TypeError):
            return False
        if iterations <= 0 or not expected:
            return False
        actual = _derive(password, salt, iterations, len(expected))
        return hmac.compare_digest(actual, expected)
    # Legacy SHA-256 format
    legacy = hashlib.sha256((legacy_salt + password).encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy.encode("ascii"), encoded.encode("utf-8", "replace"))


def needs_rehash(encoded: str) -> bool:
    parts = encoded.split("$")
    return not (len(parts) == 4 and parts[0] == PREFIX and parts[1] == str(ITERATIONS))


# ---------------------------------------------------------------------------
# Async API — PBKDF2 runs off the event loop with a concurrency cap.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

#: At most this many PBKDF2 computations run at once.
MAX_CONCURRENT_HASHES = 4
_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_HASHES, thread_name_prefix="qcli-pbkdf2")
_dummy_hash: str | None = None


async def hash_password_async(password: str) -> tuple[str, str]:
    return await asyncio.get_running_loop().run_in_executor(_executor, hash_password, password)


async def verify_password_async(password: str, encoded: str, legacy_salt: str = "") -> bool:
    return await asyncio.get_running_loop().run_in_executor(
        _executor, verify_password, password, encoded, legacy_salt,
    )


async def dummy_verify_async(password: str) -> None:
    """Run a full PBKDF2 verification against a throwaway hash so unknown
    users take as long as known ones."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = (await hash_password_async(secrets.token_hex(16)))[0]
    await verify_password_async(password, _dummy_hash)
