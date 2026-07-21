"""Unit tests for the GetStatesWithAuth response parser."""

from __future__ import annotations

from btvgrpc.get_states_response import parse_get_states_response
from pb_helpers import ld, vint


def _response(entries: list[tuple[str, bytes]]) -> bytes:
    """Build a GetStates response: field2 -> field1 -> stream of path+value entries."""
    stream = b"".join(ld(1, ld(1, path.encode()) + value) for path, value in entries)
    return ld(2, ld(1, stream))


def test_int_value():
    raw = _response([("volume", ld(2, vint(1, 18)))])
    assert parse_get_states_response(raw) == {"volume": 18}


def test_omitted_zero_int_is_zero_not_none():
    # Brightness at 0 arrives as an empty field-2 submessage (proto3 omitted-zero).
    raw = _response([("display_setting.brightness", ld(2, b""))])
    assert parse_get_states_response(raw) == {"display_setting.brightness": 0}


def test_bool_true_and_omitted_false():
    raw = _response([("power", ld(3, vint(1, 1))), ("mute", ld(3, b""))])
    assert parse_get_states_response(raw) == {"power": True, "mute": False}


def test_string_value():
    raw = _response([("display_setting.picture_mode", ld(4, ld(1, b"professional")))])
    assert parse_get_states_response(raw) == {
        "display_setting.picture_mode": "professional"
    }


def test_multiple_entries():
    raw = _response(
        [
            ("volume", ld(2, vint(1, 5))),
            ("display_setting.brightness", ld(2, b"")),
            ("power", ld(3, vint(1, 1))),
        ]
    )
    assert parse_get_states_response(raw) == {
        "volume": 5,
        "display_setting.brightness": 0,
        "power": True,
    }
