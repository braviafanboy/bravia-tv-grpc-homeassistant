"""Build GetNonce request bytes (proto-shaped)."""

from __future__ import annotations

from .wire import encode_varint


def build_get_nonce_request(session_id: str) -> bytes:
    """Return serialized GetNonce request: field 1 = session_id string."""
    sid = session_id.encode("utf-8")
    return b"\x0a" + encode_varint(len(sid)) + sid
