"""Button entities for the Bravia TV (gRPC) integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ambient
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
        async_add_entities(
            [
                BraviaTvAgentSetupButton(coordinator),
                BraviaTvFindRemoteButton(coordinator),
                BraviaTvSaveProfileButton(coordinator),
                BraviaTvRestoreProfileButton(coordinator),
                BraviaTvDeleteProfileButton(coordinator),
                ambient.BraviaTvAmbientResetButton(coordinator),
            ]
        )


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


class BraviaTvFindRemoteButton(BraviaTvAgentEntity, ButtonEntity):
    """Makes the TV's remote beep, as the TV's own Find remote does (through
    the HA TV Agent, over Bluetooth). Works while the TV is on."""

    _attr_translation_key = "find_remote"

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, "remote_finder", "find_remote")

    async def async_press(self) -> None:
        await self.coordinator.async_find_remote()


class BraviaTvSaveProfileButton(BraviaTvAgentEntity, ButtonEntity):
    """Saves the TV's current picture settings as a profile, under the name in
    the New profile name text box. Works while the TV is on."""

    _attr_translation_key = "save_picture_profile"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, "display", "save_picture_profile")

    async def async_press(self) -> None:
        await self.coordinator.async_save_picture_profile_from_input()


class BraviaTvRestoreProfileButton(BraviaTvAgentEntity, ButtonEntity):
    """Restores the picture profile chosen in the Picture profile select, writing
    its settings back to the TV. Works while the TV is on."""

    _attr_translation_key = "restore_picture_profile"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, "display", "restore_picture_profile")

    async def async_press(self) -> None:
        await self.coordinator.async_restore_selected_picture_profile()


class BraviaTvDeleteProfileButton(BraviaTvAgentEntity, ButtonEntity):
    """Deletes the picture profile chosen in the Picture profile select."""

    _attr_translation_key = "delete_picture_profile"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, "display", "delete_picture_profile")

    async def async_press(self) -> None:
        await self.coordinator.async_delete_selected_picture_profile()
