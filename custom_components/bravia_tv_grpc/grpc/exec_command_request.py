"""Build ExecCommandWithAuth request bytes (wire format differs from .proto)."""

from __future__ import annotations

import hashlib
import hmac

from .wire import encode_signed_varint, encode_varint


def _embedded_session_block(session_random: bytes, session_id: str) -> bytes:
    # session_random is 64 bytes on the TV; length-prefix it rather than
    # assuming a fixed size.
    session_id_bytes = session_id.encode("utf-8")
    embedded_data = (
        b"\x0a"
        + encode_varint(len(session_random))
        + session_random
        + b"\x1a"
        + encode_varint(len(session_id_bytes))
        + session_id_bytes
    )
    return b"\x12" + encode_varint(len(embedded_data)) + embedded_data


def _value_suffix(
    *,
    int_value: int | None,
    bool_value: bool | None,
    string_value: str | None,
    any_value: str | None = None,
) -> bytes:
    if int_value is not None:
        inner = b"\x08" + encode_signed_varint(int_value)
        return b"\x22" + encode_varint(len(inner)) + inner
    if bool_value is not None:
        inner = b"\x08" + (b"\x01" if bool_value else b"\x00")
        return b"\x2a" + encode_varint(len(inner)) + inner
    if string_value is not None:
        string_bytes = string_value.encode("utf-8")
        inner = b"\x0a" + encode_varint(len(string_bytes)) + string_bytes
        return b"\x32" + encode_varint(len(inner)) + inner
    if any_value is not None:
        # any-typed fields (e.g. system_setting.application/input) take the
        # value in field 7 (0x3a) as a nested string, verified live via the
        # non-visual system_setting.friendly_name field. Field 6 (string_value)
        # is silently ignored for these; field 7 returns 0801 on success.
        any_bytes = any_value.encode("utf-8")
        inner = b"\x0a" + encode_varint(len(any_bytes)) + any_bytes
        return b"\x3a" + encode_varint(len(inner)) + inner
    msg = "exactly one of int_value, bool_value, string_value, or any_value is required"
    raise ValueError(msg)


def build_exec_command_signing_preimage(
    command_path: str,
    *,
    session_random: bytes,
    session_id: str,
    int_value: int | None = None,
    bool_value: bool | None = None,
    string_value: str | None = None,
    any_value: str | None = None,
) -> bytes:
    """
    Return the 68-byte (typical) inner exec body signed for ``auth_token``.

    Frida ``hmacApplyMac`` signs ``HMAC-SHA256(hmac_key, preimage)`` where
    preimage is the nested command + embedded session block (no outer wrappers).
    """
    value_kwargs = (
        int_value is not None,
        bool_value is not None,
        string_value is not None,
        any_value is not None,
    )
    if sum(value_kwargs) != 1:
        msg = (
            "exactly one of int_value, bool_value, string_value, or any_value "
            "is required"
        )
        raise ValueError(msg)

    path_bytes = command_path.encode("utf-8")
    depth3 = (
        b"\x0a"
        + encode_varint(len(path_bytes))
        + path_bytes
        + b"\x10\x01"
        + _value_suffix(
            int_value=int_value,
            bool_value=bool_value,
            string_value=string_value,
            any_value=any_value,
        )
    )
    depth2 = b"\x0a" + encode_varint(len(depth3)) + depth3
    depth1 = b"\x0a" + encode_varint(len(depth2)) + depth2
    return depth1 + _embedded_session_block(session_random, session_id)


def sign_exec_auth_token(
    hmac_key_hex: str,
    command_path: str,
    *,
    session_random: bytes,
    session_id: str,
    int_value: int | None = None,
    bool_value: bool | None = None,
    string_value: str | None = None,
    any_value: str | None = None,
) -> bytes:
    """Compute rolling ExecCommandWithAuth auth_token from Sony Seeds hmac_key."""
    try:
        key_bytes = (
            bytes.fromhex(hmac_key_hex)
            if len(hmac_key_hex) == 64
            else hmac_key_hex.encode("utf-8")[:32].ljust(32, b"\x00")
        )
    except ValueError as exc:
        msg = "hmac_key_hex must be valid hex"
        raise ValueError(msg) from exc
    if len(key_bytes) != 32:
        msg = f"hmac_key must be 32 bytes, got {len(key_bytes)}"
        raise ValueError(msg)

    preimage = build_exec_command_signing_preimage(
        command_path,
        session_random=session_random,
        session_id=session_id,
        int_value=int_value,
        bool_value=bool_value,
        string_value=string_value,
        any_value=any_value,
    )
    return hmac.new(key_bytes, preimage, hashlib.sha256).digest()


def build_exec_command_with_auth_request(
    command_path: str,
    *,
    session_random: bytes,
    session_id: str,
    auth_token: bytes,
    int_value: int | None = None,
    bool_value: bool | None = None,
    string_value: str | None = None,
    any_value: str | None = None,
) -> bytes:
    """
    Return serialized ExecCommandWithAuth request matching BRAVIA Connect captures.

    Nested layout (three ``0a`` path wraps + embedded session + trailing auth token):
      field 1: command block (path + fixed ``10 01`` + value variant + session embed)
      field 2: auth_token (32 B)
    """
    if len(auth_token) != 32:
        msg = f"auth_token must be 32 bytes, got {len(auth_token)}"
        raise ValueError(msg)

    inner_cmd = build_exec_command_signing_preimage(
        command_path,
        session_random=session_random,
        session_id=session_id,
        int_value=int_value,
        bool_value=bool_value,
        string_value=string_value,
        any_value=any_value,
    )
    cmd_block = b"\x0a" + encode_varint(len(inner_cmd)) + inner_cmd
    auth_token_bytes = b"\x12" + encode_varint(len(auth_token)) + auth_token
    outer = cmd_block + auth_token_bytes
    return b"\x0a" + encode_varint(len(outer)) + outer


def parse_exec_response(raw: bytes) -> bool:
    """Return True when ExecCommandWithAuth response body is ``08 01``."""
    return raw == b"\x08\x01"
