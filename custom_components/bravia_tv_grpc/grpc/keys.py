"""The one place a stored Sony ``hmac_key`` becomes signing-key bytes."""

from __future__ import annotations


def hmac_key_bytes(hmac_key_hex: str) -> bytes:
    """Return the 32-byte HMAC key for a stored ``hmac_key``.

    Sony issues the key as 64 hex characters. Anything else is used as UTF-8,
    truncated or zero-padded to 32 bytes (kept from the original client for
    keys issued in another form). Raises ``ValueError`` for an empty key or 64
    characters that are not hex: both mean the stored key is unusable, which
    callers treat like a rejected key (fetch fresh ones) instead of signing
    with a key the TV can never accept."""
    if not hmac_key_hex:
        raise ValueError("hmac_key is empty")
    if len(hmac_key_hex) == 64:
        key = bytes.fromhex(hmac_key_hex)
        if len(key) != 32:  # fromhex skips whitespace
            raise ValueError("hmac_key is not 64 hex characters")
        return key
    return hmac_key_hex.encode("utf-8")[:32].ljust(32, b"\x00")
