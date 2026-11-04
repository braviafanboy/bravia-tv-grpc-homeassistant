"""Agent entities survive a setup while the agent is unreachable (TV in standby).

Regression for a live failure: the integration reloads itself when the TV
drops off the network; if that happens in standby the agent cannot answer, and
setup used to skip every agent entity. The agent was found again minutes
later, but its entities stayed missing until the next restart with the TV on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.bravia_tv_grpc import (  # noqa: E402
    _async_update_listener,
    binary_sensor,
    button,
    select,
    sensor,
    update,
)
from custom_components.bravia_tv_grpc.const import (  # noqa: E402
    CONF_AGENT_TOKEN,
    DOMAIN,
)
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    BraviaTvCoordinator,
    agent_path,
)
from custom_components.bravia_tv_grpc.entity import (  # noqa: E402
    BraviaTvAgentEntity,
    BraviaTvPictureEntity,
    async_add_picture_entities,
)


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _setup(hass: HomeAssistant, *, token: str | None, options=None):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    data = {"host": "tv.local"}
    if token:
        data[CONF_AGENT_TOKEN] = token
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="dev", data=data, options=options or {}
    )
    entry.add_to_hass(hass)
    client = MagicMock()
    client.capabilities = {}
    coordinator = BraviaTvCoordinator(hass, entry, client)
    coordinator._state["power"] = True
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return entry, coordinator


async def _platform_entities(hass, entry) -> list:
    added: list = []
    for platform in (sensor, binary_sensor, button, select, update):
        await platform.async_setup_entry(hass, entry, added.extend)
    return added


async def test_paired_agent_unreachable_at_setup_keeps_its_entities(
    hass: HomeAssistant,
):
    entry, coordinator = _setup(hass, token="tok")
    assert coordinator.agent is None  # the TV is in standby: nothing answered
    entities = await _platform_entities(hass, entry)
    agent_entities = [e for e in entities if isinstance(e, BraviaTvAgentEntity)]
    keys = {e.unique_id.split("_agent_", 1)[1] for e in agent_entities}
    # Sensors, binary sensors, the setup button, the update entity and (no
    # key in the options) the read-only power-saving sensor.
    assert {"illuminance", "panel", "setup", "update", "power_saving"} <= keys, keys
    # Present but unavailable until the agent's stream is up.
    assert all(not e.available for e in agent_entities)
    coordinator._state[agent_path("online")] = True
    coordinator._state[agent_path("environment")] = {"illuminance_lx": 12}
    coordinator.async_set_updated_data(dict(coordinator._state))
    illuminance = next(e for e in agent_entities if e.unique_id.endswith("illuminance"))
    assert illuminance.available


async def test_no_agent_ever_paired_creates_no_agent_entities(hass: HomeAssistant):
    entry, coordinator = _setup(hass, token=None)
    entities = await _platform_entities(hass, entry)
    assert not [e for e in entities if isinstance(e, BraviaTvAgentEntity)]


async def test_picture_entities_appear_when_the_agent_reports_later(
    hass: HomeAssistant,
):
    entry, coordinator = _setup(hass, token="tok")
    added: list = []
    async_add_picture_entities(
        coordinator, entry, added.extend, lambda c, t, k: BraviaTvPictureEntity(c, t)
    )
    assert added == []  # nothing known yet
    coordinator._state[agent_path("picture")] = {
        "targets": {
            "contrast": {
                "value": "90",
                "available": True,
                "candidates": [{"min": 0, "max": 100, "step": 1}],
            }
        }
    }
    with patch.object(coordinator, "async_agent_resync_rest", new=AsyncMock()):
        coordinator.async_set_updated_data(dict(coordinator._state))
        await hass.async_block_till_done()
    assert [e.target for e in added] == ["contrast"]


async def test_first_pairing_after_setup_reloads_once(hass: HomeAssistant):
    entry, coordinator = _setup(hass, token=None)
    coordinator.agent_entities_created = coordinator.agent_expected  # False
    coordinator.applied_manual_port = None
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_AGENT_TOKEN: "new"}
    )
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload:
        await _async_update_listener(hass, entry)
    reload.assert_awaited_once_with(entry.entry_id)
    # Already set up with the agent's entities: no reload for later data writes.
    coordinator.agent_entities_created = True
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload:
        await _async_update_listener(hass, entry)
    reload.assert_not_awaited()


async def test_manual_refresh_returns_pushed_state_and_keeps_failures(
    hass: HomeAssistant,
):
    """homeassistant.update_entity must not raise NotImplementedError, and must
    not make a dead TV look alive."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    _entry, coordinator = _setup(hass, token=None)
    coordinator._state["volume"] = 12
    coordinator.async_set_updated_data(dict(coordinator._state))
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data["volume"] == 12

    coordinator.async_set_update_error(UpdateFailed("Bravia TV liveness probe failed"))
    await coordinator.async_refresh()
    assert not coordinator.last_update_success


async def test_agent_update_entity_refresh_rechecks_the_manifest(
    hass: HomeAssistant,
):
    entry, coordinator = _setup(hass, token="tok")
    entity = update.BraviaTvAgentUpdate(coordinator)
    with patch.object(
        coordinator, "async_check_agent_update", new=AsyncMock()
    ) as check:
        await entity.async_update()
    check.assert_awaited_once()


async def test_retired_living_decor_select_is_removed(hass: HomeAssistant):
    from homeassistant.helpers import entity_registry as er

    from custom_components.bravia_tv_grpc import _remove_retired_entities

    entry, _coordinator = _setup(hass, token="tok")
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "select",
        DOMAIN,
        "dev_agent_living_decor_theme",
        config_entry=entry,
    )
    _remove_retired_entities(hass, entry)
    assert registry.async_get(old.entity_id) is None


async def test_retired_offset_numbers_are_removed(hass: HomeAssistant):
    from homeassistant.helpers import entity_registry as er

    from custom_components.bravia_tv_grpc import _remove_retired_entities

    entry, _coordinator = _setup(hass, token="tok")
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "number", DOMAIN, "dev_agent_picture_redOffset", config_entry=entry
    )
    keep = registry.async_get_or_create(
        "number", DOMAIN, "dev_agent_picture_redGain", config_entry=entry
    )
    _remove_retired_entities(hass, entry)
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get(keep.entity_id) is not None
