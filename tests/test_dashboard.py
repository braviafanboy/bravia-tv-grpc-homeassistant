"""Dashboards on the TV: the display user, tokens, and the agent calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.core_config import async_process_ha_core_config  # noqa: E402
from homeassistant.exceptions import (  # noqa: E402
    HomeAssistantError,
    ServiceValidationError,
)

from custom_components.bravia_tv_grpc import dashboard as dashboards  # noqa: E402
from custom_components.bravia_tv_grpc.const import AGENT_VERSION, DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    AGENT_ONLINE,
    BraviaTvCoordinator,
)
from custom_components.bravia_tv_grpc.grpc import agent as agentmod  # noqa: E402


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
async def _owner(hass: HomeAssistant):
    """A real install already has its owner (onboarding); Home Assistant makes
    the first user ever created the owner."""
    await hass.auth.async_create_user("Owner")


@pytest.fixture
async def _internal_url(hass: HomeAssistant):
    await async_process_ha_core_config(
        hass, {"internal_url": "http://192.168.1.5:8123"}
    )


def _coordinator(hass: HomeAssistant, result=None) -> BraviaTvCoordinator:
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
        c.agent.async_dashboard_open = AsyncMock(side_effect=result)
    else:
        c.agent.async_dashboard_open = AsyncMock(return_value=result or {})
    c.agent.async_dashboard_close = AsyncMock(return_value={})
    c.agent.async_dashboard_token = AsyncMock(return_value={})
    return c


def test_dashboard_path():
    assert dashboards.dashboard_path("lovelace/0") == "lovelace/0"
    assert dashboards.dashboard_path(" /energy ") == "energy"
    assert dashboards.dashboard_path("dashboard-tv/home?x=1") == "dashboard-tv/home?x=1"
    for bad in ("", "  ", "http://evil/x", "//evil/x", "https://ha/lovelace", "a\\b"):
        with pytest.raises(dashboards.DashboardPathError):
            dashboards.dashboard_path(bad)


async def test_display_user_created_once(hass: HomeAssistant):
    token, expires_in = await dashboards.async_access_token(hass)
    assert expires_in == 1800
    users = [
        u
        for u in await hass.auth.async_get_users()
        if u.name == dashboards.DISPLAY_USER_NAME
    ]
    assert len(users) == 1
    user = users[0]
    assert not user.is_admin
    assert user.local_only
    assert not user.system_generated
    refresh = hass.auth.async_validate_access_token(token)
    assert refresh is not None and refresh.user.id == user.id

    # Reused: same user, same refresh token, a new access token.
    token2, _ = await dashboards.async_access_token(hass)
    users = [
        u
        for u in await hass.auth.async_get_users()
        if u.name == dashboards.DISPLAY_USER_NAME
    ]
    assert len(users) == 1
    assert len(users[0].refresh_tokens) == 1
    assert hass.auth.async_validate_access_token(token2).id == refresh.id


async def test_display_user_recreated_if_deleted(hass: HomeAssistant):
    await dashboards.async_access_token(hass)
    user = await dashboards.async_display_user(hass)
    await hass.auth.async_remove_user(user)
    again = await dashboards.async_display_user(hass)
    assert again.id != user.id


async def test_overlay_user_can_only_look(hass: HomeAssistant):
    """An overlay is only ever looked at, so its token belongs to a user in
    Home Assistant's read-only group: it can read states, not call actions."""
    token, _ = await dashboards.async_access_token(hass, interactive=False)
    viewer = hass.auth.async_validate_access_token(token).user
    assert viewer.name == dashboards.VIEW_USER_NAME
    assert viewer.local_only and not viewer.is_admin
    assert viewer.permissions.check_entity("lock.front_door", "read")
    assert not viewer.permissions.check_entity("lock.front_door", "control")

    # The full-screen dashboard is operated with the remote: an ordinary user.
    token, _ = await dashboards.async_access_token(hass, interactive=True)
    operator = hass.auth.async_validate_access_token(token).user
    assert operator.id != viewer.id
    assert operator.name == dashboards.DISPLAY_USER_NAME
    assert operator.permissions.check_entity("lock.front_door", "control")
    assert not operator.is_admin


