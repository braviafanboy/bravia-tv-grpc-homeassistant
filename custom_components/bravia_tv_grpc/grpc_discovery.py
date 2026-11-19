"""Discover the BRAVIA TV gRPC ControlDeviceService port.

BRAVIA TVs serve the service on a DYNAMIC port that changes across reboots. We
locate it by probing candidate TCP ports and confirming the service by RPC
behaviour: a real method on the
ControlDeviceService path returns INVALID_ARGUMENT for an empty payload, while
any other HTTP/2 server returns UNIMPLEMENTED (or fails to speak gRPC at all).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import errno
import inspect
from itertools import chain
import logging
import socket
import time
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

# The TV's dynamic port has only ever been seen in the Linux ephemeral range,
# so that is scanned first; the rest of the unprivileged ports follow, each
# port once.
EPHEMERAL_PORTS = range(32768, 61000)
OTHER_PORTS = tuple(chain(range(1024, 32768), range(61000, 65536)))
# A scan is the last resort after the cached port and mDNS. It is repeated at
# most this often per TV: sooner while the TV's address answers at all (a scan
# is then quick: closed ports refuse at once), rarely while it is silent (every
# port would wait out its timeout, and a silent TV has no port to find).
SCAN_INTERVAL_ALIVE = 15 * 60
SCAN_INTERVAL_SILENT = 6 * 60 * 60
_DATA_SCANS = "bravia_tv_grpc_port_scans"
# Ports probed to tell whether the TV's address answers at all: its REST API,
# Cast, and the HA TV Agent. A refusal counts as an answer.
_ALIVE_PROBE_PORTS = (80, 8008, 8585, 443)
_SILENT_ERRNOS = {
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.EHOSTDOWN,
    errno.ETIMEDOUT,
}


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def host_answers(
    host: str, ports: Iterable[int] = _ALIVE_PROBE_PORTS, timeout: float = 0.7
) -> bool:
    """Whether anything at ``host`` answers TCP at all: a connection, or a
    refusal. Silence on every probed port (timeouts, no route) says the TV is
    off the network, where a port scan could only wait."""
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except ConnectionRefusedError:
            return True
        except TimeoutError:
            continue
        except OSError as err:
            if err.errno in _SILENT_ERRNOS or err.errno is None:
                continue
            return True  # some other answer from the network stack
    return False


def scan_open_ports(
    host: str, ports: Iterable[int], timeout: float = 0.3, workers: int = 200
) -> list[int]:
    """Return the subset of ``ports`` accepting TCP connections on ``host``.

    Closed ports refuse immediately; only filtered ports wait the full timeout.
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


def discover_grpc_port(
    host: str,
    candidate_ports: tuple[int, ...] = (),
    scan_range: Iterable[int] | None = None,
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


def scan_for_grpc_port(host: str) -> int | None:
    """Scan ``host`` for the control service: the ephemeral range first, then
    the remaining unprivileged ports (each port once)."""
    return discover_grpc_port(host, (), EPHEMERAL_PORTS) or discover_grpc_port(
        host, (), OTHER_PORTS
    )


async def async_resolve_port(
    hass: HomeAssistant,
    host: str,
    cached: int | None,
    manual: int | None = None,
    *,
    hint: int | None = None,
    scan_key: str | None = None,
) -> int | None:
    """Locate the dynamic gRPC port: manual -> hint -> cached -> mDNS -> scan.

    ``hint`` is a port mDNS just announced for this TV. ``scan_key`` (the
    config entry id) rate-limits the scan across attempts; without one the
    scan always runs (the config flow, where the user is waiting)."""
    run = hass.async_add_executor_job
    # 0. Manual override from options — try it first (escape hatch for networks
    #    where discovery is blocked). If it no longer serves (TV rebooted onto a
    #    new port), fall through to normal discovery rather than failing.
    seen: set[int] = set()
    for candidate in (manual, hint, cached):
        # 1. The port mDNS announced, then the one from the previous run —
        #    instant when the TV hasn't rebooted.
        if candidate and candidate not in seen:
            seen.add(candidate)
            if await run(is_control_device_service, host, candidate):
                return candidate
    # 2. mDNS (_sonysmarthome._tcp) — fast, handles a changed port after reboot.
    #    Best-effort: any zeroconf hiccup must fall through to the scan, never
    #    fail the caller.
    try:
        port = await async_discover_port_mdns(hass, host)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("mDNS port discovery failed; falling back to scan", exc_info=True)
        port = None
    if port and port not in seen and await run(is_control_device_service, host, port):
        return port
    # 3. TCP scan — last resort, for networks where mDNS does not get through.
    if scan_key is not None:
        scans: dict[tuple[str, bool], float] = hass.data.setdefault(_DATA_SCANS, {})
        alive = await run(host_answers, host)
        last = scans.get((scan_key, alive))
        now = time.monotonic()
        if alive:
            if last is not None and now - last < SCAN_INTERVAL_ALIVE:
                return None
        elif last is None or now - last < SCAN_INTERVAL_SILENT:
            # Nothing answers at that address, so a scan would only wait out
            # every port. Note when that was first seen, and scan much later
            # in case the TV merely ignores the ports probed.
            scans.setdefault((scan_key, alive), now)
            return None
        scans[(scan_key, alive)] = now
    return await run(scan_for_grpc_port, host)
