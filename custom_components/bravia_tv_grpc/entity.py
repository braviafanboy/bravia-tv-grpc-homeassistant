"""Base entity for the Bravia TV (gRPC) integration."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_MODEL_ID,
    CONF_SW_VERSION,
    DOMAIN,
)
from .coordinator import BraviaTvCoordinator


class BraviaTvEntity(CoordinatorEntity[BraviaTvCoordinator]):
    """Common base: device info, availability, and gRPC-path state access."""

    _attr_has_entity_name = True
    # Writable platforms set this so a field that reports a non-"none"
    # unavailable_reason is treated as unavailable even when it has no (or an
    # unreliable) .availability/.visibility companion. Read-only diagnostics
    # leave it False so they keep showing their value while restricted.
    _gate_on_unavailable_reason: bool = False

    def __init__(self, coordinator: BraviaTvCoordinator, grpc_path: str) -> None:
        super().__init__(coordinator)
        self._grpc_path = grpc_path
        entry = coordinator.config_entry
        unique_root = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{unique_root}_{grpc_path}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_root)},
            manufacturer=entry.data.get(CONF_MANUFACTURER, "Sony"),
            model=entry.data.get(CONF_MODEL),
            model_id=entry.data.get(CONF_MODEL_ID),
            sw_version=entry.data.get(CONF_SW_VERSION),
            name=coordinator.device_name,
        )

    @property
    def available(self) -> bool:
        """Reflect the field's own availability/visibility flags when present.

        Some fields are conditionally usable (e.g. tv_speaker / bluetooth volume
        only when that output is active). The device exposes ``<path>.availability``
        and/or ``<path>.visibility`` companions that latch False when the field
        isn't currently controllable — for the per-output volumes ``.visibility``
        is the reliable gate (``.availability`` can stay True). If either is
        explicitly False, treat the entity as unavailable.

        For writable entities we additionally honour ``<path>.unavailable_reason``:
        many fields advertise gates there (e.g. brightness under power saving,
        picture_mode locked by an app) that only sometimes flip an availability
        companion, so a non-"none" reason on a control means the write would be a
        silent no-op — surface it as unavailable instead.
        """
        if not super().available:
            return False
        data = self.coordinator.data or {}
        for suffix in (".availability", ".visibility"):
            if data.get(f"{self._grpc_path}{suffix}") is False:
                return False
        if self._gate_on_unavailable_reason:
            reason = data.get(f"{self._grpc_path}.unavailable_reason")
            if reason not in (None, "", "none"):
                return False
        return True

    def state_value(self, path: str) -> Any:
        """Current value of any gRPC path (for entities that read paths other
        than their own), or None if unknown."""
        return (self.coordinator.data or {}).get(path)

    @property
    def _value(self) -> Any:
        """Current value for this entity's gRPC path, or None if unknown."""
        return self.state_value(self._grpc_path)

    async def _async_set(self, value: Any) -> None:
        await self.coordinator.async_set_field(self._grpc_path, value)