async def test_remove_display_user(hass: HomeAssistant):
    await dashboards.async_access_token(hass)
    await dashboards.async_access_token(hass, interactive=False)
    await dashboards.async_open_session(hass, "entry", "full", None)
    await dashboards.async_remove_display_user(hass)
    assert not [
        u
        for u in await hass.auth.async_get_users()
        if u.name in (dashboards.DISPLAY_USER_NAME, dashboards.VIEW_USER_NAME)
    ]
    assert await dashboards.async_get_session(hass, "entry") is None
    # Idempotent.
    await dashboards.async_remove_display_user(hass)


async def test_sessions(hass: HomeAssistant):
    """A session marks a dashboard Home Assistant opened on a TV; it survives
    for as long as the dashboard is meant to show."""
    assert await dashboards.async_get_session(hass, "tv1") is None
    # Full screen without a duration: until closed.
    session = await dashboards.async_open_session(hass, "tv1", "full", None)
    assert session["mode"] == "full" and session["expires"] is None
    assert await dashboards.async_get_session(hass, "tv1") == session
    assert await dashboards.async_get_session(hass, "tv2") is None
    # An overlay always ends: its duration, or the agent's 30 s default.
    timed = await dashboards.async_open_session(hass, "tv2", "overlay", 20)
    assert 20 < timed["expires"] - timed["opened"] < 200
    default = await dashboards.async_open_session(hass, "tv3", "overlay", None)
    assert 30 < default["expires"] - default["opened"] < 200
    assert not dashboards.session_alive(timed, timed["expires"] + 1)
    assert dashboards.session_alive(timed, timed["opened"] + 1)
    await dashboards.async_close_session(hass, "tv1")
    assert await dashboards.async_get_session(hass, "tv1") is None
    await dashboards.async_close_session(hass, "tv1")  # idempotent


async def test_show_full(hass: HomeAssistant, _internal_url):
    c = _coordinator(hass, {"state": "shown"})
    await c.async_show_dashboard("lovelace/0", mode="full", position="right", size=40)
    args, kwargs = c.agent.async_dashboard_open.call_args
    url, token, expires_in = args
    assert url == "http://192.168.1.5:8123/lovelace/0"
    user = hass.auth.async_validate_access_token(token).user
    assert user.name == dashboards.DISPLAY_USER_NAME
    assert expires_in == 1800
    # Showing it opened the session that later token requests are tied to.
    session = await dashboards.async_get_session(hass, c.config_entry.entry_id)
    assert session["mode"] == "full"
    # Position and size only apply to overlays.
    assert kwargs == {
        "mode": "full",
        "position": None,
        "size": None,
        "duration_s": None,
        "kiosk": None,
    }


async def test_show_overlay(hass: HomeAssistant, _internal_url):
    c = _coordinator(hass, {"state": "shown"})
    await c.async_show_dashboard(
        "/energy",
        mode="overlay",
        position="top_right",
        size=30,
        duration=20,
        kiosk=False,
    )
    args, kwargs = c.agent.async_dashboard_open.call_args
    assert args[0] == "http://192.168.1.5:8123/energy"
    assert kwargs == {
        "mode": "overlay",
        "position": "top_right",
        "size": 30,
        "duration_s": 20,
        "kiosk": False,
    }
    # An overlay gets the view-only user's token.
    user = hass.auth.async_validate_access_token(args[1]).user
    assert user.name == dashboards.VIEW_USER_NAME
    assert not user.permissions.check_entity("light.kitchen", "control")


async def test_show_refuses_urls(hass: HomeAssistant, _internal_url):
    c = _coordinator(hass, {"state": "shown"})
    with pytest.raises(ServiceValidationError) as err:
        await c.async_show_dashboard("http://evil.example/")
    assert err.value.translation_key == "dashboard_invalid_path"
    c.agent.async_dashboard_open.assert_not_awaited()


