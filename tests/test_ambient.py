"""Ambient brightness: entities mirror and edit the agent's ``ambient`` block."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.bravia_tv_grpc import ambient  # noqa: E402
from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    AGENT_ONLINE,
    BraviaTvCoordinator,
    agent_path,
)
from custom_components.bravia_tv_grpc.grpc import agent as agentmod  # noqa: E402

STATUS = {
    "enabled": True,
    "learning": False,
    "state": "paused",
    "reason": "manual",
    "picture_mode": "cinema",
    "brightness": 18,
    "target": 20,
    "lux": 22,
    "own_curve": True,
    "anchors_lux": [0, 10, 25, 60, 150, 500],
    "curve": [5, 6, 8, 12, 20, 22],
    "curves": {
        "default": [9, 9, 11, 15, 25, 25],
        "modes": {"cinema": [5, 6, 8, 12, 20, 22]},
    },
}


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _coordinator(hass: HomeAssistant, status=STATUS) -> BraviaTvCoordinator:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="dev", data={"host": "tv", "agent_token": "t"}
    )
    entry.add_to_hass(hass)
    client = MagicMock()
    client.capabilities = {}
    coordinator = BraviaTvCoordinator(hass, entry, client)
    coordinator._state["power"] = True
    coordinator._state[AGENT_ONLINE] = True
    if status is not None:
        coordinator._state[agent_path("ambient")] = status
    coordinator.async_set_updated_data(dict(coordinator._state))
    return coordinator


async def test_entities_mirror_the_agent_status(hass: HomeAssistant):
    c = _coordinator(hass)
    enabled, learning = ambient.switches(c)
    assert enabled.available and enabled.is_on is True
    assert learning.is_on is False
    anchors = ambient.anchors(c)
    assert [a.native_value for a in anchors] == [5, 6, 8, 12, 20, 22]
    assert anchors[2]._attr_translation_placeholders == {"lux": "25"}
    status = ambient.BraviaTvAmbientStatusSensor(c)
    assert status.native_value == "paused"
    attrs = status.extra_state_attributes
    assert attrs["reason"] == "manual"
    assert attrs["curves"]["modes"]["cinema"] == [5, 6, 8, 12, 20, 22]


async def test_unavailable_with_an_agent_that_lacks_it(hass: HomeAssistant):
    c = _coordinator(hass, status=None)
    assert not ambient.switches(c)[0].available
    assert ambient.anchors(c)[0].native_value is None


async def test_editing_an_anchor_keeps_the_curve_in_order(hass: HomeAssistant):
    c = _coordinator(hass)
    c.async_ambient = AsyncMock()
    anchors = ambient.anchors(c)
    # Raising the 25 lux anchor above the 60 lux one lifts that one too.
    await anchors[2].async_set_native_value(15)
    c.async_ambient.assert_awaited_with("curve", {"anchors": [5, 6, 15, 15, 20, 22]})
    # Lowering the 150 lux anchor below lower ones pulls them down.
    await anchors[4].async_set_native_value(7)
    c.async_ambient.assert_awaited_with("curve", {"anchors": [5, 6, 7, 7, 7, 22]})


async def test_switches_and_reset_send_the_right_requests(hass: HomeAssistant):
    c = _coordinator(hass)
    c.async_ambient = AsyncMock()
    enabled, learning = ambient.switches(c)
    await enabled.async_turn_off()
    c.async_ambient.assert_awaited_with("config", {"enabled": False})
    await learning.async_turn_on()
    c.async_ambient.assert_awaited_with("config", {"learning": True})
    await ambient.BraviaTvAmbientResetButton(c).async_press()
    c.async_ambient.assert_awaited_with("reset", {})


async def test_coordinator_applies_the_new_status_and_translates_refusals(
    hass: HomeAssistant,
):
    c = _coordinator(hass)
    c.agent = MagicMock()
    c.agent.async_ambient = AsyncMock(return_value={**STATUS, "enabled": False})
    await c.async_ambient("config", {"enabled": False})
    assert c.agent_value("ambient", "enabled") is False
    c.agent.async_ambient = AsyncMock(
        side_effect=agentmod.AgentRequestError("invalid_curve", 400)
    )
    with pytest.raises(HomeAssistantError):
        await c.async_ambient("curve", {"anchors": [1, 2, 3]})


async def test_signal_hint_is_sent_and_never_raises(hass: HomeAssistant):
    c = _coordinator(hass)
    c.agent = MagicMock()
    c.agent.async_ambient = AsyncMock(return_value={})
    c._last_hdr_type = "hdr10"
    await c._async_send_ambient_signal()
    c.agent.async_ambient.assert_awaited_with("context", {"signal": "hdr10"})
    # An agent without the endpoint, or any failure, is ignored.
    c.agent.async_ambient = AsyncMock(side_effect=agentmod.AgentError("404"))
    await c._async_send_ambient_signal()
    c.agent.async_ambient = AsyncMock(side_effect=RuntimeError("boom"))
    await c._async_send_ambient_signal()


async def test_the_agents_unavailable_is_a_state_of_its_own(hass: HomeAssistant):
    """The agent reports "unavailable" when the feature cannot run (no light
    sensor, no key). That word is what Home Assistant calls an entity it
    cannot reach, so the sensor shows "not_available" instead."""
    coordinator = _coordinator(
        hass, {**STATUS, "state": "unavailable", "reason": "no_light_sensor"}
    )
    sensor = ambient.BraviaTvAmbientStatusSensor(coordinator)
    assert sensor.available
    assert sensor.native_value == "not_available"
    assert "unavailable" not in sensor.options
    assert sensor.native_value in sensor.options
    assert sensor.extra_state_attributes["reason"] == "no_light_sensor"
    # An unknown state is unknown, not passed through.
    coordinator._state[agent_path("ambient")] = {**STATUS, "state": "dancing"}
    assert sensor.native_value is None


async def test_moving_readings_are_not_recorded(hass: HomeAssistant):
    sensor = ambient.BraviaTvAmbientStatusSensor(_coordinator(hass))
    attributes = sensor.extra_state_attributes
    assert {"lux", "target", "brightness", "curves", "anchors_lux"} <= set(attributes)
    assert sensor._unrecorded_attributes >= {
        "lux",
        "target",
        "brightness",
        "curves",
        "anchors_lux",
    }
    # What explains the state is kept.
    assert not {"reason", "picture_mode", "own_curve"} & sensor._unrecorded_attributes


async def test_anchor_range_follows_the_brightness_slider(hass: HomeAssistant):
    from types import SimpleNamespace

    coordinator = _coordinator(hass)
    assert ambient.BraviaTvAmbientAnchor(coordinator, 0).native_max_value == 50
    coordinator.client.capabilities = {
        "display_setting.brightness": SimpleNamespace(type="int", min=0, max=100)
    }
    assert ambient.BraviaTvAmbientAnchor(coordinator, 0).native_max_value == 100
