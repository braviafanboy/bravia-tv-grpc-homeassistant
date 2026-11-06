"""On-screen message: the notify entity shows a banner via the agent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    AGENT_ONLINE,
    BraviaTvCoordinator,
)
from custom_components.bravia_tv_grpc.grpc import agent as agentmod  # noqa: E402
from custom_components.bravia_tv_grpc.notify import (  # noqa: E402
    BraviaTvNotifyEntity,
)


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _coordinator(hass: HomeAssistant, result) -> BraviaTvCoordinator:
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
    if isinstance(result, Exception):
        c.agent.async_notify = AsyncMock(side_effect=result)
    else:
        c.agent.async_notify = AsyncMock(return_value=result)
    return c


async def test_send_message_overlay(hass: HomeAssistant):
    c = _coordinator(hass, {"state": "shown", "overlay": True})
    await BraviaTvNotifyEntity(c).async_send_message("hello", title="HA")
    c.agent.async_notify.assert_awaited_once_with(
        "hello", "HA", duration_s=None, icon="message"
    )


async def test_send_notification_icon_duration(hass: HomeAssistant):
    c = _coordinator(hass, {"state": "shown", "overlay": True})
    await BraviaTvNotifyEntity(c).async_send_notification(
        "hi", title="T", icon="alert", duration=20
    )
    c.agent.async_notify.assert_awaited_once_with(
        "hi", "T", duration_s=20, icon="alert"
    )


async def test_no_overlay_raises(hass: HomeAssistant):
    c = _coordinator(hass, {"state": "notification", "overlay": False})
    with pytest.raises(HomeAssistantError) as err:
        await c.async_notify("hello")
    assert err.value.translation_key == "notify_no_overlay"


async def test_agent_offline_raises(hass: HomeAssistant):
    c = _coordinator(hass, agentmod.AgentError("down"))
    with pytest.raises(HomeAssistantError) as err:
        await c.async_notify("hello")
    assert err.value.translation_key == "agent_offline"


async def test_older_agent_404_needs_update(hass: HomeAssistant):
    c = _coordinator(hass, agentmod.AgentRequestError("not_found", 404))
    with pytest.raises(HomeAssistantError) as err:
        await c.async_notify("hello")
    assert err.value.translation_key == "notify_needs_agent_update"


async def test_other_agent_error_offline(hass: HomeAssistant):
    c = _coordinator(hass, agentmod.AgentRequestError("boom", 500))
    with pytest.raises(HomeAssistantError) as err:
        await c.async_notify("hello")
    assert err.value.translation_key == "agent_offline"