@pytest.mark.parametrize(
    ("result", "key"),
    [
        ({"state": "no_permission"}, "dashboard_no_overlay"),
        ({"state": "failed"}, "dashboard_failed"),
        (agentmod.AgentRequestError("not_found", 404), "dashboard_needs_agent_update"),
        (agentmod.AgentRequestError("invalid_url", 400), "dashboard_failed"),
        (agentmod.AgentError("down"), "agent_offline"),
    ],
)
async def test_show_errors(hass: HomeAssistant, _internal_url, result, key):
    c = _coordinator(hass, result)
    with pytest.raises(HomeAssistantError) as err:
        await c.async_show_dashboard("lovelace/0", mode="overlay")
    assert err.value.translation_key == key
    if key == "dashboard_needs_agent_update":
        assert err.value.translation_placeholders["required"] == AGENT_VERSION
    # Nothing is showing, so nothing may ask for tokens.
    assert await dashboards.async_get_session(hass, c.config_entry.entry_id) is None


async def test_show_needs_agent_online(hass: HomeAssistant, _internal_url):
    c = _coordinator(hass, {"state": "shown"})
    c._state[AGENT_ONLINE] = False
    with pytest.raises(HomeAssistantError) as err:
        await c.async_show_dashboard("lovelace/0")
    assert err.value.translation_key == "agent_offline"


async def test_close(hass: HomeAssistant, _internal_url):
    c = _coordinator(hass, {"state": "shown"})
    await c.async_show_dashboard("lovelace/0")
    await c.async_close_dashboard()
    c.agent.async_dashboard_close.assert_awaited_once()
    # Closed: its session is over.
    assert await dashboards.async_get_session(hass, c.config_entry.entry_id) is None
    assert c._dashboard_session is None
    c.agent.async_dashboard_close.side_effect = agentmod.AgentRequestError(
        "not_found", 404
    )
    with pytest.raises(HomeAssistantError) as err:
        await c.async_close_dashboard()
    assert err.value.translation_key == "dashboard_needs_agent_update"


_NEEDS_TOKEN = {
    "dashboard": {
        "showing": True,
        "mode": "full",
        "loaded": True,
        "token_needed": True,
    }
}


async def test_token_needed_answered_once(hass: HomeAssistant, _internal_url):
    c = _coordinator(hass, {"state": "shown"})
    await c.async_show_dashboard("lovelace/0")
    c._apply_agent_document(_NEEDS_TOKEN)
    c._apply_agent_document(_NEEDS_TOKEN)  # a second event while the first send runs
    await hass.async_block_till_done()
    c.agent.async_dashboard_token.assert_awaited_once()
    token, expires_in = c.agent.async_dashboard_token.call_args.args
    assert hass.auth.async_validate_access_token(token) is not None
    assert expires_in == 1800

    # Not needed: nothing sent.
    c.agent.async_dashboard_token.reset_mock()
    c._apply_agent_document({"dashboard": {"showing": True, "token_needed": False}})
    await hass.async_block_till_done()
    c.agent.async_dashboard_token.assert_not_awaited()


async def test_token_refresh_matches_the_dashboard_kind(
    hass: HomeAssistant, _internal_url
):
    c = _coordinator(hass, {"state": "shown"})
    await c.async_show_dashboard("lovelace/0", mode="overlay", duration=600)
    c._apply_agent_document(
        {"dashboard": {"showing": True, "mode": "overlay", "token_needed": True}}
    )
    await hass.async_block_till_done()
    token = c.agent.async_dashboard_token.call_args.args[0]
    assert (
        hass.auth.async_validate_access_token(token).user.name
        == dashboards.VIEW_USER_NAME
    )


async def test_no_token_without_a_dashboard_opened_from_home_assistant(
    hass: HomeAssistant, caplog
):
    """The far end of the agent link saying "my page wants a token" is not
    enough: a token is only sent for a dashboard this integration opened.
    No display user is created for such a request either."""
    c = _coordinator(hass)
    c._apply_agent_document(_NEEDS_TOKEN)
    c._apply_agent_document(_NEEDS_TOKEN)
    await hass.async_block_till_done()
    c.agent.async_dashboard_token.assert_not_awaited()
    assert not [
        u for u in await hass.auth.async_get_users() if "HA TV Agent" in (u.name or "")
    ]
    # Said once, not on every state event.
    assert caplog.text.count("asked for a dashboard token") == 1


