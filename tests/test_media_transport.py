"""Local media transport via the agent, with Sony-cloud fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    AGENT_ONLINE,
    BraviaTvCoordinator,
    agent_path,
)
from custom_components.bravia_tv_grpc.grpc import agent as agentmod  # noqa: E402


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _coordinator(hass: HomeAssistant) -> BraviaTvCoordinator:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="dev", data={"host": "tv", "agent_token": "t"}
    )
    entry.add_to_hass(hass)
    client = MagicMock()
    client.capabilities = {}
    c = BraviaTvCoordinator(hass, entry, client)
    c._state["power"] = True
    c._state[AGENT_ONLINE] = True
    c.agent = MagicMock()
    return c


async def test_agent_handles_command(hass: HomeAssistant):
    c = _coordinator(hass)
    c.agent.async_media_command = AsyncMock(return_value={"ok": True})
    assert await c.async_agent_media_command("play") is True
    c.agent.async_media_command.assert_awaited_once_with("play", None)


async def test_seek_passes_position(hass: HomeAssistant):
    c = _coordinator(hass)
    c.agent.async_media_command = AsyncMock(return_value={"ok": True})
    assert await c.async_agent_media_command("seek", 5000) is True
    c.agent.async_media_command.assert_awaited_once_with("seek", 5000)


async def test_no_session_falls_back(hass: HomeAssistant):
    c = _coordinator(hass)
    c.agent.async_media_command = AsyncMock(return_value={"ok": False})
    assert await c.async_agent_media_command("pause") is False


async def test_agent_error_falls_back(hass: HomeAssistant):
    c = _coordinator(hass)
    c.agent.async_media_command = AsyncMock(side_effect=agentmod.AgentError("404"))
    assert await c.async_agent_media_command("next") is False


async def test_offline_falls_back(hass: HomeAssistant):
    c = _coordinator(hass)
    c._state[AGENT_ONLINE] = False
    assert await c.async_agent_media_command("play") is False


async def test_foreground_mismatch_falls_back(hass: HomeAssistant):
    """A session that isn't the foreground app (stale background session) must
    not be controlled; the command falls back to the cloud."""
    c = _coordinator(hass)
    c._state["system_setting.application"] = "com.kick.mobile"  # foreground
    c._state[agent_path("media")] = {"app": "com.spotify.tv.android"}  # stale
    c.agent.async_media_command = AsyncMock(return_value={"ok": True})
    assert await c.async_agent_media_command("play") is False
    c.agent.async_media_command.assert_not_awaited()  # never hit the agent


async def test_foreground_match_uses_agent(hass: HomeAssistant):
    c = _coordinator(hass)
    c._state["system_setting.application"] = "com.spotify.tv.android"
    c._state[agent_path("media")] = {"app": "com.spotify.tv.android"}
    c.agent.async_media_command = AsyncMock(return_value={"ok": True})
    assert await c.async_agent_media_command("pause") is True
    c.agent.async_media_command.assert_awaited_once_with("pause", None)
