"""Build GetStatesWithAuth request bytes (wire format differs from .proto)."""

from __future__ import annotations

from .wire import encode_varint


def build_get_states_body(
    field_paths: list[str], *, session_random: bytes, session_id: str
) -> bytes:
    """The signed part of a GetStatesWithAuth request: the nested path list
    followed by the embedded session block.

    These exact bytes are what the TV verifies the HMAC over, and what
    :func:`wrap_signed_request` then sends, so the signed and the sent bytes
    cannot differ.
    """
    inner_parts = b""
    for path in field_paths:
        path_bytes = path.encode("utf-8")
        inner_parts += b"\x0a" + encode_varint(len(path_bytes)) + path_bytes

    nested_field = b"\x0a" + encode_varint(len(inner_parts)) + inner_parts

    # BRAVIA TVs return a 64-byte session_random. Emit the field length-prefixed
    # rather than assuming a fixed size.
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
    return nested_field + embedded_field


def wrap_signed_request(body: bytes, auth_token: bytes) -> bytes:
    """Wrap a signed body and its 32-byte HMAC in the request envelope.

    Top-level layout (6558 bytes with 177 paths on fw 001.454):
      field 1 (6521 B): the signed body
      field 2 (34 B): auth_token (32 B HMAC-SHA256)
    """
    if len(auth_token) != 32:
        msg = f"auth_token must be 32 bytes, got {len(auth_token)}"
        raise ValueError(msg)
    field_list_bytes = b"\x0a" + encode_varint(len(body)) + body
    auth_token_bytes = b"\x12" + encode_varint(len(auth_token)) + auth_token
    return field_list_bytes + auth_token_bytes


def build_get_states_with_auth_request(
    field_paths: list[str],
    *,
    session_random: bytes,
    session_id: str,
    auth_token: bytes,
) -> bytes:
    """Return the serialized GetStatesWithAuth request for ``auth_token``."""
    return wrap_signed_request(
        build_get_states_body(
            field_paths, session_random=session_random, session_id=session_id
        ),
        auth_token,
    )
