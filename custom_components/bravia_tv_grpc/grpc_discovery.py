"""Discover the BRAVIA TV gRPC ControlDeviceService port.

BRAVIA TVs serve the service on a DYNAMIC port that changes across reboots.
TCP probe helpers come from ``pybravia-connect``; mDNS browsing stays here
because it needs Home Assistant's zeroconf integration.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING

from pybravia_connect import discover_grpc_port, is_control_device_service
from pybravia_connect.discovery import ZEROCONF_TYPE, scan_open_ports

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "ZEROCONF_TYPE",
    "async_discover_port_mdns",
    "async_resolve_device_mdns",
    "discover_grpc_port",
    "is_control_device_service",
    "scan_open_ports",
]


async def _async_find_service_info(hass: HomeAssistant, host: str, timeout: float):
    """Browse ``_sonysmarthome._tcp`` and return the AsyncServiceInfo whose
    address matches ``host`` (or None). Returns on the first match rather than
    always waiting the full timeout."""
    from homeassistant.components import zeroconf as ha_zeroconf
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

    # async_get_async_zeroconf became a sync @callback in newer HA cores (it
    # used to be a coroutine); support both so neither version breaks the flow.
    aiozc = ha_zeroconf.async_get_async_zeroconf(hass)
    if inspect.isawaitable(aiozc):
        aiozc = await aiozc
    names: list[tuple[str, str]] = []

    def _handler(zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Added:
            names.append((service_type, name))

    browser = AsyncServiceBrowser(aiozc.zeroconf, ZEROCONF_TYPE, handlers=[_handler])
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen: set[tuple[str, str]] = set()
    try:
        while loop.time() < deadline:
            for entry in [n for n in names if n not in seen]:
                seen.add(entry)
                service_type, name = entry
                info = AsyncServiceInfo(service_type, name)
                if await info.async_request(aiozc.zeroconf, 2000) and host in (
                    info.parsed_addresses()
                ):
                    return info
            await asyncio.sleep(0.2)
    finally:
        await browser.async_cancel()
    return None


async def async_discover_port_mdns(
    hass: HomeAssistant, host: str, timeout: float = 5.0
) -> int | None:
    """Resolve the gRPC port for ``host`` via mDNS (``_sonysmarthome._tcp``).

    Fast and reliable; avoids the TCP scan. Returns None if the TV is not
    currently advertising (e.g. just after a reboot) so callers can fall back.
    """
    info = await _async_find_service_info(hass, host, timeout)
    if info is not None:
        _LOGGER.debug("mDNS resolved %s -> port %s", host, info.port)
        return info.port
    return None


async def async_resolve_device_mdns(
    hass: HomeAssistant, host: str, timeout: float = 5.0
) -> tuple[int, str, dict[str, str]] | None:
    """Return ``(port, instance_name, txt_properties)`` for the Sony device at
    ``host``, or None.

    Lets the manual (IP) config-flow bind the entered address to the *exact*
    Sony device — the instance name carries its unique id — so accounts with
    more than one TV pair the right one, and a soundbar can be rejected.
    """
    info = await _async_find_service_info(hass, host, timeout)
    if info is None:
        return None
    props: dict[str, str] = {}
    for key, value in (info.properties or {}).items():
        k = key.decode() if isinstance(key, bytes) else str(key)
        if isinstance(value, bytes):
            v = value.decode(errors="replace")
        else:
            v = "" if value is None else str(value)
        props[k] = v
    return info.port, info.name or "", props
