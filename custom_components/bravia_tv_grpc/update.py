"""Update platform for the Bravia TV (gRPC) integration.

Surfaces the TV firmware update state in Home Assistant's Updates dashboard.
Whether an update exists comes from ``fw_update.update_exist`` and the available
version from ``fw_update.info``; the currently-installed version isn't in the
gRPC schema, so it comes from the Sony cloud device info captured at setup.
Installing is deliberately not offered — ``fw_update.start_update`` is a
disruptive, irreversible flash best left to the TV's own update UI.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_SW_VERSION, DOMAIN
from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvEntity
from .util import parse_json_value

_FW_UPDATE = "fw_update.update_exist"
_FW_INFO = "fw_update.info"
_FW_START_AVAIL = "fw_update.start_update_availability"
# start_update_availability enum values that mean an update is being applied
# (anything other than the idle "available").
_IN_PROGRESS_STATES = {"update_ongoing", "wait_for_reboot", "merging"}
_RELEASE_NOTES_TIMEOUT = 10
_RELEASE_NOTES_MAX_CHARS = 8000

_LOGGER = logging.getLogger(__name__)


def _html_to_text(markup: str) -> str:
    """Reduce a release-notes page to readable text (headings, lists, breaks)."""
    text = re.sub(r"(?is)<(script|style|head|nav|footer)\b.*?</\1>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|section|article)>", "\n", text)
    text = re.sub(r"(?i)<li\b[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Bravia TV firmware update entity if advertised."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    if _FW_UPDATE in coordinator.client.capabilities:
        async_add_entities([BraviaTvFirmwareUpdate(coordinator)])


class BraviaTvFirmwareUpdate(BraviaTvEntity, UpdateEntity):
    """TV firmware update state (informational; install left to the TV)."""

    _attr_translation_key = "firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.RELEASE_NOTES

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _FW_UPDATE)

    @property
    def entity_picture(self) -> str | None:
        # UpdateEntity defaults this to the HA brands CDN icon for the
        # integration domain, which 404s for a custom integration. Fall back to
        # the frontend's built-in update/firmware icon instead of a broken image.
        return None

    def _info(self) -> dict:
        data = parse_json_value(self.state_value(_FW_INFO))
        return data if isinstance(data, dict) else {}

    @property
    def installed_version(self) -> str | None:
        # Not in the gRPC schema; taken from the Sony cloud device info captured
        # at setup (see __init__._ensure_device_info). Accurate at pairing and
        # while an update is pending; can briefly lag right after an update is
        # applied, until the device info is re-fetched.
        return self.coordinator.config_entry.data.get(CONF_SW_VERSION)

    @property
    def latest_version(self) -> str | None:
        # update_exist is the authoritative "an update is available" flag: report
        # the available version then, else the installed version so the entity
        # reads "up to date".
        if not self.state_value(_FW_UPDATE):
            return self.installed_version
        return self._info().get("version") or "Available update"

    @property
    def in_progress(self) -> bool:
        # fw_update.start_update_availability reports the update mechanism state;
        # anything past idle ("available") means a firmware update is applying.
        # Note: this path isn't push-notified, so it refreshes on (re)connect
        # rather than live — but a firmware install reboots the TV, which forces
        # a reconnect + reseed, so the "applying/waiting for reboot" phases are
        # picked up in practice.
        return self.state_value(_FW_START_AVAIL) in _IN_PROGRESS_STATES

    @property
    def release_url(self) -> str | None:
        return self._info().get("url") or None

    async def async_release_notes(self) -> str | None:
        """Fetch the TV's release-notes page and return it as plain text.

        The TV only carries the URL (Sony's page for the pending version), so
        this is best effort: None when there is no pending update or the page
        can't be fetched.
        """
        url = self.release_url
        if not url:
            return None
        try:
            async with asyncio.timeout(_RELEASE_NOTES_TIMEOUT):
                session = async_get_clientsession(self.hass)
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    body = await resp.text()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("release notes fetch failed for %s", url, exc_info=True)
            return None
        text = _html_to_text(body)
        return text[:_RELEASE_NOTES_MAX_CHARS] or None
