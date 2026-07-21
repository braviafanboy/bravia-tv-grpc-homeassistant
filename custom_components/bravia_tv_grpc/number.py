"""Number platform for the Bravia TV (gRPC) integration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvEntity

# gRPC int path -> (friendly name, icon). Range comes from the capability meta.
#
# The per-output volumes and voice_zoom are writable only while that output is
# the active one; the base entity gates them via .visibility / .availability
# (observed: bluetooth uses .visibility, voice_zoom uses .availability), so they
# show unavailable when their output isn't active.
#
# wired_headphone is intentionally omitted: this TV model has no headphone jack.
# gRPC int path -> (entity translation key, icon). Name comes from strings.json.
_NUMBERS: dict[str, tuple[str, str]] = {
    "volume": ("volume", "mdi:volume-high"),
    "display_setting.brightness": ("brightness", "mdi:brightness-6"),
    "sound_setting.volume.tv_speaker": ("tv_speaker_volume", "mdi:speaker"),
    "sound_setting.volume.bluetooth": ("bluetooth_volume", "mdi:bluetooth-audio"),
    # Dialogue-clarity boost (-6..+6), only on TV Speakers as the sole output.
    "sound_setting.voice_zoom": ("voice_zoom", "mdi:account-voice"),
    # Output level to a connected HDMI/eARC audio system.
    "sound_setting.volume.hdmi": ("hdmi_output_volume", "mdi:volume-high"),
    # Levels for the TV's Direct Connect wireless rear speakers / subwoofer
    # (BRAVIA Theatre speakers paired straight to the TV, no soundbar; -10..+10).
    # Availability latches on until a matching speaker has been paired
    # (unavailable_reason no_connection_history), so the base entity shows them
    # unavailable until then.
    "sound_setting.volume.rear": ("rear_speaker_volume", "mdi:speaker-multiple"),
    "sound_setting.volume.subwoofer": ("subwoofer_volume", "mdi:speaker-wireless"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Bravia TV number entities from advertised capabilities."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    caps = coordinator.client.capabilities
    async_add_entities(
        BraviaTvNumber(coordinator, path, key, icon)
        for path, (key, icon) in _NUMBERS.items()
        if path in caps and caps[path].type == "int"
    )


class BraviaTvNumber(BraviaTvEntity, NumberEntity):
    """An integer gRPC field exposed as a number, ranged by its capability."""

    _gate_on_unavailable_reason = True

    def __init__(
        self,
        coordinator: BraviaTvCoordinator,
        grpc_path: str,
        translation_key: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator, grpc_path)
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        # The master `volume` number duplicates the media_player's volume slider,
        # so it's disabled by default (enable it for automations if wanted).
        if grpc_path == "volume":
            self._attr_entity_registry_enabled_default = False
        meta = coordinator.client.capabilities.get(grpc_path)
        if meta is not None and meta.min is not None:
            self._attr_native_min_value = float(meta.min)
        if meta is not None and meta.max is not None:
            self._attr_native_max_value = float(meta.max)
        self._attr_native_step = 1

    @property
    def native_value(self) -> float | None:
        value = self._value
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self._async_set(int(value))
