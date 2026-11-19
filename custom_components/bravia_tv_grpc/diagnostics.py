"""Diagnostics support for the Bravia TV (gRPC) integration.

Dumps the (redacted) config entry, the device's advertised capability schema,
the current coordinator state, and the installed-app list — enough to debug the
reverse-engineered wire behaviour from a bug report without any live probing.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_GRPC_DEVICE_ID, CONF_GRPC_KEYS, DOMAIN
from .coordinator import BraviaTvCoordinator

# Secrets and personally-identifying values. Covers config-entry keys, the
# gRPC state paths that carry identifiers or names people chose, and the keys
# inside the agent's blocks that say what is playing or which devices are
# paired. Diagnostics get attached to public issues: anything that says who
# or where must not be in them. A redacted key is redacted whole, which also
# covers the values the TV sends as JSON text.
TO_REDACT = {
    CONF_GRPC_KEYS,
    CONF_GRPC_DEVICE_ID,
    CONF_HOST,
    "device_id",
    "hmac_key",
    "session_key",
    "key_id",
    "refresh_token",
    "access_token",
    "serial_number",
    "system_setting.serial_number",
    "unique_id",
    "device_unique_id",
    "agent_token",
    "rest_psk",
    "agent_cert",
    # Who the agent says it is paired with: a name, and this Home Assistant's
    # own address on the network.
    "paired_with",
    "paired_from",
    # Names the user chose: the TV's own, the entry's (it holds the address).
    "system_setting.friendly_name",
    "title",
    "name",
    # What is playing.
    "artist",
    "album",
    "artwork_url",
    # Paired and connected devices: their names and ids, as JSON text.
    "sound_setting.volume.bluetooth.device_info",
    "sound_setting.audio_output.available_devices",
    "sound_setting.audio_output.active_devices",
    "hdmi_connected_device.sshinfo",
    # Paired wireless-speaker identifiers (surfaced as sensor attributes).
    *(
        f"speaker_connection_setting.{field}.{pos}"
        for field in ("serial_number", "wifi_mac_address")
        for pos in ("rl", "rr", "sw")
    ),
}


def _diagnostics(
    coordinator: BraviaTvCoordinator, entry: ConfigEntry
) -> dict[str, Any]:
    """Build the shared (redacted) diagnostics payload."""
    caps = coordinator.client.capabilities
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "connection": {
            "offline": coordinator.offline,
            "connected": coordinator.client.connected,
        },
        "capabilities": {
            path: {
                "type": meta.type,
                "min": meta.min,
                "max": meta.max,
                "values": list(meta.values) if meta.values else None,
            }
            for path, meta in sorted(caps.items())
        },
        "cloud_capabilities": {
            name: list(commands)
            for name, commands in sorted(coordinator.cloud_capabilities.items())
        },
        "agent": {
            "present": coordinator.agent is not None,
            "online": coordinator.agent_online,
            "info": async_redact_data(dict(coordinator.agent_info), TO_REDACT),
            "pinned_fingerprint": coordinator.agent.fingerprint
            if coordinator.agent
            else None,
            "rest": async_redact_data(coordinator.agent_value("rest") or {}, TO_REDACT),
            "picture_targets": sorted(
                (coordinator.agent_value("picture", "targets") or {}).keys()
            ),
        },
        "state": async_redact_data(dict(coordinator.data or {}), TO_REDACT),
        "app_list": [
            {
                "id": app.get("id"),
                "label": app.get("label"),
                "resource_types": [
                    r.get("type")
                    for r in (app.get("resources") or [])
                    if isinstance(r, dict)
                ],
            }
            for app in coordinator.app_list
        ],
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    return _diagnostics(coordinator, entry)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device (this integration has one device/entry)."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "device": {
            "model": device.model,
            "manufacturer": device.manufacturer,
            "sw_version": device.sw_version,
        },
        **_diagnostics(coordinator, entry),
    }
