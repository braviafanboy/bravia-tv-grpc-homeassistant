"""Ambient brightness entities.

The HA TV Agent runs the loop on the TV: it sets the Brightness slider from
the room's light level with a curve per picture mode (SDR only), and pauses
or learns when someone changes the brightness by hand. Home Assistant edits
and mirrors it through the agent's ``ambient`` block:

- switches: on/off, and learning from manual changes;
- six numbers: the current picture mode's curve, the slider value at fixed
  light levels (they show and edit whichever mode is active, as the TV's own
  picture menu does);
- a button to reset the current mode's curve to the shared default;
- a status sensor saying whether it is active, and why not.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvAgentEntity

BLOCK = "ambient"
# Lux of each curve anchor; the agent reports its own axis, this is the
# fallback for naming before it has.
ANCHOR_LUX = (0, 10, 25, 60, 150, 500)
STATES = ["off", "active", "paused", "unavailable"]


class BraviaTvAmbientEntity(BraviaTvAgentEntity):
    """An ambient brightness entity; unavailable with agents that lack it."""

    def __init__(self, coordinator: BraviaTvCoordinator, key: str) -> None:
        super().__init__(coordinator, BLOCK, f"ambient_{key}")

    @property
    def available(self) -> bool:
        return super().available and isinstance(
            self.coordinator.agent_value(BLOCK), dict
        )


class BraviaTvAmbientSwitch(BraviaTvAmbientEntity, SwitchEntity):
    """Ambient brightness on/off, or learning from manual changes."""

    def __init__(self, coordinator: BraviaTvCoordinator, field: str) -> None:
        super().__init__(coordinator, field)
        self._field = field
        self._attr_translation_key = (
            "ambient_brightness" if field == "enabled" else "ambient_learning"
        )
        if field == "learning":
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool | None:
        value = self.agent_value(self._field)
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_ambient("config", {self._field: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_ambient("config", {self._field: False})


class BraviaTvAmbientAnchor(BraviaTvAmbientEntity, NumberEntity):
    """The current picture mode's brightness at one light level."""

    _attr_translation_key = "ambient_anchor"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 50
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: BraviaTvCoordinator, index: int) -> None:
        super().__init__(coordinator, f"anchor_{index}")
        self._index = index
        self._attr_translation_placeholders = {"lux": str(ANCHOR_LUX[index])}

    def _curve(self) -> list[int] | None:
        curve = self.agent_value("curve")
        if isinstance(curve, list) and len(curve) == len(ANCHOR_LUX):
            return [int(v) for v in curve]
        return None

    @property
    def native_value(self) -> float | None:
        curve = self._curve()
        return curve[self._index] if curve else None

    async def async_set_native_value(self, value: float) -> None:
        curve = self._curve()
        if curve is None:
            return
        curve[self._index] = round(value)
        # Keep the curve non-decreasing around the edited anchor, as the agent
        # would: lower anchors may not exceed it, higher ones not fall below.
        for i in range(self._index - 1, -1, -1):
            curve[i] = min(curve[i], curve[i + 1])
        for i in range(self._index + 1, len(curve)):
            curve[i] = max(curve[i], curve[i - 1])
        await self.coordinator.async_ambient("curve", {"anchors": curve})


class BraviaTvAmbientResetButton(BraviaTvAmbientEntity, ButtonEntity):
    """Drop the current picture mode's own curve; it follows the default again."""

    _attr_translation_key = "ambient_reset"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, "reset")

    async def async_press(self) -> None:
        await self.coordinator.async_ambient("reset", {})


class BraviaTvAmbientStatusSensor(BraviaTvAmbientEntity, SensorEntity):
    """Off, active, paused or unavailable, with the reason and the curves."""

    _attr_translation_key = "ambient_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> str | None:
        state = self.agent_value("state")
        return state if state in STATES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        keys = (
            "reason",
            "picture_mode",
            "brightness",
            "target",
            "lux",
            "own_curve",
            "anchors_lux",
            "curves",
        )
        return {key: self.agent_value(key) for key in keys}


def switches(coordinator: BraviaTvCoordinator) -> list[BraviaTvAmbientSwitch]:
    return [
        BraviaTvAmbientSwitch(coordinator, "enabled"),
        BraviaTvAmbientSwitch(coordinator, "learning"),
    ]


def anchors(coordinator: BraviaTvCoordinator) -> list[BraviaTvAmbientAnchor]:
    return [BraviaTvAmbientAnchor(coordinator, i) for i in range(len(ANCHOR_LUX))]
