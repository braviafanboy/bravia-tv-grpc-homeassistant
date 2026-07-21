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
