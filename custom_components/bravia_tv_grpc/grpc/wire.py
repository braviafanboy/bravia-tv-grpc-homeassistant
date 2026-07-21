"""Protobuf wire-format primitives shared across the grpc/ codecs.

The TV's authenticated requests/responses don't match the nominal .proto schema,
so they're built and parsed by hand. These are the low-level varint / field
helpers every codec module needs; keeping one copy avoids the several
near-identical reimplementations they previously each carried.
"""

from __future__ import annotations

from typing import Any


def encode_varint(value: int) -> bytes:
    """Encode an unsigned integer as a protobuf varint."""
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def encode_signed_varint(value: int) -> bytes:
    """Encode int32/int64 as a protobuf varint (sign-extended when negative)."""
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            break
    return bytes(out)


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint at ``pos``; return ``(value, next_pos)``.

    Best-effort: a truncated varint returns the partial value rather than
    raising (all inputs here come from the trusted device)."""
    value = shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return value, pos


def length_delimited(field: int, body: bytes) -> bytes:
    """Build a length-delimited (wire type 2) protobuf field."""
    return bytes([(field << 3) | 2]) + encode_varint(len(body)) + body


def decode_field(data: bytes, pos: int) -> tuple[tuple[int, int, Any] | None, int]:
    """Decode one protobuf field (wire types varint/0 and length-delimited/2).

    Returns ``((field_num, wire_type, value), next_pos)`` or ``(None, pos)`` for
    an unsupported/truncated field."""
    if pos >= len(data):
        return None, pos
    tag = data[pos]
    field_num = tag >> 3
    wire_type = tag & 0x7
    pos += 1
    if wire_type == 0:
        value, pos = read_varint(data, pos)
        return (field_num, wire_type, value), pos
    if wire_type == 2:
        length, pos = read_varint(data, pos)
        if pos + length > len(data):
            return None, pos
        value = data[pos : pos + length]
        pos += length
        return (field_num, wire_type, value), pos
    return None, pos


def parse_fields(raw: bytes) -> dict[int, list[bytes]]:
    """Shallow parse -> ``{field_number: [length-delimited values]}``.

    Varint fields are skipped; parsing stops at the first other wire type."""
    out: dict[int, list[bytes]] = {}
    pos = 0
    while pos < len(raw):
        tag = raw[pos]
        pos += 1
        wire_type = tag & 0x7
        field_num = tag >> 3
        if wire_type == 2:
            length, pos = read_varint(raw, pos)
            out.setdefault(field_num, []).append(raw[pos : pos + length])
            pos += length
        elif wire_type == 0:
            _, pos = read_varint(raw, pos)
        else:
            break
    return out
