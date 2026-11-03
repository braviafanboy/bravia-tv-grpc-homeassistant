"""Coordinator tests for the BRAVIA TV agent link (require HA harness)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.bravia_tv_grpc.const import (  # noqa: E402
    CONF_AGENT_TOKEN,
    CONF_GRPC_DEVICE_ID,
    CONF_GRPC_KEYS,
    DOMAIN,
)
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    AGENT_ONLINE,
    BraviaTvCoordinator,
    agent_path,
)
from custom_components.bravia_tv_grpc.grpc import agent as agentmod  # noqa: E402

COORD = "custom_components.bravia_tv_grpc.coordinator"
_KEYS = '{"device_id": "dev", "hmac_key": "ab"}'
_INFO = {"agent": {"version": "0.1.0", "contract": 1, "port": 8585}}
_DOC = {
    "environment": {"illuminance_lx": 12.0, "temperature_c": 22.5},
    "display": {"on": True},
    "media": {
        "active": True,
        "app": "a",
        "state": "playing",
        "title": "T",
        "position_s": 3.0,
    },
    "bluetooth": {
        "remote": {"name": "R", "connected": True, "battery_pct": 30},
        "devices": [],
    },
    "wifi": {"rssi_dbm": -50, "link_mbps": 400},
}

pytestmark = pytest.mark.agent_probe


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _client() -> MagicMock:
    client = MagicMock()
    client.capabilities = {}
    client.get_states.return_value = {"power": True, "volume": 10}
    client.read_application_list.return_value = []
    client.exec_command.return_value = True
    return client


def _entry(hass: HomeAssistant, **extra):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev",
        data={
            "host": "tv.local",
            CONF_GRPC_KEYS: _KEYS,
            CONF_GRPC_DEVICE_ID: "dev",
            **extra,
        },
    )
    entry.add_to_hass(hass)
    return entry


class FakeAgent:
    """Stand-in for agentmod.AgentClient driven by the test."""

    def __init__(self, session, host, port=8585, token=None):
        self.host = host
        self.port = port
        self.token = token
        self.info = dict(_INFO)
        self.state_doc = dict(_DOC)
        self.events: asyncio.Queue = asyncio.Queue()
        self.pair_tokens: list[str] = []
        self.reject_token: str | None = None
        self.stream_started = asyncio.Event()

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def url(self, path):
        return self.base_url + path

    async def async_info(self):
        return self.info

    async def async_state(self):
        if not self.token or self.token == self.reject_token:
            raise agentmod.AgentUnauthorized("state")
        return self.state_doc

    async def async_themes(self):
        return [{"id": "theme://a", "title": "A"}]

    async def async_pair(self, name):
        if not self.pair_tokens:
            raise agentmod.AgentPairingClosed("closed")
        self.token = self.pair_tokens.pop(0)
        return self.token

    async def async_events(self):
        self.stream_started.set()
        while True:
            ev = await self.events.get()
            if ev is None:
                raise agentmod.AgentError("ended")
            yield ev

    async def async_artwork(self):
        return b"png"


async def _until(predicate, timeout=5.0):
    """Yield to the loop until ``predicate()`` holds (or fail after timeout)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def _start(hass, entry, fake_holder, *, configure=None, cloud_caps=None):
    """Build and start a coordinator whose agent client is a FakeAgent.

    The agent task starts eagerly inside ``async_start``, so anything the
    fake must know (pair tokens, …) is applied by ``configure`` at creation.
    """

    def factory(session, host, port=8585, token=None):
        fake = FakeAgent(session, host, port, token)
        if configure:
            configure(fake)
        fake_holder.append(fake)
        return fake

    with (
        patch(f"{COORD}.agentmod.AgentClient", side_effect=factory),
        patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()),
    ):
        coordinator = BraviaTvCoordinator(hass, entry, _client())
        coordinator._state["power"] = True
        if cloud_caps:
            coordinator.cloud_capabilities = cloud_caps
        await coordinator.async_start()
        return coordinator


async def test_stored_token_streams_state(hass: HomeAssistant):
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes)
    fake = fakes[0]
    assert coordinator.agent is fake
    await asyncio.wait_for(fake.stream_started.wait(), 5)
    assert coordinator.agent_value("environment", "illuminance_lx") == 12.0
    fake.events.put_nowait(("state", {**_DOC, "environment": {"illuminance_lx": 99.0}}))
    await _until(
        lambda: coordinator.agent_value("environment", "illuminance_lx") == 99.0
    )
    assert coordinator.data[AGENT_ONLINE] is True
    assert coordinator.agent_value("bluetooth", "remote", "battery_pct") == 30
    assert coordinator.agent_themes[0]["id"] == "theme://a"
    assert entry.data[CONF_AGENT_TOKEN] == "tok"
    await coordinator.async_shutdown()


async def test_pairs_via_local_launch_when_no_token(hass: HomeAssistant):
    entry = _entry(hass)
    fakes: list[FakeAgent] = []
    with patch(f"{COORD}.asyncio.sleep", new=AsyncMock()):
        coordinator = await _start(
            hass, entry, fakes, configure=lambda f: f.pair_tokens.append("fresh")
        )
        await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) == "fresh")
        await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    # The agent was launched through the local app-launch path for its window.
    coordinator.client.exec_command.assert_any_call(
        "system_setting.application", agentmod.AGENT_PACKAGE
    )
    assert entry.data[CONF_AGENT_TOKEN] == "fresh"
    assert coordinator.data[AGENT_ONLINE] is True
    await coordinator.async_shutdown()


async def test_pairs_via_cloud_deep_link_first(hass: HomeAssistant):
    entry = _entry(hass)
    fakes: list[FakeAgent] = []
    sent: list[dict] = []

    async def fake_cloud(name, commands):
        sent.append(commands[0])
        fakes[0].reject_token = None  # the agent adopted whatever we delivered
        return {}

    with (
        patch(f"{COORD}.asyncio.sleep", new=AsyncMock()),
        patch(
            f"{COORD}.BraviaTvCoordinator.async_cloud_command", side_effect=fake_cloud
        ),
    ):
        coordinator = await _start(
            hass, entry, fakes, cloud_caps={"application.deep_link": ("post",)}
        )
        fake = fakes[0]
        await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) is not None)
    assert sent and sent[0]["name"] == "application.deep_link"
    uri = sent[0]["args"]["uri"] if "args" in sent[0] else sent[0]["uri"]
    assert uri.startswith("braviaagent://pair?token=")
    assert entry.data[CONF_AGENT_TOKEN] == fake.token
    coordinator.client.exec_command.assert_not_called()
    await coordinator.async_shutdown()


async def test_no_agent_is_harmless(hass: HomeAssistant):
    entry = _entry(hass)

    class Absent(FakeAgent):
        async def async_info(self):
            raise agentmod.AgentError("refused")

    with (
        patch(
            f"{COORD}.agentmod.AgentClient", side_effect=lambda *a, **k: Absent(*a, **k)
        ),
        patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()),
    ):
        coordinator = BraviaTvCoordinator(hass, entry, _client())
        await coordinator.async_start()
    assert coordinator.agent is None
    assert AGENT_ONLINE not in coordinator.data
    await coordinator.async_shutdown()


async def test_offline_tv_marks_agent_offline(hass: HomeAssistant):
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes)
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    coordinator._set_agent_online(False)
    assert coordinator.data[AGENT_ONLINE] is False
    assert coordinator.data[agent_path("media")] is None
    await coordinator.async_shutdown()
