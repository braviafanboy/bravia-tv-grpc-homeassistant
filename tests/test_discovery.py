"""Finding the TV's dynamic gRPC port: the order things are tried in, and
when a port scan is (not) run (require HA test harness)."""

from __future__ import annotations

import errno
import socket
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.bravia_tv_grpc import grpc_discovery as disc  # noqa: E402

MOD = "custom_components.bravia_tv_grpc.grpc_discovery"


class _Network:
    """What the lookups find: which ports serve the TV, what mDNS says,
    whether the address answers at all, and what a scan would turn up."""

    def __init__(self) -> None:
        self.serving: set[int] = set()
        self.mdns: int | None = None
        self.alive = True
        self.scan_result: int | None = None
        self.probed: list[int] = []
        self.scans = 0

    def _serves(self, host: str, port: int, timeout: float = 3.0) -> bool:
        self.probed.append(port)
        return port in self.serving

    def _scan(self, host: str) -> int | None:
        self.scans += 1
        return self.scan_result

    def __enter__(self) -> _Network:
        self._patches = [
            patch(f"{MOD}.is_control_device_service", side_effect=self._serves),
            patch(
                f"{MOD}.async_discover_port_mdns",
                new=AsyncMock(side_effect=lambda hass, host: self.mdns),
            ),
            patch(f"{MOD}.host_answers", side_effect=lambda host: self.alive),
            patch(f"{MOD}.scan_for_grpc_port", side_effect=self._scan),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        for p in reversed(self._patches):
            p.stop()


async def test_order_manual_hint_cached_mdns(hass: HomeAssistant):
    with _Network() as net:
        net.serving = {1, 2, 3, 4}
        net.mdns = 4
        resolve = disc.async_resolve_port
        assert await resolve(hass, "tv", 3, 1, hint=2, scan_key="e") == 1
        net.serving.discard(1)  # the manual port no longer serves: fall through
        assert await resolve(hass, "tv", 3, 1, hint=2, scan_key="e") == 2
        net.serving.discard(2)
        assert await resolve(hass, "tv", 3, 1, hint=2, scan_key="e") == 3
        net.serving.discard(3)
        assert await resolve(hass, "tv", 3, 1, hint=2, scan_key="e") == 4
        assert net.scans == 0
        # The same port is not probed twice in one lookup.
        net.probed.clear()
        net.serving.clear()
        net.mdns = 3
        await resolve(hass, "tv", 3, 3, hint=3, scan_key="e")
        assert net.probed == [3]


async def test_mdns_failure_falls_through_to_the_scan(hass: HomeAssistant):
    with _Network() as net:
        net.scan_result = 40000
        with patch(
            f"{MOD}.async_discover_port_mdns",
            new=AsyncMock(side_effect=RuntimeError("zeroconf hiccup")),
        ):
            assert (
                await disc.async_resolve_port(hass, "tv", None, scan_key="e") == 40000
            )


async def test_scan_is_rate_limited_per_tv(hass: HomeAssistant):
    """A TV whose address answers but whose port is not found (mDNS blocked,
    the TV still booting) is scanned, then left alone for a while: a scan per
    attempt is a port scan every few seconds."""
    with _Network() as net, patch(f"{MOD}.time.monotonic") as clock:
        clock.return_value = 1000.0
        assert await disc.async_resolve_port(hass, "tv", 5, scan_key="a") is None
        assert net.scans == 1
        for _ in range(5):
            assert await disc.async_resolve_port(hass, "tv", 5, scan_key="a") is None
        assert net.scans == 1
        # Another TV has its own allowance.
        await disc.async_resolve_port(hass, "tv2", 5, scan_key="b")
        assert net.scans == 2
        clock.return_value = 1000.0 + disc.SCAN_INTERVAL_ALIVE + 1
        net.scan_result = 41000
        assert await disc.async_resolve_port(hass, "tv", 5, scan_key="a") == 41000
        assert net.scans == 3


async def test_silent_address_is_not_scanned(hass: HomeAssistant):
    """A TV in standby is off the network: every one of ~64,000 ports would
    wait out its timeout, on every attempt. Nothing is scanned until the
    address has been silent for hours (in case the TV merely ignores the
    ports probed), and a TV that answers again is scanned at once."""
    with _Network() as net, patch(f"{MOD}.time.monotonic") as clock:
        net.alive = False
        clock.return_value = 0.0
        for step in range(10):
            clock.return_value = step * 60.0
            assert await disc.async_resolve_port(hass, "tv", 5, scan_key="a") is None
        assert net.scans == 0
        clock.return_value = disc.SCAN_INTERVAL_SILENT + 1
        await disc.async_resolve_port(hass, "tv", 5, scan_key="a")
        assert net.scans == 1
        clock.return_value += 60
        await disc.async_resolve_port(hass, "tv", 5, scan_key="a")
        assert net.scans == 1
        # It answers again: its own, shorter allowance starts fresh.
        net.alive = True
        net.scan_result = 42000
        assert await disc.async_resolve_port(hass, "tv", 5, scan_key="a") == 42000
        assert net.scans == 2


async def test_without_a_key_the_scan_always_runs(hass: HomeAssistant):
    """The config flow: the user is waiting, and it runs once."""
    with _Network() as net:
        net.alive = False
        net.scan_result = 43000
        assert await disc.async_resolve_port(hass, "tv", None) == 43000
        assert await disc.async_resolve_port(hass, "tv", None) == 43000
        assert net.scans == 2


def test_scan_ranges_cover_every_port_once():
    ports = list(disc.EPHEMERAL_PORTS) + list(disc.OTHER_PORTS)
    assert len(ports) == len(set(ports)) == 65536 - 1024
    assert min(ports) == 1024 and max(ports) == 65535


def test_scan_tries_the_ephemeral_range_first():
    calls: list = []

    def discover(host, candidates=(), scan_range=None):
        calls.append(scan_range)
        return 50000 if scan_range is disc.OTHER_PORTS else None

    with patch(f"{MOD}.discover_grpc_port", side_effect=discover):
        assert disc.scan_for_grpc_port("tv") == 50000
    assert calls == [disc.EPHEMERAL_PORTS, disc.OTHER_PORTS]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("connects", True),
        (ConnectionRefusedError(), True),  # a closed port still answers
        (OSError(errno.ECONNRESET, "reset"), True),
        (TimeoutError(), False),
        (OSError(errno.EHOSTUNREACH, "no route"), False),
        (OSError(errno.ENETUNREACH, "no net"), False),
        (socket.gaierror("no such host"), False),
    ],
)
def test_host_answers(outcome, expected):
    attempts: list[int] = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def connect(address, timeout=None):
        attempts.append(address[1])
        if outcome == "connects":
            return _Conn()
        raise outcome

    with patch(f"{MOD}.socket.create_connection", side_effect=connect):
        assert disc.host_answers("tv") is expected
    # Silence is only concluded after every probe port; an answer ends it.
    assert len(attempts) == (1 if expected else len(disc._ALIVE_PROBE_PORTS))
