"""Text platform for the Bravia TV (gRPC) integration."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvEntity

_FRIENDLY_NAME = "system_setting.friendly_name"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Bravia TV text entities from advertised capabilities."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    if _FRIENDLY_NAME in coordinator.client.capabilities:
        async_add_entities([BraviaTvFriendlyNameText(coordinator)])


class BraviaTvFriendlyNameText(BraviaTvEntity, TextEntity):
    """The TV's display / network name."""

    _gate_on_unavailable_reason = True
    _attr_translation_key = "name"
    _attr_icon = "mdi:tag"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_max = 64

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _FRIENDLY_NAME)

    @property
    def native_value(self) -> str | None:
        value = self._value
        return value if isinstance(value, str) else None

    async def async_set_value(self, value: str) -> None:
        await self._async_set(value)
