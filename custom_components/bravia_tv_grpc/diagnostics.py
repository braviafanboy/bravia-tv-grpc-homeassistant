"""Diagnostics support for the Bravia TV (gRPC) integration.

Dumps the (redacted) config entry, the device's advertised capability schema,
the current coordinator state, and the installed-app list — enough to debug the
reverse-engineered wire behaviour from a bug report without any live probing.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_GRPC_DEVICE_ID, CONF_GRPC_KEYS, DOMAIN
from .coordinator import BraviaTvCoordinator

# Secrets and personally-identifying values. Covers both config-entry keys and
# the gRPC state paths (serial number) that appear in coordinator data.
TO_REDACT = {
    CONF_GRPC_KEYS,
    CONF_GRPC_DEVICE_ID,
    "device_id",
    "hmac_key",
    "session_key",
    "key_id",
    "refresh_token",
    "access_token",
    "serial_number",
    "system_setting.serial_number",
    "unique_id",
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
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
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
        "state": async_redact_data(dict(coordinator.data or {}), TO_REDACT),
        "app_list": [
            {"id": app.get("id"), "label": app.get("label")}
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
            "name": device.name,
            "model": device.model,
            "manufacturer": device.manufacturer,
            "sw_version": device.sw_version,
        },
        **_diagnostics(coordinator, entry),
    }