async def test_session_ends_when_the_tv_reports_the_dashboard_gone(
    hass: HomeAssistant, _internal_url
):
    c = _coordinator(hass, {"state": "shown"})
    await c.async_show_dashboard("lovelace/0")
    entry_id = c.config_entry.entry_id
    # A "not showing" right after opening may predate the open: ignored.
    c._apply_agent_document({"dashboard": {"showing": False, "token_needed": False}})
    await hass.async_block_till_done()
    assert await dashboards.async_get_session(hass, entry_id) is not None
    # Once settled, it ends the session (Back on the remote, panel off, …).
    c._dashboard_session["opened"] -= dashboards.SESSION_SETTLE_S + 1
    c._apply_agent_document({"dashboard": {"showing": False, "token_needed": False}})
    await hass.async_block_till_done()
    assert c._dashboard_session is None
    assert await dashboards.async_get_session(hass, entry_id) is None
    # …after which a token request is refused again.
    c._apply_agent_document(_NEEDS_TOKEN)
    await hass.async_block_till_done()
    c.agent.async_dashboard_token.assert_not_awaited()


async def test_expired_session_gets_no_token(hass: HomeAssistant, _internal_url):
    c = _coordinator(hass, {"state": "shown"})
    await c.async_show_dashboard("lovelace/0", mode="overlay", duration=10)
    c._dashboard_session["expires"] = c._dashboard_session["opened"] - 1
    c._apply_agent_document(
        {"dashboard": {"showing": True, "mode": "overlay", "token_needed": True}}
    )
    await hass.async_block_till_done()
    c.agent.async_dashboard_token.assert_not_awaited()


async def test_token_send_failure_is_quiet(hass: HomeAssistant, _internal_url):
    c = _coordinator(hass, {"state": "shown"})
    await c.async_show_dashboard("lovelace/0")
    c.agent.async_dashboard_token.side_effect = agentmod.AgentError("down")
    c._apply_agent_document({"dashboard": {"showing": True, "token_needed": True}})
    await hass.async_block_till_done()
    # A later request is still answered.
    c.agent.async_dashboard_token.side_effect = None
    c._apply_agent_document({"dashboard": {"showing": True, "token_needed": True}})
    await hass.async_block_till_done()
    assert c.agent.async_dashboard_token.await_count == 2


async def _register_tv(hass: HomeAssistant, result=None):
    """A coordinator stored like async_setup_entry does, with its device."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.bravia_tv_grpc.services import async_setup_services

    c = _coordinator(hass, result)
    hass.data.setdefault(DOMAIN, {})[c.config_entry.entry_id] = c
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=c.config_entry.entry_id, identifiers={(DOMAIN, "dev")}
    )
    async_setup_services(hass)
    return c, device


async def test_action_targets_the_tv_device(hass: HomeAssistant, _internal_url):
    c, device = await _register_tv(hass, {"state": "shown"})
    await hass.services.async_call(
        DOMAIN,
        "show_dashboard",
        {"device_id": device.id, "dashboard": "lovelace/0", "mode": "overlay"},
        blocking=True,
    )
    args, kwargs = c.agent.async_dashboard_open.call_args
    assert args[0] == "http://192.168.1.5:8123/lovelace/0"
    assert kwargs["mode"] == "overlay"
    assert kwargs["position"] == "right"
    assert kwargs["size"] == 40

    await hass.services.async_call(
        DOMAIN, "close_dashboard", {"device_id": device.id}, blocking=True
    )
    c.agent.async_dashboard_close.assert_awaited_once()


async def test_action_without_a_tv(hass: HomeAssistant, _internal_url):
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    c, _ = await _register_tv(hass, {"state": "shown"})
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    stranger = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "x")}
    )
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "show_dashboard",
            {"device_id": stranger.id, "dashboard": "lovelace/0"},
            blocking=True,
        )
    assert err.value.translation_key == "no_tv_targeted"
    c.agent.async_dashboard_open.assert_not_awaited()


async def test_no_display_user_before_onboarding(hass: HomeAssistant):
    """Without a real user yet, the TV's user would become the owner: refuse."""
    with (
        patch.object(hass.auth, "async_get_users", AsyncMock(return_value=[])),
        patch.object(hass.auth, "async_create_user", AsyncMock()) as create,
        pytest.raises(HomeAssistantError) as err,
    ):
        await dashboards.async_display_user(hass)
    assert err.value.translation_key == "dashboard_no_owner"
    create.assert_not_awaited()
