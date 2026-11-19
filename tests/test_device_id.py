"""The device unique id in a ``_sonysmarthome._tcp`` name, whole or cut short."""

from __future__ import annotations

from btvgrpc.device_id import parse_instance_name, same_device
import pytest

UID = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
SUFFIX = "._sonysmarthome._tcp.local."


def test_whole_id():
    assert parse_instance_name(f"BRAVIA 8 II-{UID}{SUFFIX}") == ("BRAVIA 8 II", UID)
    # Without the service type, in upper case, with a hyphen in the name, and
    # with the suffix a responder adds when two devices share a name.
    assert parse_instance_name(f"BRAVIA 8 II-{UID}") == ("BRAVIA 8 II", UID)
    assert parse_instance_name(f"Wohn-Zimmer TV-{UID.upper()} (2){SUFFIX}") == (
        "Wohn-Zimmer TV",
        UID,
    )


def test_a_name_cut_short_keeps_the_beginning_of_the_id():
    # As seen on a soundbar with a 20-character name: 60 characters in all,
    # so the id lost its last one.
    name = f"BRAVIA Theatre Bar 7-{UID[:39]}"
    assert len(name) == 60
    assert parse_instance_name(name + SUFFIX) == ("BRAVIA Theatre Bar 7", UID[:39])
    # A longer name leaves less; a hyphen in the name does not confuse it.
    name = f"Wohn-Zimmer Fernseher an der Wand-{UID[:26]}"
    assert len(name) == 60
    assert parse_instance_name(name + SUFFIX) == (
        "Wohn-Zimmer Fernseher an der Wand",
        UID[:26],
    )
    # Nor does a name that itself ends in something like an id.
    assert parse_instance_name(f"Den-cafe0001-{UID[:20]}") == (
        "Den-cafe0001",
        UID[:20],
    )
    # One DNS label holds 63 bytes at most.
    name = f"Living Room BRAVIA 8 II-{UID[:39]}"
    assert len(name) == 63
    assert parse_instance_name(name)[1] == UID[:39]


def test_too_little_left_of_the_id_is_no_id():
    name = "A television with a name much longer than fits-" + UID[:7]
    assert parse_instance_name(name + SUFFIX) == (name, None)


@pytest.mark.parametrize(
    "name",
    ["Kitchen TV", "HT-A7000", "KD-55X85J", "Bedroom-2024", "BRAVIA-", "-"],
)
def test_a_name_without_an_id(name):
    assert parse_instance_name(name + SUFFIX) == (name, None)


def test_nothing_at_all():
    assert parse_instance_name("") == (None, None)
    assert parse_instance_name(None) == (None, None)
    assert parse_instance_name(SUFFIX) == (None, None)


def test_same_device():
    assert same_device(UID, UID)
    assert same_device(UID, UID.upper())
    # One of them cut short, either way round.
    assert same_device(UID[:39], UID)
    assert same_device(UID, UID[:8])
    assert same_device(UID[:20], UID[:30])
    # Different devices, also when both were cut at the same place.
    other = UID[:-1] + "0"
    assert not same_device(UID, other)
    assert not same_device(UID[:39], other[:38] + "f")
    assert not same_device(UID[:12], "f" + UID[1:12])
    assert not same_device(UID[:20], "ffff" + UID[4:30])
    # Too little to go on, or nothing.
    assert not same_device(UID[:7], UID)
    assert not same_device("", UID)
    assert not same_device(None, UID)
    assert not same_device(UID, None)
    assert not same_device(None, None)
    assert not same_device(12345678, "12345678")
