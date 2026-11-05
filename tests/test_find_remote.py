"""Find remote: the button asks the agent to beep the remote over Bluetooth."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.bravia_tv_grpc.button import (  # noqa: E402
    BraviaTvFindRemoteButton,
)
from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    AGENT_ONLINE,
    BraviaTvCoordinator,
)
from custom_components.bravia_tv_grpc.grpc import agent as agentmod  # noqa: E402


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _coordinator(hass: HomeAssistant, outcome) -> BraviaTvCoordinator:
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
    if isinstance(outcome, Exception):
        c.agent.async_find_remote = AsyncMock(side_effect=outcome)
    else:
        c.agent.async_find_remote = AsyncMock(return_value=outcome)
    return c


@pytest.mark.parametrize("outcome", ["ringing", "found"])
async def test_beeping_succeeds(hass: HomeAssistant, outcome):
    c = _coordinator(hass, outcome)
    await BraviaTvFindRemoteButton(c).async_press()
    c.agent.async_find_remote.assert_awaited_once()


@pytest.mark.parametrize(
    ("outcome", "key"),
    [
        ("unsupported", "remote_finder_unsupported"),
        ("no_remote", "remote_finder_no_remote"),
        ("no_permission", "remote_finder_no_permission"),
        ("failed", "remote_finder_failed"),
        ("something_new", "remote_finder_failed"),
        (agentmod.AgentError("down"), "agent_offline"),
    ],
)
async def test_failures_are_explained(hass: HomeAssistant, outcome, key):
    c = _coordinator(hass, outcome)
    with pytest.raises(HomeAssistantError) as err:
        await c.async_find_remote()
    assert err.value.translation_key == key
