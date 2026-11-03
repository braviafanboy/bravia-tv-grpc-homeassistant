"""Select platform for the Bravia TV (gRPC) integration.

Notably exposes ``display_setting.picture_mode`` with the device's real option
list -- including the Custom for Pro / calibrated / Dolby Vision modes that the
HTTP REST integration could not enumerate.
"""

from __future__ import annotations

import asyncio
import json
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import picture
from .const import DOMAIN
from .coordinator import BraviaTvCoordinator
from .entity import (
    BraviaTvAgentEntity,
    BraviaTvCloudEntity,
    BraviaTvEntity,
    BraviaTvPictureEntity,
    async_add_picture_entities,
)
from .grpc import cloud_commands as cloud
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
# Reports whether the last activate_devices took effect (verified live: true
# after each successful switch). Read back, with active_devices, to confirm.
_AUDIO_RESULT = "sound_setting.audio_output.activation_result"
_AUDIO_CONFIRM_DELAY = 1.5
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

# Cloud-only enum capabilities (no local gRPC path): cloud name -> (translation
# key, icon). Options come from the TV's advertised cloud capability values.
# sleep_timer: "off" | "15min" ... "120min"; the TV clears it on power-off.
# speaker_type (tv / audio_system) overlaps the local Speakers select, so it is
# created disabled by default.
_CLOUD_SELECTS: dict[str, tuple[str, str]] = {
    "sleep_timer": ("sleep_timer", "mdi:timer-sand"),
    "speaker_type": ("speaker_type", "mdi:speaker-multiple"),
}
_CLOUD_DISABLED_BY_DEFAULT = {"speaker_type"}
_CLOUD_ACRONYMS = {"tv": "TV", "hdmi": "HDMI"}


def _cloud_label(value: str) -> str:
    """``15min`` -> ``15 min``, ``audio_system`` -> ``Audio System``."""
    if value.endswith("min") and value[:-3].isdigit():
        return f"{value[:-3]} min"
    if "_" in value or value in _CLOUD_ACRONYMS:
        return " ".join(
            _CLOUD_ACRONYMS.get(w, w.capitalize()) for w in value.split("_")
        )
    return friendly_enum_label(value)


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
    for name, (key, icon) in _CLOUD_SELECTS.items():
        values = coordinator.cloud_values.get(name)
        if coordinator.cloud_supports(name) and values:
            entities.append(BraviaTvCloudSelect(coordinator, name, key, icon, values))
    if coordinator.agent is not None and coordinator.cloud_supports(
        _SELECT_MEDIA, "post"
    ):
        entities.append(BraviaTvThemeSelect(coordinator))
    if coordinator.agent is not None and coordinator.rest_writable:
        entities.append(BraviaTvPowerSavingSelect(coordinator))
    async_add_entities(entities)
    if coordinator.rest_writable:
        async_add_picture_entities(
            coordinator,
            entry,
            async_add_entities,
            lambda c, target, kind: (
                BraviaTvPictureSelect(c, target)
                if kind == picture.KIND_SELECT
                else None
            ),
        )


class BraviaTvCloudSelect(BraviaTvCloudEntity, SelectEntity):
    """A cloud-only enum capability (e.g. the sleep timer) as a select."""

    def __init__(
        self,
        coordinator: BraviaTvCoordinator,
        cloud_name: str,
        translation_key: str,
        icon: str,
        options: tuple[str, ...],
    ) -> None:
        super().__init__(coordinator, cloud_name)
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._raw_options = list(options)
        if cloud_name in _CLOUD_DISABLED_BY_DEFAULT:
            self._attr_entity_registry_enabled_default = False

    @property
    def options(self) -> list[str]:
        return [_cloud_label(v) for v in self._raw_options]

    @property
    def current_option(self) -> str | None:
        value = self._value
        if not isinstance(value, str):
            return None
        if value not in self._raw_options:
            self._raw_options.append(value)
        return _cloud_label(value)

    async def async_select_option(self, option: str) -> None:
        raw = next((v for v in self._raw_options if _cloud_label(v) == option), None)
        if raw is None:
            _LOGGER.warning("Unknown %s option: %s", self._cloud_name, option)
            return
        await self._async_cloud_set(raw)


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
        wanted = preserved + chosen
        await self.coordinator.async_set_field(_AUDIO_ACTIVATE, json.dumps(wanted))
        # The write returns before the switch completes; confirm it took.
        await asyncio.sleep(_AUDIO_CONFIRM_DELAY)
        states = await self.coordinator.async_read_paths([_AUDIO_ACTIVE, _AUDIO_RESULT])
        active = parse_json_value(states.get(_AUDIO_ACTIVE))
        if states.get(_AUDIO_RESULT) is False or (
            isinstance(active, list) and not set(wanted) <= set(active)
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="exec_rejected",
                translation_placeholders={"path": _AUDIO_ACTIVATE},
            )


_SELECT_MEDIA = "select_media"
_LIVING_DECOR = "com.sony.dtv.livingfit"


class BraviaTvThemeSelect(BraviaTvAgentEntity, SelectEntity):
    """Living Decor theme: options from the agent's theme list, shown through
    the cloud ``select_media`` command (the TV never reports which theme is
    on, so the last selection is kept optimistically until the TV restarts)."""

    _attr_translation_key = "living_decor_theme"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, "display", "living_decor_theme")
        self._current: str | None = None

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.agent_themes)

    def _themes(self) -> dict[str, str]:
        return {
            str(t.get("title") or t.get("id")): str(t["id"])
            for t in self.coordinator.agent_themes
            if t.get("id")
        }

    @property
    def options(self) -> list[str]:
        return list(self._themes())

    @property
    def current_option(self) -> str | None:
        return self._current if self._current in self._themes() else None

    async def async_select_option(self, option: str) -> None:
        theme_id = self._themes().get(option)
        if theme_id is None:
            raise HomeAssistantError(f"Unknown Living Decor theme {option}")
        await self.coordinator.async_cloud_command(
            _SELECT_MEDIA,
            [
                cloud.post_command(
                    _SELECT_MEDIA, {"contentId": theme_id, "packageName": _LIVING_DECOR}
                )
            ],
        )
        self._current = option
        self.async_write_ha_state()


class BraviaTvPictureSelect(BraviaTvPictureEntity, SelectEntity):
    """A picture setting with named choices (color temperature, Motionflow, …);
    the choices are the TV's own values."""

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def options(self) -> list[str]:
        return picture.select_options(self.target, self.info)

    @property
    def current_option(self) -> str | None:
        value = self.raw_value
        return value if value in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self.async_write(option)


POWER_SAVING_MODES = ["off", "low", "high"]


class BraviaTvPowerSavingSelect(BraviaTvAgentEntity, SelectEntity):
    """The TV's power-saving mode (REST IP control, relayed by the agent)."""

    _attr_translation_key = "power_saving"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = POWER_SAVING_MODES

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, "power_saving", "power_saving")

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.agent_value("rest", "available") is True
        )

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.agent_value("power_saving")
        return value if value in POWER_SAVING_MODES else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_power_saving(option)
