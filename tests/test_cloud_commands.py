"""Unit tests for the pure parts of grpc/cloud_commands.py."""

from __future__ import annotations

import json
from pathlib import Path

from btvgrpc import cloud_commands as cc
import pytest

_SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "cloud_device.json"


def test_set_scalar_uses_value():
    assert cc.set_command("sleep_timer", "30min") == {
        "name": "sleep_timer",
        "command": "set",
        "args": {"value": "30min"},
    }
    assert cc.set_command("picture", False)["args"] == {"value": False}
    assert cc.set_command("seek", 120)["args"] == {"value": 120}


def test_set_dict_uses_args():
    # Any-typed capabilities take a JSON object under ``args`` (SetAny).
    cmd = cc.set_command("input", {"label": "HDMI 1"})
    assert cmd["args"] == {"label": "HDMI 1"}


def test_plus_post_toggle():
    assert cc.plus_command("seek", -10) == {
        "name": "seek",
        "command": "plus",
        "args": {"value": -10},
    }
    # Verified live: the argument object is sent flat (no extra nesting).
    assert cc.post_command("application.deep_link", {"uri": "x"}) == {
        "name": "application.deep_link",
        "command": "post",
        "args": {"uri": "x"},
    }
    assert cc.toggle_command("mute") == {"name": "mute", "command": "toggle"}


def test_build_message_envelope():
    # The exact document BRAVIA Connect posts to /messages.
    msg = cc.build_message("dev-1", [cc.set_command("sleep_timer", "off")])
    assert msg == {
        "device_id": "dev-1",
        "type": "command",
        "body": {
            "commands": [
                {"name": "sleep_timer", "command": "set", "args": {"value": "off"}}
            ]
        },
    }


def test_build_message_requires_commands():
    with pytest.raises(ValueError):
        cc.build_message("dev-1", [])


def test_unwrap_data_envelope():
    assert cc.unwrap({"data": {"message_id": "m"}}) == {"message_id": "m"}
    assert cc.unwrap({"message_id": "m"}) == {"message_id": "m"}
    assert cc.unwrap({"data": None}) is None


_TV_RESULT = {
    "device_id": "dev-1",
    "result": "SUCCESS",
    "error": None,
    "reports": {
        "changes": ["sleep_timer"],
        "states": [{"name": "sleep_timer", "value": "30min", "type": "enum"}],
    },
    "timestamp": "2026-11-02T18:00:00Z",
}


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (_TV_RESULT, True),
        ({"data": _TV_RESULT}, True),
        ({**_TV_RESULT, "result": "FAILURE", "error": "POWER_OFF"}, False),
        ({"result": True}, True),
        ({"result": False}, False),
        # Verified live 2026-11-02: lowercase outcome, and an unanswered poll
        # answers HTTP 200 with result "waiting".
        ({"device_id": "dev-1", "result": "success", "reports": {}}, True),
        ({"device_id": "dev-1", "result": "waiting"}, None),
        ({}, None),
        (None, None),
        ({"data": None}, None),
        ({"device_id": "dev-1"}, None),
    ],
)
def test_parse_result(document, expected):
    assert cc.parse_result(document)[0] is expected


def test_reported_states():
    assert cc.reported_states(_TV_RESULT) == {"sleep_timer": "30min"}
    assert cc.reported_states({"reports": None}) == {}
    assert cc.reported_states({}) == {}


def test_capabilities_from_captured_device():
    device = json.loads(_SCHEMA.read_text())
    caps = cc.capabilities_from_device(device)
    # Every cloud-only entry must be a capability the K-65XR8M2 actually
    # advertises to the cloud, with the same command words.
    for name, commands in cc.CLOUD_ONLY_CAPABILITIES.items():
        assert caps.get(name) == commands, name
    assert cc.capability_values(device, "sleep_timer") == cc.SLEEP_TIMER_VALUES
    assert cc.capability_values(device, "playback") == cc.PLAYBACK_VALUES
    assert cc.capability_values(device, "nope") == ()
