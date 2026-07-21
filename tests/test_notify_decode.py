"""Unit tests for the StartNotifyStates delta decoder."""

from __future__ import annotations

from btvgrpc import notify_decode as nd
from btvgrpc.application_list import APPLICATION_LIST_PATH
from pb_helpers import ld, vint


def _delta(path: str, value_field: bytes) -> bytes:
    """A notify delta payload: field1 -> field1 -> {field1=path, <value>}."""
    inner = ld(1, path.encode()) + value_field
    return ld(1, ld(1, inner))


def test_int_value():
    payload = _delta("volume", ld(2, vint(1, 18)))
    assert nd.decode_notify_delta(payload) == ("volume", 18)


def test_omitted_zero_int_is_zero_not_none():
    # An int at 0 is proto3-omitted: field 2 present but empty.
    payload = _delta("display_setting.brightness", ld(2, b""))
    assert nd.decode_notify_delta(payload) == ("display_setting.brightness", 0)


def test_bool_true_and_omitted_false():
    assert nd.decode_notify_delta(_delta("power", ld(3, vint(1, 1)))) == ("power", True)
    assert nd.decode_notify_delta(_delta("mute", ld(3, b""))) == ("mute", False)


def test_string_value():
    value = ld(4, ld(1, b"video"))
    assert nd.decode_notify_delta(
        _delta("display_and_sound_setting.content_mode", value)
    ) == ("display_and_sound_setting.content_mode", "video")


def test_maybe_signed_int_roundtrip():
    assert nd._maybe_signed_int(0) == 0
    assert nd._maybe_signed_int(6) == 6
    # A negative int64 arriving as an unsigned varint.
    assert nd._maybe_signed_int((1 << 64) - 6) == -6


def test_parse_notify_message_dispatches_normal_delta():
    raw = ld(2, _delta("volume", ld(2, vint(1, 7))))
    assert nd.parse_notify_message(raw) == ("volume", 7)


def test_parse_notify_message_large_field3_is_app_list_trigger():
    raw = ld(3, b"\x00" * 300)  # big AES-GCM blob = installed-app-list change
    assert nd.parse_notify_message(raw) == (APPLICATION_LIST_PATH, None)


def test_parse_notify_message_small_field3_ignored():
    raw = ld(3, b"session-id")  # small field 3 is just the session id
    assert nd.parse_notify_message(raw) == (None, None)
