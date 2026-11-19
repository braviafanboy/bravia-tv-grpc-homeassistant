"""What the coordinator does when something around it fails: Sony's cloud,
the agent link, a write, the keys (require HA test harness)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from fake_tv import CLOUD_DEVICE, CLOUD_STATES, CREDS, FakeTv  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402

from custom_components.bravia_tv_grpc.bravia_tv_client import (  # noqa: E402
    BraviaTvCallError,
    BraviaTvConnectionError,
)
from custom_components.bravia_tv_grpc.const import (  # noqa: E402
    CONF_AGENT_TOKEN,
    CONF_GRPC_KEYS,
    DOMAIN,
)
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    AGENT_ONLINE,
    BraviaTvCoordinator,
    needs_reauth,
    schema_store,
)
from custom_components.bravia_tv_grpc.grpc import agent as agentmod  # noqa: E402
from custom_components.bravia_tv_grpc.grpc import cloud_commands as cloud  # noqa: E402
from custom_components.bravia_tv_grpc.grpc import credentials as cred  # noqa: E402

COORD = "custom_components.bravia_tv_grpc.coordinator"
_DEVICE = CLOUD_DEVICE | {"device_id": "dev"}


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _entry(hass: HomeAssistant, creds: dict | None = None, **data):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev",
        title="Living room TV",
        data={
            "host": "tv",
            "grpc_device_id": "dev",
            CONF_GRPC_KEYS: cred.credentials_to_json(creds or CREDS),
            **data,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass: HomeAssistant, entry) -> BraviaTvCoordinator:
    return BraviaTvCoordinator(
        hass, entry, FakeTv("tv", 40000, "dev", CREDS["hmac_key"])
    )


def _cloud_up():
    return (
        patch(
            f"{COORD}.cred.async_get_devices",
            new=AsyncMock(return_value={"devices": [_DEVICE]}),
        ),
        patch(
            f"{COORD}.cred.async_get_device_states",
            new=AsyncMock(return_value=CLOUD_STATES),
        ),
    )


def _cloud_down():
    return patch(
        f"{COORD}.cred.async_get_devices",
        new=AsyncMock(side_effect=aiohttp.ClientConnectionError("no internet")),
    )


# -- Sony's cloud ----------------------------------------------------------------


async def test_cloud_capabilities_survive_an_unreachable_cloud(hass: HomeAssistant):
    """Home Assistant starts while Sony cannot be reached (a power cut: it is
    up before the internet is). The cloud-backed controls used to be gone
    until something reloaded the entry; now the capabilities seen last time
    are used."""
    entry = _entry(hass)
    first = _coordinator(hass, entry)
    up_devices, up_states = _cloud_up()
    with up_devices, up_states:
        await first.async_start()
    assert first.cloud_supports("sleep_timer") and first._cloud_loaded
    known = dict(first.cloud_capabilities)
    await first.async_shutdown()

    second = _coordinator(hass, entry)
    with _cloud_down():
        await second.async_start()
    assert not second._cloud_loaded
    assert second.cloud_capabilities == known
    assert second.cloud_values["sleep_timer"] == first.cloud_values["sleep_timer"]
    # The poll is running, to load them properly once Sony answers again.
    assert second._cancel_cloud_poll is not None
    up_devices, up_states = _cloud_up()
    with (
        up_devices,
        up_states,
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await second._async_cloud_poll()
    assert second._cloud_loaded
    reload.assert_not_called()  # same controls as the entities were built from
    await second.async_shutdown()


async def test_cloud_seen_for_the_first_time_later_reloads(hass: HomeAssistant):
    """Nothing stored yet and Sony unreachable at start: no cloud entities.
    When the cloud answers, the entry is set up afresh so they exist."""
    entry = _entry(hass)
    coordinator = _coordinator(hass, entry)
    with _cloud_down():
        await coordinator.async_start()
    assert coordinator.cloud_capabilities == {}
    assert coordinator._cancel_cloud_poll is not None
    with (
        _cloud_down(),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await coordinator._async_cloud_poll()  # still down: keep waiting
    reload.assert_not_called()
    up_devices, up_states = _cloud_up()
    with (
        up_devices,
        up_states,
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await coordinator._async_cloud_poll()
    reload.assert_called_once_with(entry.entry_id)
    assert (await schema_store(hass, entry.entry_id).async_load())["cloud"]
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientConnectionError("Cannot connect to host"),
        TimeoutError(),
        OSError("network unreachable"),
        cred.GrpcCredentialsRefreshError("HTTP 500", status=500),
    ],
)
async def test_cloud_failures_are_translated(hass: HomeAssistant, error):
    """No internet used to surface as a raw aiohttp error with a traceback."""
    entry = _entry(hass)
    coordinator = _coordinator(hass, entry)
    with (
        patch(f"{COORD}.cloud.async_run_commands", new=AsyncMock(side_effect=error)),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await coordinator.async_cloud_command(
            "sleep_timer", [cloud.set_command("sleep_timer", "off")]
        )
    assert caught.value.translation_key == "cloud_unreachable"
    assert caught.value.translation_placeholders["name"] == "sleep_timer"
    assert caught.value.translation_placeholders["reason"]


async def test_cloud_command_cannot_hang_the_queue(hass: HomeAssistant):
    entry = _entry(hass)
    coordinator = _coordinator(hass, entry)

    async def never(*args, **kwargs):
        await asyncio.sleep(3600)

    with (
        patch(f"{COORD}._CLOUD_COMMAND_TIMEOUT", 0.05),
        patch(f"{COORD}.cloud.async_run_commands", new=never),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await coordinator.async_cloud_command(
            "sleep_timer", [cloud.set_command("sleep_timer", "off")]
        )
    assert caught.value.translation_key == "cloud_unreachable"
    assert not coordinator._cloud_lock.locked()


async def test_rotated_refresh_token_is_stored(hass: HomeAssistant):
    """If Sony hands out a new refresh token with an access token, the old
    one may stop working: the new one must be what is kept."""
    entry = _entry(hass, {**CREDS, "access_token_expires_at": 1})
    coordinator = _coordinator(hass, entry)
    with patch(
        f"{COORD}.cred.async_refresh_access_token",
        new=AsyncMock(
            return_value={
                "access_token": "new-at",
                "expires_in": 3600,
                "refresh_token": "rt-2",
            }
        ),
    ):
        assert await coordinator._async_cloud_token() == "new-at"
    assert (
        cred.parse_credentials_json(entry.data[CONF_GRPC_KEYS])["refresh_token"]
        == "rt-2"
    )
    # Unchanged token: nothing rewritten.
    with (
        patch(
            f"{COORD}.cred.async_refresh_access_token",
            new=AsyncMock(
                return_value={"access_token": "at-3", "refresh_token": "rt-2"}
            ),
        ),
        patch.object(hass.config_entries, "async_update_entry") as update,
    ):
        await coordinator._async_cloud_token(force=True)
    update.assert_not_called()


# -- keys --------------------------------------------------------------------------


async def test_keys_are_renewed_at_half_their_life(hass: HomeAssistant):
    """They only come from Sony. Renewing an hour before they lapse left local
    control an hour to ride out a cloud outage; at half-life it has twelve."""
    now = time.time()
    entry = _entry(
        hass,
        {
            **CREDS,
            "session_keys_fetched_at": now,
            "session_keys_expires_at": now + 86400,
        },
    )
    coordinator = _coordinator(hass, entry)
    with patch(f"{COORD}.async_call_later") as later:
        coordinator._schedule_key_renewal()
    delay = later.call_args[0][1].total_seconds()
    assert 11.9 * 3600 < delay <= 12 * 3600
    # Half the life already gone: renew now (after the minimum wait).
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_GRPC_KEYS: cred.credentials_to_json(
                {
                    **CREDS,
                    "session_keys_fetched_at": now - 80000,
                    "session_keys_expires_at": now + 6400,
                }
            ),
        },
    )
    with patch(f"{COORD}.async_call_later") as later:
        coordinator._schedule_key_renewal()
    assert later.call_args[0][1].total_seconds() == 60


def test_which_refresh_failures_need_the_user():
    assert needs_reauth(cred.GrpcCredentialsError("no refresh token"))
    for status in (400, 401, 403):
        assert needs_reauth(cred.GrpcCredentialsRefreshError("x", status=status))
    for status in (None, 404, 429, 500, 503):
        assert not needs_reauth(cred.GrpcCredentialsRefreshError("x", status=status))
    assert not needs_reauth(OSError("reset"))
    assert not needs_reauth(TimeoutError())


# -- the TV ----------------------------------------------------------------------------


async def test_writes_report_no_answer_and_refusal_differently(hass: HomeAssistant):
    entry = _entry(hass)
    coordinator = _coordinator(hass, entry)
    with patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()):
        await coordinator.async_start()
    tv = coordinator.client

    def no_answer(path, value, timeout=8.0):
        raise BraviaTvConnectionError("write to volume failed: UNAVAILABLE")

    tv.exec_command = no_answer
    for call in (
        coordinator.async_set_field("volume", 5),
        coordinator.async_send_key("remote_key.d_pad", "up"),
    ):
        with pytest.raises(HomeAssistantError) as caught:
            await call
        assert caught.value.translation_key == "tv_unreachable"
        assert caught.value.translation_placeholders == {"name": "BRAVIA 8 II"}

    def refused(path, value, timeout=8.0):
        raise BraviaTvCallError("write to volume", "INVALID_ARGUMENT")

    tv.exec_command = refused
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_set_field("volume", 5)
    assert caught.value.translation_key == "exec_rejected"
    assert coordinator.data["volume"] == 18  # not applied

    tv.get_states = lambda paths=None, timeout=10.0: (_ for _ in ()).throw(
        BraviaTvConnectionError("down")
    )
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_read_paths(["volume"])
    assert caught.value.translation_key == "tv_unreachable"
    await coordinator.async_shutdown()


async def test_a_reply_that_decodes_to_nothing_is_still_alive(hass: HomeAssistant):
    """The liveness probe asks whether the TV answers, not what. Treating an
    empty decode as a dead connection would tear it down in a loop."""
    entry = _entry(hass)
    coordinator = _coordinator(hass, entry)
    with patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()):
        await coordinator.async_start()
    coordinator.client.get_states = lambda paths=None, timeout=10.0: {}
    for _ in range(3):
        await coordinator._async_periodic_refresh(None)
    assert not coordinator.offline
    await coordinator.async_shutdown()


# -- the agent link ------------------------------------------------------------


class _Agent:
    """A minimal agent whose stream the test feeds."""

    def __init__(self, session, host, port=8585, token=None, fingerprint=None):
        self.host, self.token, self.fingerprint = host, token, fingerprint
        self.states_served = 0
        self.fail_next_state: Exception | None = None
        self.events: asyncio.Queue = asyncio.Queue()
        self.info = {"agent": {"version": "0.9.1", "contract": 1}}

    pinned = property(lambda self: self.fingerprint is not None)

    def pin(self, fingerprint):
        self.fingerprint = fingerprint

    async def async_fetch_fingerprint(self):
        return "ab" * 32

    async def async_info(self):
        return self.info

    async def async_state(self):
        if self.fail_next_state is not None:
            err, self.fail_next_state = self.fail_next_state, None
            raise err
        self.states_served += 1
        return {"environment": {"illuminance_lx": self.states_served}}

    async def async_events(self):
        while True:
            item = await self.events.get()
            if isinstance(item, Exception):
                raise item
            yield item

    async def async_picture_config(self, *args):
        return {}

    async def async_ambient(self, sub, body):
        return {}


@pytest.mark.agent_probe
@pytest.mark.parametrize(
    "error",
    [ValueError("Chunk too big"), RuntimeError("anything at all"), KeyError("x")],
)
async def test_agent_link_outlives_any_error(hass: HomeAssistant, error, caplog):
    """Anything that was not one of the agent's own errors used to end the
    link for good, silently, with its entities frozen on their last values."""
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok", "agent_fingerprint": "ab" * 32})
    agents: list[_Agent] = []

    def factory(*args, **kwargs):
        agents.append(_Agent(*args, **kwargs))
        return agents[-1]

    with (
        patch(f"{COORD}.agentmod.AgentClient", side_effect=factory),
        patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()),
        patch(
            f"{COORD}.agentmod.async_fetch_manifest",
            new=AsyncMock(side_effect=agentmod.AgentError("x")),
        ),
        patch(f"{COORD}.asyncio.sleep", new=AsyncMock()),
    ):
        coordinator = _coordinator(hass, entry)
        await coordinator.async_start()
        agent = agents[0]
        await _until(lambda: agent.states_served == 1)
        assert coordinator.data[AGENT_ONLINE] is True

        agent.events.put_nowait(error)  # the stream blows up
        await _until(lambda: agent.states_served == 2)
    assert coordinator.agent_value("environment", "illuminance_lx") == 2
    assert coordinator.data[AGENT_ONLINE] is True
    assert not coordinator._agent_task.done()
    assert "failed unexpectedly" in caplog.text  # and it says so
    await coordinator.async_shutdown()


@pytest.mark.agent_probe
async def test_agent_newer_than_the_integration_raises_a_repair(hass: HomeAssistant):
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok", "agent_fingerprint": "ab" * 32})
    agents: list[_Agent] = []

    def factory(*args, **kwargs):
        agent = _Agent(*args, **kwargs)
        agent.info = {"agent": {"version": "2.0.0", "contract": agentmod.CONTRACT + 1}}
        agents.append(agent)
        return agent

    with (
        patch(f"{COORD}.agentmod.AgentClient", side_effect=factory),
        patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()),
        patch(
            f"{COORD}.agentmod.async_fetch_manifest",
            new=AsyncMock(side_effect=agentmod.AgentError("x")),
        ),
    ):
        coordinator = _coordinator(hass, entry)
        await coordinator.async_start()
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, f"agent_contract_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders["contract"] == str(agentmod.CONTRACT + 1)
    # The matching contract clears it.
    coordinator.agent_info = {
        "agent": {"version": "0.9.1", "contract": agentmod.CONTRACT}
    }
    coordinator._update_agent_version_issue()
    assert registry.async_get_issue(DOMAIN, f"agent_contract_{entry.entry_id}") is None
    await coordinator.async_shutdown()


_real_sleep = asyncio.sleep


async def _until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await _real_sleep(0.01)
