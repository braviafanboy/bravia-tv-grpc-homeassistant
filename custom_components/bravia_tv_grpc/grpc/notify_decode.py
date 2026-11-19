"""
Decode StartNotifyStates delta payloads (field path + value).

Notify updates arrive in ``StartNotifyStatesResponse.session_random`` — not
``states``. Wire format is a nested protobuf blob documented in grpc_test2 PoC.
"""

# ruff: noqa: PLR0911, PLR0912, PLR2004

from __future__ import annotations

from typing import Any

from .application_list import APPLICATION_LIST_PATH
from .wire import decode_field, read_varint


def _nested_varint(payload: bytes) -> int | None:
    field, _ = decode_field(payload, 0)
    if field and field[0] == 1 and field[1] == 0:
        return int(field[2])
    return None


def _nested_string(payload: bytes) -> str | None:
    field, _ = decode_field(payload, 0)
    if field and field[0] == 1 and field[1] == 2:
        try:
            return field[2].decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _maybe_signed_int(value: int) -> int:
    """Reinterpret protobuf int64 varints that arrived as unsigned."""
    if value >= 1 << 63:
        return value - (1 << 64)
    return value


def _nested_strings(payload: bytes) -> list[str]:
    """Every ``repeated string = 1`` of a submessage (an enum array)."""
    out: list[str] = []
    pos = 0
    while pos < len(payload):
        field, pos = decode_field(payload, pos)
        if not field:
            break
        if field[0] == 1 and field[1] == 2:
            out.append(field[2].decode("utf-8", errors="replace"))
    return out


def _nested_ints(payload: bytes) -> list[int]:
    """Every ``repeated int32 = 1`` of a submessage, packed or not."""
    out: list[int] = []
    pos = 0
    while pos < len(payload):
        field, pos = decode_field(payload, pos)
        if not field:
            break
        if field[0] != 1:
            continue
        if field[1] == 0:
            out.append(_maybe_signed_int(int(field[2])))
        elif field[1] == 2:  # packed
            packed, inner = field[2], 0
            while inner < len(packed):
                value, inner = read_varint(packed, inner)
                out.append(_maybe_signed_int(value))
    return out


def extract_array_value(fields: dict[int, tuple[int, Any]]) -> tuple[bool, Any]:
    """The TV's less common value kinds, shared by the push and GetStates
    decoders: ``(found, value)``.

    A state's value is one of seven kinds. Besides int (2), bool (3), enum (4)
    and any (5) the TV has ``enum_as_int`` (6: an int), ``enum_array`` (7: a
    list of strings, e.g. ``error_status``) and ``enum_as_int_array`` (8: a
    list of ints). An empty submessage is the type's zero value: 0 or ``[]``.
    """
    if 6 in fields:
        wire_type, raw = fields[6]
        if wire_type == 2 and isinstance(raw, bytes):
            nested = _nested_varint(raw) if raw else 0
            if nested is not None:
                return True, _maybe_signed_int(nested)
    if 7 in fields:
        wire_type, raw = fields[7]
        if wire_type == 2 and isinstance(raw, bytes):
            return True, _nested_strings(raw)
    if 8 in fields:
        wire_type, raw = fields[8]
        if wire_type == 2 and isinstance(raw, bytes):
            return True, _nested_ints(raw)
    return False, None


def _extract_value(fields: dict[int, tuple[int, Any]]) -> Any:
    if 2 in fields:
        wire_type, raw = fields[2]
        if wire_type == 0:
            return _maybe_signed_int(int(raw)) if isinstance(raw, int) else raw
        if wire_type == 2 and not raw:
            # Proto3 omitted-zero int: value present and 0 (e.g. brightness→0),
            # the int analogue of the empty-bool-is-False case below.
            return 0
        if wire_type == 2 and raw:
            nested = _nested_varint(raw)
            if nested is not None:
                return _maybe_signed_int(nested)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text and all(c.isprintable() or c.isspace() for c in text):
                return text
            return raw.hex()
    if 3 in fields:
        wire_type, raw = fields[3]
        if wire_type == 2:
            if not raw:
                return False
            nested = _nested_varint(raw)
            if nested is not None:
                return bool(nested)
    for key in (4, 5):
        if key in fields:
            wire_type, raw = fields[key]
            if wire_type == 2 and raw:
                text = _nested_string(raw)
                if text is not None:
                    return text
    found, value = extract_array_value(fields)
    if found:
        return value
    return None


def parse_notify_message(raw: bytes) -> tuple[str | None, Any]:
    """Decode a raw StartNotifyStates message (bytes, no generated proto).

    Normal deltas carry the payload in field 2. An installed-app change is pushed
    as a large AES-GCM blob in field 3 (a small field 3 is just the session_id),
    which breaks the generated deserializer — we surface it as a synthetic
    ``(application_list, None)`` trigger for the coordinator to re-read the list.
    """
    fields: dict[int, tuple[int, int, Any]] = {}
    pos = 0
    while pos < len(raw):
        field, pos = decode_field(raw, pos)
        if not field:
            break
        fields[field[0]] = field
    if 3 in fields and fields[3][1] == 2 and len(fields[3][2]) > 256:
        return APPLICATION_LIST_PATH, None
    if 2 in fields and fields[2][1] == 2:
        return decode_notify_delta(fields[2][2])
    return None, None


def decode_notify_delta(payload: bytes | str) -> tuple[str | None, Any]:
    """Return ``(field_path, value)`` from a notify ``session_random`` blob."""
    if isinstance(payload, str):
        payload = bytes.fromhex(payload)
    offset = 0
    outer, offset = decode_field(payload, offset)
    if not outer or outer[0] != 1 or outer[1] != 2:
        return None, None
    inner, _ = decode_field(outer[2], 0)
    if not inner or inner[0] != 1 or inner[1] != 2:
        return None, None
    fields: dict[int, tuple[int, Any]] = {}
    pos = 0
    while pos < len(inner[2]):
        field, pos = decode_field(inner[2], pos)
        if not field:
            break
        fields[field[0]] = (field[1], field[2])
    path = None
    if 1 in fields and fields[1][0] == 2:
        path = fields[1][1].decode("utf-8", errors="ignore")
    value = _extract_value(fields)
    return path, value
