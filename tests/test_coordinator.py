"""Coordinator tests (require pytest-homeassistant-custom-component + HA)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402

from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (
    BraviaTvCoordinator,  # noqa: E402
)


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _client() -> MagicMock:
    client = MagicMock()
    client.capabilities = {}
    client.get_states.return_value = {"power": True, "volume": 10}
    client.read_application_list.return_value = []
    return client


async def test_seed_and_delta(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id="dev", data={"host": "x"})
    entry.add_to_hass(hass)
    coordinator = BraviaTvCoordinator(hass, entry, _client())

    await coordinator.async_start()
    assert coordinator.data["power"] is True
    assert coordinator.data["volume"] == 10

    # A push delta updates a single path.
    coordinator._apply_delta("volume", 15)
    assert coordinator.data["volume"] == 15

    await coordinator.async_shutdown()


async def test_device_name_prefers_friendly_name(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="dev", title="Bravia TV (x)", data={"host": "x"}
    )
    entry.add_to_hass(hass)
    client = _client()
    client.get_states.return_value = {"system_setting.friendly_name": "BRAVIA 8 II"}
    coordinator = BraviaTvCoordinator(hass, entry, client)
    await coordinator.async_start()
    assert coordinator.device_name == "BRAVIA 8 II"

    # Falls back to the entry title when the TV reports no friendly name.
    coordinator._state.pop("system_setting.friendly_name")
    assert coordinator.device_name == "Bravia TV (x)"

    await coordinator.async_shutdown()


async def test_availability_toggles(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id="dev", data={"host": "x"})
    entry.add_to_hass(hass)
    coordinator = BraviaTvCoordinator(hass, entry, _client())
    await coordinator.async_start()
    assert coordinator.last_update_success is True

    coordinator.async_set_update_error(UpdateFailed("connection lost"))
    assert coordinator.last_update_success is False

    # Recovery restores availability.
    coordinator.async_set_updated_data(dict(coordinator.data))
    assert coordinator.last_update_success is True

    await coordinator.async_shutdown()


async def test_firmware_available_sets_pending_flag(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id="dev", data={"host": "x"})
    entry.add_to_hass(hass)
    coordinator = BraviaTvCoordinator(hass, entry, _client())
    await coordinator.async_start()
    assert not entry.data.get("fw_update_pending")

    # An available firmware update persists the sticky pending flag (so setup can
    # re-fetch the installed version once it's applied).
    coordinator._apply_delta("fw_update.update_exist", True)
    assert entry.data.get("fw_update_pending") is True

    await coordinator.async_shutdown()
