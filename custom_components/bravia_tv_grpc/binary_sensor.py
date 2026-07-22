"""Binary sensor platform for the Bravia TV (gRPC) integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvEntity
from .util import parse_json_value

# Firmware update is exposed via the update platform (update.py), not here.
_ERROR_STATUS = "error_status"
# Info about the single Sony-protocol device linked over HDMI/eARC (the "ssh" in
# the path is Sony's HES protocol namespace, not secure shell): any-typed JSON
# like {"version": "0x10", "device_id": "0xbac76409"}. The API exposes ONE such
# value -- there is no per-HDMI-port variant -- so it reflects the linked
# Sony/eARC device (typically the soundbar), not every port.
_HDMI_DEVICE = "hdmi_connected_device.sshinfo"

# Plain boolean paths -> (translation key, device class | None, entity category).
_BOOL_SENSORS: dict[str, tuple[str, BinarySensorDeviceClass | None, EntityCategory]] = {
    "system_setting.wifi_availability": (
        "wifi",
        BinarySensorDeviceClass.CONNECTIVITY,
        EntityCategory.DIAGNOSTIC,
    ),
    # Multiview (PIP/PBP) active.
    "system_setting.multiview": ("multiview", None, EntityCategory.DIAGNOSTIC),
    # Remote Start (wake for casting / remote power-on). Read-only here: although
    # caps advertise a "set" command, the write is a confirmed no-op over gRPC
    # even when remote_start.unavailable_reason is "none" (verified live: set
    # returns an empty response and the value is unchanged). Its unavailable_reason
    # enum (energy_mode_not_optimized/increased) shows it is really gated by the
    # TV's Eco/energy mode, which lives in the REST power-saving API, not this
    # control plane -- so it is surfaced read-only, never as a switch.
    "remote_start": ("remote_start", None, EntityCategory.DIAGNOSTIC),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Bravia TV binary sensors from advertised capabilities."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    caps = coordinator.client.capabilities
    entities: list[BinarySensorEntity] = [
        BraviaTvBoolSensor(coordinator, path, key, device_class, category)
        for path, (key, device_class, category) in _BOOL_SENSORS.items()
        if path in caps
    ]
    if _ERROR_STATUS in caps:
        entities.append(BraviaTvErrorSensor(coordinator))
    if _HDMI_DEVICE in caps:
        entities.append(BraviaTvHdmiDeviceSensor(coordinator))
    async_add_entities(entities)


class BraviaTvBoolSensor(BraviaTvEntity, BinarySensorEntity):
    """A boolean gRPC field exposed as a binary sensor."""

    def __init__(
        self,
        coordinator: BraviaTvCoordinator,
        grpc_path: str,
        translation_key: str,
        device_class: BinarySensorDeviceClass | None,
        entity_category: EntityCategory | None,
    ) -> None:
        super().__init__(coordinator, grpc_path)
        self._attr_translation_key = translation_key
        self._attr_device_class = device_class
        self._attr_entity_category = entity_category

    @property
    def is_on(self) -> bool | None:
        value = self._value
        return None if value is None else bool(value)


class BraviaTvErrorSensor(BraviaTvEntity, BinarySensorEntity):
    """TV fault/protection status (e.g. a paired speaker in protection).

    ``error_status`` is an enum-array; anything other than ``no_error`` is a
    problem. The active codes are exposed as an attribute.
    """

    _attr_translation_key = "error_status"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _ERROR_STATUS)

    def _errors(self) -> list[str]:
        raw = self._value
        if raw is None:
            return []
        if isinstance(raw, str):
            parsed = parse_json_value(raw)
            items = parsed if isinstance(parsed, list) else [raw]
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        return [str(x) for x in items if x and x != "no_error"]

    @property
    def is_on(self) -> bool:
        # No value reported means no fault has been signalled -> clear.
        return bool(self._errors())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        errors = self._errors()
        return {"errors": errors} if errors else {}


# device_id values that mean "nothing linked". When unlinked the TV reports the
# literal string "none" (with version "0x00"); a real link carries a hex id (and
# version "0x10"), so the id alone decides linked/unlinked.
_HDMI_EMPTY_IDS = {"", "0x0", "0x00000000", "0", "none"}


class BraviaTvHdmiDeviceSensor(BraviaTvEntity, BinarySensorEntity):
    """Whether the single Sony-protocol device is linked over HDMI/eARC.

    Read-only ``hdmi_connected_device.sshinfo`` any-typed JSON. The API exposes
    only one value (not per port), so this reflects the linked Sony/eARC device
    -- typically the soundbar -- with its id and protocol version as attributes
    (shown only while connected, since HA drops attributes from an off entity).

    This is also the BRAVIA Connect "combined" indicator: the TV+soundbar
    association drops this to ``{"device_id": "none"}`` while audio still routes
    to the soundbar, so an uncombine is otherwise silent.
    """

    _attr_translation_key = "hdmi_device"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _HDMI_DEVICE)

    def _info(self) -> dict[str, Any]:
        raw = self._value
        data = parse_json_value(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, dict) else {}

    def _linked_id(self) -> str | None:
        """The linked device's id, or None if nothing is linked."""
        device_id = self._info().get("device_id")
        if device_id is None:
            return None
        text = str(device_id).strip()
        return None if text.lower() in _HDMI_EMPTY_IDS else text

    @property
    def is_on(self) -> bool | None:
        if self._info().get("device_id") is None:
            return None  # nothing reported yet
        return self._linked_id() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._info()
        attrs: dict[str, Any] = {}
        if self._linked_id() is not None:
            attrs["device_id"] = info["device_id"]
            if info.get("version") is not None:
                attrs["version"] = info["version"]
        return attrs
