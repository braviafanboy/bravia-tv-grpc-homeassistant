"""Base-entity availability tests (require HA test harness).

Writable entities honour a non-"none" <path>.unavailable_reason (the write
would be a silent no-op); read-only diagnostics keep showing their value.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import EntityCategory  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.bravia_tv_grpc.binary_sensor import (  # noqa: E402
    BraviaTvBoolSensor,
)
from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    BraviaTvCoordinator,
)
from custom_components.bravia_tv_grpc.switch import BraviaTvSwitch  # noqa: E402


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _coordinator(hass: HomeAssistant) -> BraviaTvCoordinator:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id="dev", data={"host": "x"})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.capabilities = {}
    client.get_states.return_value = {}
    client.read_application_list.return_value = []
    return BraviaTvCoordinator(hass, entry, client)


async def test_media_player_duplicates_disabled_by_default(hass: HomeAssistant) -> None:
    """power/mute switches and the master volume number duplicate the
    media_player, so they're disabled by default; unique settings are not."""
    from custom_components.bravia_tv_grpc.number import BraviaTvNumber

    coordinator = _coordinator(hass)
    await coordinator.async_start()

    power = BraviaTvSwitch(coordinator, "power", "power")
    mute = BraviaTvSwitch(coordinator, "mute", "mute")
    bt3d = BraviaTvSwitch(coordinator, "sound_setting.bt_3d_surround", "bt3d")
    vol = BraviaTvNumber(coordinator, "volume", "volume", "mdi:volume-high")
    hdmi_vol = BraviaTvNumber(
        coordinator,
        "sound_setting.volume.hdmi",
        "hdmi_output_volume",
        "mdi:volume-high",
    )
    tv_spk_vol = BraviaTvNumber(
        coordinator,
        "sound_setting.volume.tv_speaker",
        "tv_speaker_volume",
        "mdi:speaker",
    )
    brightness = BraviaTvNumber(
        coordinator, "display_setting.brightness", "brightness", "mdi:brightness-6"
    )

    assert power.entity_registry_enabled_default is False
    assert mute.entity_registry_enabled_default is False
    assert vol.entity_registry_enabled_default is False
    # Niche per-output volumes are disabled by default too.
    assert hdmi_vol.entity_registry_enabled_default is False
    assert tv_spk_vol.entity_registry_enabled_default is False
    # A unique setting stays enabled by default.
    assert bt3d.entity_registry_enabled_default is True
    assert brightness.entity_registry_enabled_default is True

    await coordinator.async_shutdown()


async def test_rear_sub_disabled_only_until_speakers_paired(
    hass: HomeAssistant,
) -> None:
    """Direct Connect rear/sub levels are disabled by default only while the TV
    reports no such speaker was ever paired; otherwise they stay enabled."""
    from custom_components.bravia_tv_grpc.number import BraviaTvNumber

    def make(coordinator, path: str, key: str) -> BraviaTvNumber:
        return BraviaTvNumber(coordinator, path, key, "mdi:speaker")

    rear = "sound_setting.volume.rear"
    sub = "sound_setting.volume.subwoofer"

    # Never paired: nothing could use the control, so it's off on a fresh install.
    unpaired = _coordinator(hass)
    await unpaired.async_start()
    unpaired._apply_delta(f"{rear}.unavailable_reason", "no_connection_history")
    unpaired._apply_delta(f"{sub}.unavailable_reason", "no_connection_history")
    assert (
        make(unpaired, rear, "rear_speaker_volume").entity_registry_enabled_default
        is False
    )
    assert (
        make(unpaired, sub, "subwoofer_volume").entity_registry_enabled_default is False
    )
    await unpaired.async_shutdown()

    # Paired (reason cleared): enabled by default.
    paired = _coordinator(hass)
    await paired.async_start()
    paired._apply_delta(f"{rear}.unavailable_reason", "none")
    paired._apply_delta(f"{sub}.unavailable_reason", "none")
    assert (
        make(paired, rear, "rear_speaker_volume").entity_registry_enabled_default
        is True
    )
    assert make(paired, sub, "subwoofer_volume").entity_registry_enabled_default is True
    await paired.async_shutdown()

    # State not known yet: fall back to enabled rather than hiding a control.
    unknown = _coordinator(hass)
    await unknown.async_start()
    assert (
        make(unknown, rear, "rear_speaker_volume").entity_registry_enabled_default
        is True
    )
    await unknown.async_shutdown()


async def test_writable_entity_gated_by_unavailable_reason(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    await coordinator.async_start()
    path = "display_setting.calibrated_picture_mode"
    switch = BraviaTvSwitch(coordinator, path, "calibrated_picture_mode")

    # No reason reported -> controllable.
    assert switch.available is True

    # A real restriction -> the write would no-op, so surface unavailable.
    coordinator._apply_delta(f"{path}.unavailable_reason", "unsupported_app")
    assert switch.available is False

    # Restriction cleared -> controllable again.
    coordinator._apply_delta(f"{path}.unavailable_reason", "none")
    assert switch.available is True

    await coordinator.async_shutdown()


async def test_readonly_sensor_ignores_unavailable_reason(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    await coordinator.async_start()
    # remote_start is read-only: it should keep showing its value even when the
    # TV reports an energy-mode restriction.
    sensor = BraviaTvBoolSensor(
        coordinator, "remote_start", "remote_start", None, EntityCategory.DIAGNOSTIC
    )
    coordinator._apply_delta(
        "remote_start.unavailable_reason", "energy_mode_not_optimized"
    )
    assert sensor.available is True

    await coordinator.async_shutdown()
