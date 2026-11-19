"""Parse GetStatesWithAuth response bytes into field-path values."""

from __future__ import annotations

from typing import Any

from .notify_decode import (
    _maybe_signed_int,
    _nested_string,
    _nested_varint,
    extract_array_value,
)
from .wire import decode_field, read_varint


def parse_get_states_response(raw: bytes) -> dict[str, Any]:
    """
    Decode GetStatesWithAuth response into ``{field_path: value}``.

    Wire layout (fw 001.454): top-level field 2 holds field 1, a concatenated
    stream of ``path + value`` entries (not notify-shaped deltas).
    """
    result: dict[str, Any] = {}
    offset = 0
    while offset < len(raw):
        field, offset = decode_field(raw, offset)
        if not field:
            break
        field_num, wire_type, payload = field
        if wire_type != 2 or not isinstance(payload, bytes):
            continue
        if field_num == 1:
            _parse_entries_stream(payload, result)
        else:
            _parse_states_blob(payload, result)
    return result


def _parse_states_blob(blob: bytes, out: dict[str, Any]) -> None:
    """Read the states out of the reply's signed wrapper.

    The wrapper is ``{serialized = 1, hmac = 2, key_id = 3}``: only field 1
    holds states. The other two are a MAC and a key name, and reading them as
    a state entry could (rarely) invent a path out of random MAC bytes."""
    pos = 0
    while pos < len(blob):
        field, pos = decode_field(blob, pos)
        if not field:
            break
        field_num, wire_type, payload = field
        if wire_type == 2 and isinstance(payload, bytes) and field_num == 1:
            _parse_entries_stream(payload, out)


def _parse_entries_stream(stream: bytes, out: dict[str, Any]) -> None:
    pos = 0
    while pos < len(stream):
        if stream[pos] != 0x0A:
            break
        path, value, next_pos = _parse_one_entry(stream, pos)
        if next_pos <= pos:
            break
        pos = next_pos
        if path:
            out[path] = value


def _parse_one_entry(data: bytes, pos: int = 0) -> tuple[str | None, Any, int]:
    """Parse the entry starting at ``pos``; return ``(path, value, next_pos)``.

    ``next_pos`` equal to ``pos`` means there is no complete entry there. The
    entry is read in place (by offset), so a long stream is not re-copied for
    every entry."""
    if pos >= len(data) or data[pos] != 0x0A:
        return None, None, pos
    entry_len, i = read_varint(data, pos + 1)
    if i + entry_len > len(data):
        return None, None, pos
    path, value = _parse_state_entry(data[i : i + entry_len])
    return path, value, i + entry_len


def _parse_state_entry(payload: bytes) -> tuple[str | None, Any]:
    path: str | None = None
    fields: dict[int, tuple[int, Any]] = {}
    pos = 0
    while pos < len(payload):
        field, pos = decode_field(payload, pos)
        if not field:
            break
        field_num, wire_type, field_payload = field
        if field_num == 1 and wire_type == 2 and isinstance(field_payload, bytes):
            try:
                path = field_payload.decode("utf-8")
            except UnicodeDecodeError:
                path = None
        else:
            fields[field_num] = (wire_type, field_payload)
    value = _extract_get_states_value(fields)
    return path, value


def _extract_get_states_value(fields: dict[int, tuple[int, Any]]) -> Any:
    """Extract a value from GetStates entry fields (int/bool/str/json)."""
    if 4 in fields:
        wire_type, raw = fields[4]
        if wire_type == 2 and isinstance(raw, bytes):
            text = _nested_string(raw)
            if text is not None:
                return text
    if 2 in fields:
        wire_type, raw = fields[2]
        if wire_type == 0:
            return _maybe_signed_int(raw)
        if wire_type == 2 and isinstance(raw, bytes):
            # An empty int submessage is proto3's omitted-zero: the value is
            # present and 0 (e.g. brightness or voice_zoom at 0), not unknown —
            # the int analogue of the empty-bool-is-False case below.
            if not raw:
                return 0
            nested = _nested_varint(raw)
            if nested is not None:
                return _maybe_signed_int(nested)
            text = _nested_string(raw)
            if text is not None:
                return text
    if 3 in fields:
        wire_type, raw = fields[3]
        if wire_type == 2 and isinstance(raw, bytes):
            # An empty bool submessage is proto3's omitted-zero: the value is
            # present and False (e.g. mute off), not unknown.
            if not raw:
                return False
            nested = _nested_varint(raw)
            if nested is not None:
                return bool(nested)
    if 5 in fields:
        wire_type, raw = fields[5]
        if wire_type == 2 and isinstance(raw, bytes):
            if not raw:
                return None
            text = _nested_string(raw)
            if text is not None:
                return text
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.hex()
            if text and all(c.isprintable() or c in "\n\r\t" for c in text):
                return text
            return raw.hex()
    found, value = extract_array_value(fields)
    if found:
        return value
    return None


# _maybe_signed_int lives in notify_decode (shared with GetStates).
