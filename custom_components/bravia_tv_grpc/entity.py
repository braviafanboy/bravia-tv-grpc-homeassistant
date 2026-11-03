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
from .coordinator import BraviaTvCoordinator, agent_path, cloud_path
from .grpc import cloud_commands as cloud


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


class BraviaTvCloudEntity(BraviaTvEntity):
    """Base for controls that only exist on the TV's Sony cloud capability set.

    State lives under ``cloud.<name>`` in the coordinator data (seeded from the
    cloud snapshot, refreshed from the TV's report after each command). Writes
    go through the Sony cloud, which the TV only acts on while it is powered
    on, so the entity is unavailable while the TV is off.
    """

    def __init__(self, coordinator: BraviaTvCoordinator, cloud_name: str) -> None:
        super().__init__(coordinator, cloud_path(cloud_name))
        self._cloud_name = cloud_name

    @property
    def available(self) -> bool:
        return super().available and bool(self.state_value("power"))

    async def _async_cloud_set(self, value: Any) -> None:
        """``set`` this capability via the cloud and reflect it optimistically."""
        await self.coordinator.async_cloud_command(
            self._cloud_name, [cloud.set_command(self._cloud_name, value)]
        )
        self.coordinator.set_cloud_value(self._cloud_name, value)


class BraviaTvAgentEntity(BraviaTvEntity):
    """Base for entities fed by the HA TV Agent (the companion app).

    State lives under ``agent.<block>`` in the coordinator data and arrives
    over the agent's event stream. The agent is only reachable while the TV
    is on, so the entity is unavailable in standby and whenever the stream is
    down (``agent.online`` false).
    """

    def __init__(self, coordinator: BraviaTvCoordinator, block: str, key: str) -> None:
        super().__init__(coordinator, agent_path(block))
        self._block = block
        entry = coordinator.config_entry
        root = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{root}_agent_{key}"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.agent_online
            and bool(self.state_value("power"))
        )

    def agent_value(self, *keys: str) -> Any:
        """A value from this entity's agent block (nested by ``keys``)."""
        return self.coordinator.agent_value(self._block, *keys)
