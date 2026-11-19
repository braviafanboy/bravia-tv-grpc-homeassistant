"""HMAC signing for GetStatesWithAuth request auth_token tail."""

from __future__ import annotations

import hashlib
import hmac

from .keys import hmac_key_bytes
from .wire import read_varint


def extract_signing_preimage(request_bytes: bytes) -> bytes:
    """
    Return bytes signed for GetStatesWithAuth auth tail.

    Wire layout: ``field1 (0x0a + varint + preimage) + field2 (0x1220 + 32 B HMAC)``.
    The TV signs the inner ``preimage`` only (no outer tag/length).
    """
    marker = b"\x12\x20"
    idx = request_bytes.rfind(marker)
    if idx < 0:
        msg = "auth_token marker not found in GetStates request"
        raise ValueError(msg)
    body = request_bytes[:idx]
    if body and body[0] == 0x0A:
        _, i = read_varint(body, 1)
        return body[i:]
    return body


def sign_get_states_auth_token(hmac_key_hex: str, preimage: bytes) -> bytes:
    """Return 32-byte HMAC-SHA256 digest for GetStatesWithAuth auth tail."""
    key = hmac_key_bytes(hmac_key_hex)
    return hmac.new(key, preimage, hashlib.sha256).digest()


def sign_get_states_request_body(hmac_key_hex: str, request_bytes: bytes) -> bytes:
    """HMAC the GetStates request body (excluding auth field and outer envelope)."""
    return sign_get_states_auth_token(
        hmac_key_hex, extract_signing_preimage(request_bytes)
    )
