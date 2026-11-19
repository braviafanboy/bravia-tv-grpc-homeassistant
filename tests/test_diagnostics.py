"""Diagnostics get attached to public issues: nothing that says who or where
may be in them."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from fake_tv import CREDS, FakeTv  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402

from custom_components.bravia_tv_grpc import diagnostics  # noqa: E402
from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    BraviaTvCoordinator,
)
from custom_components.bravia_tv_grpc.grpc import credentials as cred  # noqa: E402

COORD = "custom_components.bravia_tv_grpc.coordinator"

# Every value below that must not survive carries this marker.
SECRET = "LEAK"


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


async def _coordinator(hass: HomeAssistant):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{SECRET}-unique-id",
        title=f"Bravia TV ({SECRET}-192.168.1.20)",
        data={
            "host": f"{SECRET}-192.168.1.20",
            "grpc_port": 40000,
            "grpc_device_id": f"{SECRET}-device-id",
            "device_unique_id": f"{SECRET}-device-unique-id",
            "grpc_keys": cred.credentials_to_json(
                {**CREDS, "refresh_token": f"{SECRET}-refresh", "hmac_key": "ab" * 32}
            ),
            "agent_token": f"{SECRET}-agent-token",
            "agent_fingerprint": "cd" * 32,
            "model": "BRAVIA 8 II",
        },
        options={"rest_psk": f"{SECRET}-psk", "picture_poll_s": 60},
    )
    entry.add_to_hass(hass)
    tv = FakeTv("tv", 40000, "dev", CREDS["hmac_key"])
    tv.states.update(
        {
            "system_setting.friendly_name": f"{SECRET}'s TV",
            "system_setting.serial_number": f"{SECRET}-serial",
            "hdmi_connected_device.sshinfo": json.dumps(
                {"version": "0x10", "device_id": f"{SECRET}-soundbar"}
            ),
            "sound_setting.volume.bluetooth.device_info": json.dumps(
                {"name": f"{SECRET}'s headphones"}
            ),
            "sound_setting.audio_output.available_devices": json.dumps(
                [
                    {
                        "id": f"{SECRET}-id",
                        "name": f"{SECRET} speaker",
                        "type": "bluetooth",
                    }
                ]
            ),
            "sound_setting.audio_output.active_devices": json.dumps([f"{SECRET}-id"]),
            "speaker_connection_setting.serial_number.rl": f"{SECRET}-speaker-serial",
            "speaker_connection_setting.wifi_mac_address.sw": f"{SECRET}-mac",
        }
    )
    coordinator = BraviaTvCoordinator(hass, entry, tv)
    with patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()):
        await coordinator.async_start()
    coordinator._apply_agent_document(
        {
            "media": {
                "active": True,
                "app": "com.netflix.ninja",
                "state": "playing",
                "title": f"{SECRET} film",
                "artist": f"{SECRET} artist",
                "album": f"{SECRET} album",
                "artwork_url": f"/v1/media/artwork?v={SECRET}",
            },
            "bluetooth": {
                "remote": {"name": f"{SECRET} remote", "connected": True},
                "devices": [{"name": f"{SECRET}'s earbuds", "type": "audio"}],
            },
            "rest": {"available": True, "auth": "psk"},
            "picture": {"targets": {"contrast": {"value": "90"}}},
        }
    )
    coordinator.agent_info = {
        "agent": {"version": "0.9.1", "contract": 1},
        "device": {"model": "K-65XR8M2", "device_unique_id": f"{SECRET}-uid"},
        "pairing": {
            "paired": True,
            "paired_with": f"{SECRET}'s Home Assistant",
            "paired_from": f"{SECRET}-192.168.1.5",
        },
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return entry, coordinator


async def test_nothing_personal_survives(hass: HomeAssistant):
    entry, coordinator = await _coordinator(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id)},
        name=f"{SECRET}'s TV",
        model="BRAVIA 8 II",
    )
    for dump in (
        await diagnostics.async_get_config_entry_diagnostics(hass, entry),
        await diagnostics.async_get_device_diagnostics(hass, entry, device),
    ):
        text = json.dumps(dump, default=str)
        assert SECRET not in text, text[max(0, text.index(SECRET) - 80) :][:200]
        # What a bug report needs is still there.
        assert dump["capabilities"]["power"]["type"] == "bool"
        assert dump["state"]["volume"] == 18
        assert dump["state"]["agent.media"]["app"] == "com.netflix.ninja"
        assert dump["state"]["agent.media"]["state"] == "playing"
        assert dump["entry"]["data"]["grpc_port"] == 40000
        assert dump["entry"]["options"]["picture_poll_s"] == 60
        assert dump["agent"]["picture_targets"] == ["contrast"]
        assert dump["agent"]["info"]["agent"]["version"] == "0.9.1"
        assert dump["connection"] == {"offline": False, "connected": True}
    assert dump["device"]["model"] == "BRAVIA 8 II"
    await coordinator.async_shutdown()
