"""Remote platform for the Bravia TV (gRPC) integration.

Exposes the TV's ``remote_key`` actions through Home Assistant's
``remote.send_command`` -- the Sony on-screen shortcut menus (Quick Settings,
Audio, Guide, ...), D-pad navigation and relative channel change -- mirroring
how the Android TV Remote integration is driven. On/off mirrors power.

Only commands whose gRPC path the device advertises are accepted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import logging
from typing import Any

from homeassistant.components.remote import (
    ATTR_DELAY_SECS,
    ATTR_NUM_REPEATS,
    DEFAULT_DELAY_SECS,
    DEFAULT_NUM_REPEATS,
    RemoteEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvEntity

_LOGGER = logging.getLogger(__name__)

_POWER = "power"

# remote.send_command name -> (gRPC path, exec value). The enum keys send their
# advertised value strings; skip_channels takes a relative int (+1 / -1).
_COMMANDS: dict[str, tuple[str, Any]] = {
    # Sony on-screen shortcut menus.
    "tv": ("remote_key.shortcut", "tv"),
    "guide": ("remote_key.shortcut", "guide"),
    "app_menu": ("remote_key.shortcut", "app_menu"),
    "menu": ("remote_key.shortcut", "menu"),
    "quick_settings": ("remote_key.shortcut", "quick_settings"),
    "audio": ("remote_key.shortcut", "audio"),
    # D-pad navigation.
    "up": ("remote_key.d_pad", "up"),
    "down": ("remote_key.d_pad", "down"),
    "left": ("remote_key.d_pad", "left"),
    "right": ("remote_key.d_pad", "right"),
    "center": ("remote_key.d_pad", "center"),
    "back": ("remote_key.d_pad", "back"),
    "home": ("remote_key.d_pad", "home"),
    # Relative channel change.
    "channel_up": ("remote_key.skip_channels", 1),
    "channel_down": ("remote_key.skip_channels", -1),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Bravia TV remote if any remote_key action is advertised."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    caps = coordinator.client.capabilities
    commands = {
        name: mapping for name, mapping in _COMMANDS.items() if mapping[0] in caps
    }
    if commands:
        async_add_entities([BraviaTvRemote(coordinator, commands)])


class BraviaTvRemote(BraviaTvEntity, RemoteEntity):
    """Sends the TV's remote_key actions via remote.send_command."""

    _attr_translation_key = "remote"

    def __init__(
        self, coordinator: BraviaTvCoordinator, commands: dict[str, tuple[str, Any]]
    ) -> None:
        super().__init__(coordinator, "remote_key")
        self._commands = commands

    @property
    def is_on(self) -> bool | None:
        power = (self.coordinator.data or {}).get(_POWER)
        return None if power is None else bool(power)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_field(_POWER, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_field(_POWER, False)

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        """Send one or more remote_key commands (see _COMMANDS for names)."""
        num_repeats = kwargs.get(ATTR_NUM_REPEATS, DEFAULT_NUM_REPEATS)
        delay = kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS)
        sequence = list(command) * num_repeats
        for i, cmd in enumerate(sequence):
            mapping = self._commands.get(cmd)
            if mapping is None:
                _LOGGER.warning(
                    "Unknown Bravia remote command %r (valid: %s)",
                    cmd,
                    ", ".join(sorted(self._commands)),
                )
                continue
            path, value = mapping
            await self.coordinator.async_send_key(path, value)
            if delay and i < len(sequence) - 1:
                await asyncio.sleep(delay)
