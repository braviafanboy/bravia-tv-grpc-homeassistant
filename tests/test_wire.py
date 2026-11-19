"""Pure tests for the wire primitives, the state decoders' rarer value kinds,
the capability schema reader and the request signing (no Home Assistant)."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import time

from btvgrpc import keys, wire
from btvgrpc.exec_command_request import (
    build_exec_command_signing_preimage,
    build_exec_command_with_auth_request,
    parse_exec_response,
    sign_exec_auth_token,
)
from btvgrpc.get_capabilities_response import (
    capability_index_from_json,
    decode_capabilities_json_text,
    parse_capability_index,
    paths_for_safe_get_states,
)
from btvgrpc.get_states_auth import (
    sign_get_states_auth_token,
    sign_get_states_request_body,
)
from btvgrpc.get_states_request import (
    build_get_states_body,
    build_get_states_with_auth_request,
    wrap_signed_request,
)
from btvgrpc.get_states_response import parse_get_states_response
from btvgrpc.notify_decode import parse_notify_message
from pb_helpers import ld, varint, vint
import pytest

_CAPS = json.loads(
    (
        Path(__file__).resolve().parents[1] / "schema" / "grpc_capabilities.json"
    ).read_text()
)
KEY = "ab" * 32


# -- varints -------------------------------------------------------------------


def test_varint_roundtrip_and_signed():
    for value in (0, 1, 127, 128, 300, 2**31 - 1, 2**63 - 1):
        encoded = wire.encode_varint(value)
        assert wire.read_varint(encoded, 0) == (value, len(encoded))
    # int32 negatives travel sign-extended to 64 bits (ten bytes).
    assert len(wire.encode_signed_varint(-1)) == 10
    assert wire.encode_signed_varint(5) == b"\x05"


def test_varint_stops_after_ten_bytes():
    """A run of continuation bytes used to be read as one ever-growing
    integer: a 200 KB frame cost seconds. Ten bytes is the longest a varint
    can be, so reading stops there."""
    data = b"\xff" * 200_000
    value, pos = wire.read_varint(data, 0)
    assert pos == 10 and value < 2**70
    start = time.perf_counter()
    assert parse_notify_message(b"\x08" + data) == (None, None)
    assert parse_get_states_response(b"\x08" + data) == {}
    assert time.perf_counter() - start < 0.5


def test_truncated_input_never_raises():
    frame = ld(2, ld(1, ld(1, ld(1, b"volume") + ld(2, vint(1, 18)))))
    for cut in range(len(frame)):
        parse_notify_message(frame[:cut])
        parse_get_states_response(frame[:cut])
    assert wire.decode_field(b"", 0) == (None, 0)
    assert wire.parse_fields(b"\x0a\x05ab") == {1: [b"ab"]}


def test_many_entries_parse_in_linear_time():
    """The entry stream is read by offset, not re-copied per entry."""
    entry = ld(1, ld(1, b"p") + ld(2, vint(1, 1)))
    reply = ld(2, ld(1, entry * 60_000))
    start = time.perf_counter()
    assert parse_get_states_response(reply) == {"p": 1}
    assert time.perf_counter() - start < 2.0


# -- the TV's rarer value kinds ---------------------------------------------------


def _reply(entries: list[tuple[str, bytes]]) -> bytes:
    stream = b"".join(ld(1, ld(1, path.encode()) + value) for path, value in entries)
    # {with_hmac = 2 {serialized = 1 {states...}, hmac = 2, key_id = 3}}
    return ld(2, ld(1, stream) + ld(2, b"\x00" * 32) + ld(3, b"key"))


def _push(path: str, value: bytes) -> bytes:
    return ld(2, ld(1, ld(1, ld(1, path.encode()) + value)))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # enum_array (7): a list of strings. error_status is one.
        (
            ld(7, ld(1, b"protect_sw") + ld(1, b"protect_rl")),
            ["protect_sw", "protect_rl"],
        ),
        (ld(7, ld(1, b"no_error")), ["no_error"]),
        (ld(7, b""), []),
        # enum_as_int (6): an int; an empty submessage is zero.
        (ld(6, vint(1, 3)), 3),
        (ld(6, b""), 0),
        # enum_as_int_array (8): ints, packed or one per field.
        (ld(8, vint(1, 1) + vint(1, 2)), [1, 2]),
        (ld(8, ld(1, varint(4) + varint(5))), [4, 5]),
        (ld(8, b""), []),
    ],
)
def test_array_values_decode_on_both_channels(value, expected):
    """They used to decode to None, so error_status could never report a
    fault and the *.prerequisites / *.available_values states were unreadable."""
    assert parse_get_states_response(_reply([("error_status", value)])) == {
        "error_status": expected
    }
    assert parse_notify_message(_push("error_status", value)) == (
        "error_status",
        expected,
    )


def test_the_usual_kinds_still_decode():
    entries = [
        ("volume", ld(2, vint(1, 18))),
        ("power", ld(3, vint(1, 1))),
        ("mute", ld(3, b"")),
        ("display_setting.picture_mode", ld(4, ld(1, b"professional"))),
        ("system_setting.application", ld(5, ld(1, b"com.netflix.ninja"))),
    ]
    assert parse_get_states_response(_reply(entries)) == {
        "volume": 18,
        "power": True,
        "mute": False,
        "display_setting.picture_mode": "professional",
        "system_setting.application": "com.netflix.ninja",
    }


def test_reply_signature_fields_are_not_read_as_states():
    """The wrapper's MAC and key name are not states: random MAC bytes that
    happen to look like an entry must not invent a path."""
    fake_entry = ld(1, b"phantom") + ld(2, vint(1, 1))
    stream = ld(1, ld(1, b"volume") + ld(2, vint(1, 7)))
    reply = ld(2, ld(1, stream) + ld(2, fake_entry) + ld(3, fake_entry))
    assert parse_get_states_response(reply) == {"volume": 7}


# -- the capability schema ---------------------------------------------------------


def _caps_reply(text: bytes) -> bytes:
    return ld(1, ld(1, text))


def test_schema_from_the_committed_capture():
    text = json.dumps(_CAPS)
    raw = _caps_reply(text.encode())
    assert decode_capabilities_json_text(raw) == text
    index = parse_capability_index(raw)
    assert len(index) == 118
    assert index["power"].type == "bool"
    assert index["error_status"].type == "enum-array"
    volume = index["volume"]
    assert volume.type == "int" and volume.min == 0 and volume.max == 100
    modes = index["display_setting.picture_mode"]
    assert modes.type == "enum" and "professional" in modes.values
    safe = paths_for_safe_get_states(text)
    assert "power" in safe
    # Nonce-gated: including it would fail the whole bulk read.
    assert "system_setting.application_list" not in safe
    assert len(safe) == 104


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x0a\x03\x0a\x41\xff",  # invalid UTF-8 where the JSON should be
        _caps_reply(b"not json"),
        _caps_reply(b"[]"),
        _caps_reply(b'{"capabilities": 5}'),
        _caps_reply(b'{"capabilities":[{"name":"a","props":{"min":1e999}}]}'),
        _caps_reply(b"[" * 200_000),
        ld(2, b"binary arm"),
    ],
)
def test_schema_reader_never_raises(raw):
    """Whatever arrives, the reader answers "no schema" or a schema; an
    exception here used to end setup for good."""
    index = parse_capability_index(raw)
    assert index is None or isinstance(index, dict)
    assert paths_for_safe_get_states(decode_capabilities_json_text(raw)) is not None


def test_schema_ignores_malformed_entries():
    index = capability_index_from_json(
        {
            "capabilities": [
                5,
                {"type": "int"},
                {"name": "", "type": "int"},
                {"name": "ok", "type": "int", "props": {"min": "1", "max": None}},
                {"name": "e", "type": "enum", "props": {"values": ["a", "", 3, " b "]}},
            ]
        }
    )
    assert set(index) == {"ok", "e"}
    assert index["ok"].min == 1 and index["ok"].max is None
    assert index["e"].values == ("a", "b")


# -- signing ---------------------------------------------------------------------------


def test_hmac_key_bytes():
    assert keys.hmac_key_bytes(KEY) == bytes.fromhex(KEY)
    # Not hex-shaped: used as text, padded to 32 bytes.
    assert keys.hmac_key_bytes("short") == b"short".ljust(32, b"\x00")
    for bad in ("", "zz" * 32, "ab" * 31 + " a"):
        with pytest.raises(ValueError):
            keys.hmac_key_bytes(bad)


def test_get_states_request_signs_the_bytes_it_sends():
    paths = ["power", "volume", "display_setting.picture_mode"]
    session_random, session_id = bytes(range(64)), "key-1"
    body = build_get_states_body(
        paths, session_random=session_random, session_id=session_id
    )
    token = sign_get_states_auth_token(KEY, body)
    assert token == hmac.new(bytes.fromhex(KEY), body, hashlib.sha256).digest()
    request = wrap_signed_request(body, token)
    # Field 1 is exactly the signed body; field 2 the token.
    fields = wire.parse_fields(request)
    assert fields[1] == [body] and fields[2] == [token]
    for path in paths:
        assert path.encode() in body
    assert session_random in body and session_id.encode() in body
    # The older two-pass route (still used by the pairing tool) agrees.
    placeholder = build_get_states_with_auth_request(
        paths,
        session_random=session_random,
        session_id=session_id,
        auth_token=b"\x00" * 32,
    )
    assert sign_get_states_request_body(KEY, placeholder) == token
    with pytest.raises(ValueError):
        wrap_signed_request(body, b"short")


@pytest.mark.parametrize(
    ("kwargs", "marker"),
    [
        ({"int_value": 18}, b"\x22\x02\x08\x12"),
        ({"int_value": -1}, b"\x22\x0b\x08" + b"\xff" * 9 + b"\x01"),
        ({"bool_value": True}, b"\x2a\x02\x08\x01"),
        ({"bool_value": False}, b"\x2a\x02\x08\x00"),
        ({"string_value": "cinema"}, b"\x32\x08\x0a\x06cinema"),
        ({"any_value": "com.netflix.ninja"}, b"\x3a\x13\x0a\x11com.netflix.ninja"),
        ({"string_value": "Wohnzimmer é"}, "Wohnzimmer é".encode()),
    ],
)
def test_exec_request_encodes_each_value_kind_and_signs_it(kwargs, marker):
    session = {"session_random": b"r" * 64, "session_id": "key-1"}
    preimage = build_exec_command_signing_preimage("volume", **session, **kwargs)
    assert marker in preimage and b"volume" in preimage
    token = sign_exec_auth_token(KEY, "volume", **session, **kwargs)
    assert token == hmac.new(bytes.fromhex(KEY), preimage, hashlib.sha256).digest()
    request = build_exec_command_with_auth_request(
        "volume", auth_token=token, **session, **kwargs
    )
    outer = wire.parse_fields(request)[1][0]
    inner = wire.parse_fields(outer)
    assert inner[1] == [preimage] and inner[2] == [token]


def test_exec_request_needs_exactly_one_value():
    session = {"session_random": b"r" * 64, "session_id": "k"}
    with pytest.raises(ValueError):
        build_exec_command_signing_preimage("volume", **session)
    with pytest.raises(ValueError):
        build_exec_command_signing_preimage(
            "volume", int_value=1, bool_value=True, **session
        )
    assert parse_exec_response(b"\x08\x01") is True
    assert parse_exec_response(b"") is False
