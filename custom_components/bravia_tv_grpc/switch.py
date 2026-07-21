"""Switch platform for the Bravia TV (gRPC) integration."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvEntity

# gRPC bool path -> friendly name. Only surfaced when the device advertises them.
# The audio/picture ones are conditionally usable; the base entity gates them
# via .availability. bt_3d_surround needs a Bluetooth audio device active.
# calibrated_picture_mode is the streaming app's own calibrated preset (e.g.
# Netflix Calibrated Mode) -- available only while a supported app (Netflix /
# Prime Video) is foreground; turning it on locks picture_mode to
# '<app>Calibrated' (picture_mode then reports unavailable). Gated by the
# lot5Agreement prerequisite, which the client auto-confirms. Both verified live.
# gRPC bool path -> entity translation key (name comes from strings.json).
_SWITCHES: dict[str, str] = {
    "power": "power",
    "mute": "mute",
    "sound_setting.bt_3d_surround": "bluetooth_3d_surround",
    "display_setting.calibrated_picture_mode": "calibrated_picture_mode",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Bravia TV switch entities from advertised capabilities."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    caps = coordinator.client.capabilities
    async_add_entities(
        BraviaTvSwitch(coordinator, path, key)
        for path, key in _SWITCHES.items()
        if path in caps and caps[path].type == "bool"
    )


class BraviaTvSwitch(BraviaTvEntity, SwitchEntity):
    """A boolean gRPC field exposed as a switch."""

    _gate_on_unavailable_reason = True

    def __init__(
        self, coordinator: BraviaTvCoordinator, grpc_path: str, translation_key: str
    ) -> None:
        super().__init__(coordinator, grpc_path)
        self._attr_translation_key = translation_key

    @property
    def is_on(self) -> bool | None:
        value = self._value
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set(False)
