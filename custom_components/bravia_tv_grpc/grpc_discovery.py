"""Discover the BRAVIA TV gRPC ControlDeviceService port.

BRAVIA TVs serve the service on a DYNAMIC port that changes across reboots. We
locate it by probing candidate TCP ports and confirming the service by RPC
behaviour: a real method on the
ControlDeviceService path returns INVALID_ARGUMENT for an empty payload, while
any other HTTP/2 server returns UNIMPLEMENTED (or fails to speak gRPC at all).
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING

import grpc

from .const import GRPC_SERVICE

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# The TV advertises the ControlDeviceService (dynamic port) under this mDNS
# service type — verified on a BRAVIA 8 II (=_sonysmarthome._tcp, port 32915).
ZEROCONF_TYPE = "_sonysmarthome._tcp.local."

# A registered method used to confirm a candidate port speaks the service.
_PROBE_METHOD = f"/{GRPC_SERVICE}/GetSessionRandom"


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_open_ports(
    host: str, ports: range, timeout: float = 0.3, workers: int = 500
) -> list[int]:
    """Return the subset of ``ports`` accepting TCP connections on ``host``.

    Closed ports refuse immediately; only filtered ports wait the full timeout,
    so a high worker count keeps a full-range scan to a few tens of seconds.
    """
    from concurrent.futures import ThreadPoolExecutor

    open_ports: list[int] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda p: (p, _port_open(host, p, timeout)), ports)
        open_ports = [p for p, is_open in results if is_open]
    return open_ports


def is_control_device_service(host: str, port: int, timeout: float = 3.0) -> bool:
    """Return True if ``host:port`` hosts the Sony ControlDeviceService.

    Distinguishes the real service (registered method → INVALID_ARGUMENT on an
    empty request) from an unrelated HTTP/2 server (→ UNIMPLEMENTED).
    """
    channel = grpc.insecure_channel(f"{host}:{port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=timeout)
        call = channel.unary_unary(_PROBE_METHOD)
        try:
            call(b"", timeout=timeout)
        except grpc.RpcError as err:
            code = err.code()
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                return True
            _LOGGER.debug("Port %s not the service: %s", port, code)
            return False
        # A success on an empty payload would be unexpected but still the service.
        return True
    except (grpc.FutureTimeoutError, grpc.RpcError):
        return False
    finally:
        channel.close()


async def _async_find_service_info(hass: HomeAssistant, host: str, timeout: float):
    """Browse ``_sonysmarthome._tcp`` and return the AsyncServiceInfo whose
    address matches ``host`` (or None). Returns on the first match rather than
    always waiting the full timeout."""
    from homeassistant.components import zeroconf as ha_zeroconf
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

    aiozc = await ha_zeroconf.async_get_async_zeroconf(hass)
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


def discover_grpc_port(
    host: str,
    candidate_ports: tuple[int, ...] = (),
    scan_range: range | None = None,
) -> int | None:
    """Locate the gRPC control port on ``host``.

    Tries ``candidate_ports`` first (e.g. the previously-resolved port), then
    (optionally) probes every open port in ``scan_range`` until one answers as
    the ControlDeviceService. The TV's port is dynamic, so there is no useful
    fixed default to probe.
    """
    for port in candidate_ports:
        if _port_open(host, port) and is_control_device_service(host, port):
            _LOGGER.info("Found ControlDeviceService on %s:%s", host, port)
            return port

    if scan_range is None:
        return None

    for port in scan_open_ports(host, scan_range):
        if port in candidate_ports:
            continue
        if is_control_device_service(host, port):
            _LOGGER.info("Discovered ControlDeviceService on %s:%s", host, port)
            return port
    return None
