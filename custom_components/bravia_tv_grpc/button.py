"""Button entities for the Bravia TV (gRPC) integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvAgentEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.agent_expected:
        async_add_entities([BraviaTvAgentSetupButton(coordinator)])


class BraviaTvAgentSetupButton(BraviaTvAgentEntity, ButtonEntity):
    """Opens the agent's permission screens on the TV (Notification access
    for now-playing, Bluetooth for the remote's battery, Install unknown apps
    for updates). Reports instead of touching the TV when nothing is missing."""

    _attr_translation_key = "agent_setup"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, "display", "setup")

    @property
    def available(self) -> bool:
        # Usable whenever the TV is on; the agent need not be online yet.
        return bool(self.state_value("power"))

    async def async_press(self) -> None:
        await self.coordinator.async_agent_setup()
