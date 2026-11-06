"""Notify entity: show a message on the TV screen through the HA TV Agent.

The standard ``notify.send_message`` carries only a message and title. A richer
``bravia_tv_grpc.send_notification`` entity service adds an icon (chosen from a
list of native Sony icons) and a custom on-screen duration.
"""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import voluptuous as vol

from .const import (
    CONF_NOTIFY_DURATION,
    CONF_NOTIFY_ICON,
    DEFAULT_NOTIFY_ICON,
    DOMAIN,
    NOTIFY_ICON_OPTIONS,
)
from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvAgentEntity

MIN_DURATION = 1
MAX_DURATION = 120


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the on-screen message entity and its advanced service."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.agent_expected:
        return
    async_add_entities([BraviaTvNotifyEntity(coordinator)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "send_notification",
        {
            vol.Required("message"): cv.string,
            vol.Optional("title"): cv.string,
            vol.Optional("icon", default=DEFAULT_NOTIFY_ICON): vol.In(
                NOTIFY_ICON_OPTIONS
            ),
            vol.Optional("duration"): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_DURATION, max=MAX_DURATION)
            ),
        },
        "async_send_notification",
    )


class BraviaTvNotifyEntity(BraviaTvAgentEntity, NotifyEntity):
    """Shows a message on the TV as an on-screen banner (through the HA TV
    Agent, over whatever is playing). Works while the TV is on; needs the
    agent's "Display over other apps" permission."""

    _attr_translation_key = "on_screen_message"
    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, "display", "notify")

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        options = self.coordinator.config_entry.options
        icon = options.get(CONF_NOTIFY_ICON, DEFAULT_NOTIFY_ICON)
        duration = options.get(CONF_NOTIFY_DURATION) or None
        await self.coordinator.async_notify(
            message, title, icon=icon, duration=duration
        )

    async def async_send_notification(
        self,
        message: str,
        title: str | None = None,
        icon: str = DEFAULT_NOTIFY_ICON,
        duration: int | None = None,
    ) -> None:
        """Entity-service variant with a chosen icon and on-screen duration."""
        await self.coordinator.async_notify(
            message, title, icon=icon, duration=duration
        )
