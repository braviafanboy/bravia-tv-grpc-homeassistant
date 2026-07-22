"""Binary-sensor tests (require the HA test harness).

Focus on the HDMI Linked Device sensor, which doubles as the BRAVIA Connect
"combined" indicator: linked carries a hex device_id + version 0x10; unlinked
reports the literal string "none" + version 0x00 (while audio still routes to
the soundbar, so the uncombine is otherwise silent).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.bravia_tv_grpc.binary_sensor import (  # noqa: E402
    BraviaTvHdmiDeviceSensor,
)
from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    BraviaTvCoordinator,
)

HDMI = "hdmi_connected_device.sshinfo"


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


async def _sensor(hass: HomeAssistant) -> BraviaTvHdmiDeviceSensor:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id="dev", data={"host": "x"})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.capabilities = {}
    client.get_states.return_value = {}
    client.read_application_list.return_value = []
    coordinator = BraviaTvCoordinator(hass, entry, client)
    await coordinator.async_start()
    return BraviaTvHdmiDeviceSensor(coordinator)


async def test_linked_soundbar_is_on_with_attrs(hass: HomeAssistant) -> None:
    sensor = await _sensor(hass)
    sensor.coordinator._apply_delta(HDMI, '{"version":"0x10","device_id":"0xbac76409"}')
    assert sensor.is_on is True
    assert sensor.extra_state_attributes == {
        "device_id": "0xbac76409",
        "version": "0x10",
    }
    await sensor.coordinator.async_shutdown()


async def test_uncombined_none_is_off_without_attrs(hass: HomeAssistant) -> None:
    """The uncombined state ('none' / 0x00) must read off, not on."""
    sensor = await _sensor(hass)
    sensor.coordinator._apply_delta(HDMI, '{"version":"0x00","device_id":"none"}')
    assert sensor.is_on is False
    assert sensor.extra_state_attributes == {}
    await sensor.coordinator.async_shutdown()


async def test_zero_device_id_is_off(hass: HomeAssistant) -> None:
    sensor = await _sensor(hass)
    sensor.coordinator._apply_delta(HDMI, '{"version":"0x00","device_id":"0x0"}')
    assert sensor.is_on is False
    await sensor.coordinator.async_shutdown()


async def test_nothing_reported_is_unknown(hass: HomeAssistant) -> None:
    sensor = await _sensor(hass)
    assert sensor.is_on is None
    await sensor.coordinator.async_shutdown()
