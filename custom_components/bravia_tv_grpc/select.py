"""Select platform for the Bravia TV (gRPC) integration.

Notably exposes ``display_setting.picture_mode`` with the device's real option
list -- including the Custom for Pro / calibrated / Dolby Vision modes that the
HTTP REST integration could not enumerate.
"""

from __future__ import annotations

import json
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvEntity
from .util import friendly_enum_label, parse_json_value

_LOGGER = logging.getLogger(__name__)

# Audio output. The TV allows one output per group -- a "speaker" (TV Speaker or
# a connected audio system) and, independently, a "bluetooth" device -- so both
# a soundbar and BT headphones can be active at once. We model each group as its
# own select with an "Off" option.
#
# active_devices is a JSON array of the currently active output ids;
# available_devices lists {id, name, type}. Writing activate_devices REPLACES
# the whole active set (verified live: activate [soundbar, bt] -> both active;
# activate [bt] -> bt only). So a group change rewrites the full set, keeping the
# other group's current selection. It must be a JSON array of ids -- the
# bare-string form resets the gRPC service.
_AUDIO_ACTIVE = "sound_setting.audio_output.active_devices"
_AUDIO_AVAILABLE = "sound_setting.audio_output.available_devices"
_AUDIO_ACTIVATE = "sound_setting.audio_output.activate_devices"
_OFF = "Off"

# gRPC enum path -> (friendly name, icon). Options come from the capability.
#
# picture_mode, content_mode and theatre_mode are gated by the lot5Agreement
# prerequisite, which the client auto-confirms before writing. Some theatre_mode
# values (e.g. movieNight) are additionally context-dependent and the TV may
# reject them; the write path itself is verified working (daytime).
# gRPC enum path -> (entity translation key, icon). Name comes from strings.json.
_SELECTS: dict[str, tuple[str, str]] = {
    "display_setting.picture_mode": ("picture_mode", "mdi:image-filter-hdr"),
    "display_and_sound_setting.content_mode": ("content_mode", "mdi:television-play"),
    "display_and_sound_setting.theatre_mode": ("theatre_mode", "mdi:theater"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Bravia TV select entities from advertised enum capabilities."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    caps = coordinator.client.capabilities
    entities: list[SelectEntity] = []
    for path, (key, icon) in _SELECTS.items():
        meta = caps.get(path)
        if meta is not None and meta.type == "enum" and meta.values:
            entities.append(BraviaTvSelect(coordinator, path, key, icon, meta.values))
    if _AUDIO_ACTIVATE in caps:
        entities.append(
            BraviaTvAudioGroupSelect(coordinator, "speaker", "speakers", "mdi:speaker")
        )
        entities.append(
            BraviaTvAudioGroupSelect(
                coordinator, "bluetooth", "bluetooth", "mdi:bluetooth-audio"
            )
        )
    async_add_entities(entities)


class BraviaTvSelect(BraviaTvEntity, SelectEntity):
    """An enum gRPC field exposed as a select.

    The device reports options as raw camelCase enum values; they are shown with
    humanised labels (see ``friendly_enum_label``) and mapped back on selection.
    """

    _gate_on_unavailable_reason = True

    def __init__(
        self,
        coordinator: BraviaTvCoordinator,
        grpc_path: str,
        translation_key: str,
        icon: str,
        options: tuple[str, ...],
    ) -> None:
        super().__init__(coordinator, grpc_path)
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._raw_options = list(options)  # device enum values

    @property
    def options(self) -> list[str]:
        return [friendly_enum_label(v) for v in self._raw_options]

    @property
    def current_option(self) -> str | None:
        value = self._value
        if not isinstance(value, str):
            return None
        # A pushed value outside the advertised list still reflects reality.
        if value not in self._raw_options:
            self._raw_options.append(value)
        return friendly_enum_label(value)

    async def async_select_option(self, option: str) -> None:
        raw = next(
            (v for v in self._raw_options if friendly_enum_label(v) == option), None
        )
        if raw is None:
            _LOGGER.warning("Unknown %s option: %s", self._grpc_path, option)
            return
        await self._async_set(raw)


class BraviaTvAudioGroupSelect(BraviaTvEntity, SelectEntity):
    """One audio-output group (speakers or bluetooth) as an independent select.

    Selecting an output rewrites the full active_devices set, preserving the
    other group's current output; "Off" removes just this group's output.
    """

    def __init__(
        self,
        coordinator: BraviaTvCoordinator,
        group: str,
        translation_key: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator, _AUDIO_ACTIVE)
        self._group = group  # "bluetooth" | "speaker"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        # Both group selects share the active_devices path; disambiguate the id.
        self._attr_unique_id = f"{self._attr_unique_id}_{group}"

    def _devices(self) -> list[dict]:
        data = parse_json_value(self.state_value(_AUDIO_AVAILABLE))
        if not isinstance(data, list):
            return []
        return [
            d for d in data if isinstance(d, dict) and d.get("id") and d.get("name")
        ]

    def _in_group(self, device: dict) -> bool:
        return (device.get("type") == "bluetooth") == (self._group == "bluetooth")

    def _active_ids(self) -> list[str]:
        active = parse_json_value(self.state_value(_AUDIO_ACTIVE))
        if not isinstance(active, list):
            return []
        return [a for a in active if isinstance(a, str)]

    @property
    def options(self) -> list[str]:
        return [d["name"] for d in self._devices() if self._in_group(d)] + [_OFF]

    @property
    def current_option(self) -> str | None:
        by_id = {d["id"]: d for d in self._devices()}
        for active_id in self._active_ids():
            device = by_id.get(active_id)
            if device and self._in_group(device):
                return device["name"]
        return _OFF

    async def async_select_option(self, option: str) -> None:
        by_id = {d["id"]: d for d in self._devices()}
        # Keep the other group's active output(s); replace only this group's.
        preserved = [
            active_id
            for active_id in self._active_ids()
            if not (by_id.get(active_id) and self._in_group(by_id[active_id]))
        ]
        chosen: list[str] = []
        if option != _OFF:
            target = next(
                (
                    d["id"]
                    for d in self._devices()
                    if self._in_group(d) and d["name"] == option
                ),
                None,
            )
            if target is None:
                _LOGGER.warning("Unknown %s output: %s", self._group, option)
                return
            chosen = [target]
        await self.coordinator.async_set_field(
            _AUDIO_ACTIVATE, json.dumps(preserved + chosen)
        )
