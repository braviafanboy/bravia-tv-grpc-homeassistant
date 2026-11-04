"""Base entity for the Bravia TV (gRPC) integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import picture
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


class BraviaTvPictureEntity(BraviaTvAgentEntity):
    """A picture-quality target from the TV's REST IP control, relayed by the
    agent (``agent.picture.targets.<target>``). Named after the target;
    unavailable while IP control does not answer or the TV marks the target
    unavailable (e.g. HDMI-only settings on an app, or the Ambient Optimization
    sub-switches while the master switch is off)."""

    _attr_translation_key = "picture_setting"

    def __init__(self, coordinator: BraviaTvCoordinator, target: str) -> None:
        super().__init__(coordinator, "picture", f"picture_{target}")
        self.target = target
        self._attr_translation_placeholders = {"setting": picture.label(target)}

    @property
    def info(self) -> dict[str, Any]:
        value = self.agent_value("targets", self.target)
        return value if isinstance(value, dict) else {}

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.agent_value("rest", "available") is True
            and bool(self.info)
            and self.info.get("available") is True
        )

    @property
    def raw_value(self) -> str | None:
        value = self.info.get("value")
        return value if isinstance(value, str) and value != "" else None

    async def async_write(self, value: str) -> None:
        await self.coordinator.async_set_picture(self.target, value)


@callback
def async_add_picture_entities(
    coordinator: BraviaTvCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[BraviaTvCoordinator, str, str], Entity | None],
) -> None:
    """Add picture entities as the agent reports targets: now, and whenever a
    target appears later (the agent connects after setup, a new firmware adds
    one). ``factory(coordinator, target, kind)`` returns the entity for the
    platform, or None to skip it. Picture entities belong to a paired agent;
    their targets arrive once its stream is up, which may be after setup."""
    if not coordinator.agent_expected:
        return
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        targets = coordinator.agent_value("picture", "targets")
        if not isinstance(targets, dict):
            return
        new: list[Entity] = []
        for target, info in targets.items():
            if target in added:
                continue
            if target in picture.EXCLUDED:
                added.add(target)
                continue
            kind = picture.kind_of(target, info if isinstance(info, dict) else None)
            entity = factory(coordinator, target, kind)
            if entity is not None:
                added.add(target)
                new.append(entity)
            elif kind is not None:
                # Decided, and it belongs to another platform.
                added.add(target)
        if new:
            async_add_entities(new)
            # The watch list (what the agent polls at short intervals) is
            # built from the registry; send it again now these exist.
            coordinator.hass.async_create_task(
                coordinator.async_agent_resync_rest(),
                name="bravia_tv_picture_resync",
            )

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))
