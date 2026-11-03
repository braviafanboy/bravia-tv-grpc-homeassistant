"""Coordinator tests for the Sony cloud command channel (require HA harness)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.bravia_tv_grpc.const import (  # noqa: E402
    CONF_GRPC_DEVICE_ID,
    CONF_GRPC_KEYS,
    DOMAIN,
)
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    BraviaTvCoordinator,
    cloud_path,
)
from custom_components.bravia_tv_grpc.grpc import cloud_commands as cloud  # noqa: E402

COORD = "custom_components.bravia_tv_grpc.coordinator"

_KEYS = (
    '{"device_id": "dev", "hmac_key": "ab", "refresh_token": "rt",'
    ' "access_token": "at", "access_token_expires_at": 4102444800}'
)
_DEVICE = {
    "device_id": "dev",
    "device_type": "TV",
    "capabilities": [
        {
            "name": "sleep_timer",
            "type": "enum",
            "props": {"commands": ["set"], "values": ["off", "30min"], "report": True},
        },
        {"name": "picture", "type": "bool", "props": {"commands": ["set"]}},
        {"name": "playback", "type": "enum", "props": {"commands": ["set"]}},
    ],
}
_STATES = {
    "states": [
        {"name": "sleep_timer", "value": "off", "type": "enum"},
        {"name": "picture", "value": True, "type": "bool"},
        {"name": "power", "value": True, "type": "bool"},
    ]
}


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _client() -> MagicMock:
    client = MagicMock()
    client.capabilities = {}
    client.get_states.return_value = {"power": True, "volume": 10}
    client.read_application_list.return_value = []
    return client


def _entry(hass: HomeAssistant):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev",
        data={"host": "x", CONF_GRPC_KEYS: _KEYS, CONF_GRPC_DEVICE_ID: "dev"},
    )
    entry.add_to_hass(hass)
    return entry


def _cloud_patches():
    return (
        patch(
            f"{COORD}.cred.async_get_devices",
            new=AsyncMock(return_value={"devices": [_DEVICE]}),
        ),
        patch(
            f"{COORD}.cred.async_get_device_states",
            new=AsyncMock(return_value=_STATES),
        ),
    )


async def test_startup_loads_cloud_capabilities_and_states(
    hass: HomeAssistant,
) -> None:
    coordinator = BraviaTvCoordinator(hass, _entry(hass), _client())
    p1, p2 = _cloud_patches()
    with p1, p2:
        await coordinator.async_start()
    assert coordinator.cloud_supports("sleep_timer")
    assert coordinator.cloud_supports("picture")
    assert not coordinator.cloud_supports("seek")
    assert coordinator.cloud_values["sleep_timer"] == ("off", "30min")
    # Cloud-only states land under the cloud prefix; the local power path is
    # not overwritten by the cloud value because the device seed succeeded.
    assert coordinator.data[cloud_path("sleep_timer")] == "off"
    assert coordinator.data[cloud_path("picture")] is True
    assert coordinator.data["volume"] == 10
    await coordinator.async_shutdown()


async def test_cloud_unreachable_leaves_no_cloud_entities(hass: HomeAssistant) -> None:
    coordinator = BraviaTvCoordinator(hass, _entry(hass), _client())
    with patch(
        f"{COORD}.cred.async_get_devices",
        new=AsyncMock(side_effect=OSError("offline")),
    ):
        await coordinator.async_start()
    assert coordinator.cloud_capabilities == {}
    assert coordinator.data["power"] is True
    await coordinator.async_shutdown()


async def test_cloud_command_applies_reported_states(hass: HomeAssistant) -> None:
    coordinator = BraviaTvCoordinator(hass, _entry(hass), _client())
    p1, p2 = _cloud_patches()
    result = {
        "device_id": "dev",
        "result": "SUCCESS",
        "error": None,
        "reports": {
            "changes": ["sleep_timer"],
            "states": [{"name": "sleep_timer", "value": "30min", "type": "enum"}],
        },
    }
    run = AsyncMock(return_value=result)
    with p1, p2, patch(f"{COORD}.cloud.async_run_commands", new=run):
        await coordinator.async_start()
        await coordinator.async_cloud_command(
            "sleep_timer", [cloud.set_command("sleep_timer", "30min")]
        )
    session, device_id, token, commands = run.await_args.args
    assert device_id == "dev"
    assert token == "at"  # the stored, unexpired access token is reused
    assert commands == [
        {"name": "sleep_timer", "command": "set", "args": {"value": "30min"}}
    ]
    assert coordinator.data[cloud_path("sleep_timer")] == "30min"
    await coordinator.async_shutdown()


async def test_cloud_command_rejection_raises(hass: HomeAssistant) -> None:
    coordinator = BraviaTvCoordinator(hass, _entry(hass), _client())
    p1, p2 = _cloud_patches()
    err = cloud.CloudCommandError(
        "rejected",
        result={"result": "FAILURE", "error": "POWER_OFF"},
        error="POWER_OFF",
    )
    with (
        p1,
        p2,
        patch(f"{COORD}.cloud.async_run_commands", new=AsyncMock(side_effect=err)),
    ):
        await coordinator.async_start()
        with pytest.raises(HomeAssistantError) as excinfo:
            await coordinator.async_cloud_command(
                "picture", [cloud.set_command("picture", False)]
            )
    assert excinfo.value.translation_key == "cloud_rejected"
    assert excinfo.value.translation_placeholders["reason"] == "POWER_OFF"
    await coordinator.async_shutdown()


async def test_expired_token_is_refreshed_once(hass: HomeAssistant) -> None:
    coordinator = BraviaTvCoordinator(hass, _entry(hass), _client())
    p1, p2 = _cloud_patches()
    run = AsyncMock(
        side_effect=[cloud.CloudCommandError("401", status=401), {"result": "SUCCESS"}]
    )
    refresh = AsyncMock(return_value={"access_token": "new", "expires_in": 3600})
    with (
        p1,
        p2,
        patch(f"{COORD}.cloud.async_run_commands", new=run),
        patch(f"{COORD}.cred.async_refresh_access_token", new=refresh),
    ):
        await coordinator.async_start()
        await coordinator.async_cloud_command(
            "playback", [cloud.set_command("playback", "paused")]
        )
    assert run.await_count == 2
    assert run.await_args_list[1].args[2] == "new"
    await coordinator.async_shutdown()


async def test_power_off_clears_sleep_timer(hass: HomeAssistant) -> None:
    coordinator = BraviaTvCoordinator(hass, _entry(hass), _client())
    p1, p2 = _cloud_patches()
    with p1, p2:
        await coordinator.async_start()
    coordinator.set_cloud_value("sleep_timer", "30min")
    coordinator._apply_delta("power", False)
    assert coordinator.data[cloud_path("sleep_timer")] == "off"
    await coordinator.async_shutdown()


async def test_cloud_poll_mirrors_cloud_only_states(hass: HomeAssistant) -> None:
    coordinator = BraviaTvCoordinator(hass, _entry(hass), _client())
    p1, p2 = _cloud_patches()
    with p1, p2:
        await coordinator.async_start()
    assert coordinator.data[cloud_path("sleep_timer")] == "off"
    changed = {
        "states": [
            {"name": "sleep_timer", "value": "30min", "type": "enum"},
            {"name": "power", "value": False, "type": "bool"},  # not mirrored
        ]
    }
    with patch(
        f"{COORD}.cred.async_get_device_states", new=AsyncMock(return_value=changed)
    ):
        await coordinator._async_cloud_poll()
    assert coordinator.data[cloud_path("sleep_timer")] == "30min"
    assert coordinator.data["power"] is True
    await coordinator.async_shutdown()


async def test_turn_on_falls_back_to_cloud(hass: HomeAssistant) -> None:
    client = _client()
    client.exec_command.return_value = False  # local write rejected
    coordinator = BraviaTvCoordinator(hass, _entry(hass), client)
    p1, p2 = _cloud_patches()
    run = AsyncMock(return_value={"result": "SUCCESS"})
    device = dict(_DEVICE)
    device["capabilities"] = _DEVICE["capabilities"] + [
        {"name": "power", "type": "bool", "props": {"commands": ["set"]}}
    ]
    with (
        patch(
            f"{COORD}.cred.async_get_devices",
            new=AsyncMock(return_value={"devices": [device]}),
        ),
        p2,
        patch(f"{COORD}.cloud.async_run_commands", new=run),
    ):
        await coordinator.async_start()
        coordinator._apply_delta("power", False)
        await coordinator.async_turn_on_power()
    assert run.await_args.args[3] == [
        {"name": "power", "command": "set", "args": {"value": True}}
    ]
    assert coordinator.data["power"] is True
    await coordinator.async_shutdown()


async def test_read_paths_merges_state(hass: HomeAssistant) -> None:
    client = _client()
    coordinator = BraviaTvCoordinator(hass, _entry(hass), client)
    with patch(
        f"{COORD}.cred.async_get_devices", new=AsyncMock(side_effect=OSError("x"))
    ):
        await coordinator.async_start()
    client.get_states.return_value = {
        "sound_setting.audio_output.activation_result": True
    }
    got = await coordinator.async_read_paths(
        ["sound_setting.audio_output.activation_result"]
    )
    assert got == {"sound_setting.audio_output.activation_result": True}
    assert coordinator.data["sound_setting.audio_output.activation_result"] is True
    await coordinator.async_shutdown()


async def test_cloud_set_rejects_values_the_tv_does_not_advertise(hass: HomeAssistant):
    """An enum value outside the advertised list fails locally, no round trip."""
    entry = _entry(hass)
    coordinator = BraviaTvCoordinator(hass, entry, _client())
    coordinator.cloud_capabilities = {"sleep_timer": ("set",)}
    coordinator.cloud_values = {"sleep_timer": ("off", "30min")}
    with patch(f"{COORD}.cloud.async_run_commands", new=AsyncMock()) as run:
        with pytest.raises(HomeAssistantError):
            await coordinator.async_cloud_command(
                "sleep_timer", [cloud.set_command("sleep_timer", "99min")]
            )
        run.assert_not_called()


async def test_key_renewal_scheduled_before_expiry(hass: HomeAssistant):
    import time as _time

    entry = _entry(hass)
    coordinator = BraviaTvCoordinator(hass, entry, _client())
    expires = _time.time() + 5 * 3600
    with (
        patch(f"{COORD}.async_call_later") as later,
        patch.object(
            coordinator,
            "_cloud_credentials",
            return_value={"session_keys_expires_at": expires},
        ),
    ):
        coordinator._schedule_key_renewal()
    delay = later.call_args[0][1]
    assert 3.9 * 3600 <= delay.total_seconds() <= 4 * 3600


async def test_key_renewal_persists_and_rekeys(hass: HomeAssistant):
    entry = _entry(hass)
    client = _client()
    coordinator = BraviaTvCoordinator(hass, entry, client)
    new_creds = {
        "device_id": "dev",
        "hmac_key": "cd" * 32,
        "key_id": "k2",
        "session_key": "ef" * 32,
    }
    with (
        patch(
            f"{COORD}.cred.async_refresh_credentials",
            new=AsyncMock(return_value=new_creds),
        ),
        patch(f"{COORD}.async_call_later") as later,
    ):
        await coordinator._async_renew_keys()
    assert '"k2"' in entry.data[CONF_GRPC_KEYS]
    client.rekey.assert_called_once_with("cd" * 32, "k2", "ef" * 32)
    later.assert_called()
