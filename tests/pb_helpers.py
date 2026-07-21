"""Tiny protobuf wire builders for constructing test payloads."""

from __future__ import annotations


def varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def ld(field: int, body: bytes) -> bytes:
    """A length-delimited (wire type 2) protobuf field."""
    return bytes([(field << 3) | 2]) + varint(len(body)) + body


def vint(field: int, value: int) -> bytes:
    """A varint (wire type 0) protobuf field."""
    return bytes([(field << 3)]) + varint(value)
