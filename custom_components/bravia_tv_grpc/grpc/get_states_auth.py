"""HMAC signing for GetStatesWithAuth request auth_token tail."""

from __future__ import annotations

import hashlib
import hmac

from .wire import encode_varint, read_varint


def _hmac_key_bytes(hmac_key_hex: str) -> bytes:
    if len(hmac_key_hex) == 64:
        return bytes.fromhex(hmac_key_hex)
    return hmac_key_hex.encode("utf-8")[:32].ljust(32, b"\x00")


def extract_signing_preimage(request_bytes: bytes) -> bytes:
    """
    Return bytes signed for GetStatesWithAuth auth tail.

    Wire layout: ``field1 (0x0a + varint + preimage) + field2 (0x1220 + 32 B HMAC)``.
    Frida ``hmacApplyMac`` signs the inner ``preimage`` only (no outer tag/length).
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
    key = _hmac_key_bytes(hmac_key_hex)
    return hmac.new(key, preimage, hashlib.sha256).digest()


def sign_get_states_request_body(hmac_key_hex: str, request_bytes: bytes) -> bytes:
    """HMAC the GetStates request body (excluding auth field and outer envelope)."""
    return sign_get_states_auth_token(
        hmac_key_hex, extract_signing_preimage(request_bytes)
    )


def build_get_states_signing_preimage(
    field_list_block: bytes,
    *,
    session_random: bytes,
    session_id: str,
) -> bytes:
    """
    Preimage for full GetStatesWithAuth snapshot (Frida ``data_len=6521``).

    ``field_list_block`` is the nested path list (``0x0a`` + varint + paths).
    """
    session_id_bytes = session_id.encode("utf-8")
    embedded_data = (
        b"\x0a"
        + encode_varint(len(session_random))
        + session_random
        + b"\x1a"
        + encode_varint(len(session_id_bytes))
        + session_id_bytes
    )
    embedded_field = b"\x12" + encode_varint(len(embedded_data)) + embedded_data
    return field_list_block + embedded_field


def build_mutex_signing_preimage(
    path: str,
    *,
    session_random: bytes,
    session_id: str,
) -> bytes:
    """Preimage for mutex GetStatesWithAuth (Frida ``data_len=78``)."""
    path_bytes = path.encode("utf-8")
    depth2 = b"\x0a" + encode_varint(len(path_bytes)) + path_bytes
    depth1 = b"\x0a" + encode_varint(len(depth2)) + depth2
    session_id_bytes = session_id.encode("utf-8")
    embedded_data = (
        b"\x0a\x08"
        + session_random
        + b"\x1a"
        + encode_varint(len(session_id_bytes))
        + session_id_bytes
    )
    embedded_field = b"\x12" + encode_varint(len(embedded_data)) + embedded_data
    return depth1 + embedded_field
