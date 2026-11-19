"""Config-flow tests (require pytest-homeassistant-custom-component + HA).

Skipped where that plugin isn't installed (e.g. the pure-decoder test venv),
run in CI on a supported Python.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402

from custom_components.bravia_tv_grpc.const import (  # noqa: E402
    CONF_GRPC_DEVICE_ID,
    CONF_GRPC_PORT,
    DOMAIN,
)

CF = "custom_components.bravia_tv_grpc.config_flow"
CREDS = {"device_id": "dev-123", "hmac_key": "ab", "session_key": "cd"}


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    # The integration declares a zeroconf dependency; no-op its setup so the
    # test harness doesn't create a real zeroconf instance (sockets/threads).
    with patch("homeassistant.components.zeroconf.async_setup", return_value=True):
        yield


async def _finish_oauth(hass: HomeAssistant, flow_id: str):
    with (
        patch(f"{CF}.async_complete_oauth_flow", new=AsyncMock(return_value=CREDS)),
        patch(f"{CF}.credentials_to_json", return_value="{}"),
        patch("custom_components.bravia_tv_grpc.async_setup_entry", return_value=True),
    ):
        return await hass.config_entries.flow.async_configure(
            flow_id, {"redirect_url": "ssh-app://signin?code=abc"}
        )


async def test_user_flow_success(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with (
        patch(f"{CF}.async_resolve_device_mdns", new=AsyncMock(return_value=None)),
        patch(f"{CF}.scan_for_grpc_port", return_value=36547),
        patch(f"{CF}.start_oauth_login", return_value=("http://auth", "v", "s")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "oauth"

    result = await _finish_oauth(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["host"] == "1.2.3.4"
    assert result["data"][CONF_GRPC_PORT] == 36547
    assert result["data"][CONF_GRPC_DEVICE_ID] == "dev-123"


async def test_user_flow_no_service(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with (
        patch(f"{CF}.async_resolve_device_mdns", new=AsyncMock(return_value=None)),
        patch(f"{CF}.scan_for_grpc_port", return_value=None),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_grpc_service"}


async def test_user_flow_resolves_device_unique_id(hass: HomeAssistant) -> None:
    """A manually-entered IP is bound to the exact device via mDNS, so an
    account with several TVs pairs the right one."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    resolved = (36547, _TV_NAME, {})
    with (
        patch(f"{CF}.async_resolve_device_mdns", new=AsyncMock(return_value=resolved)),
        patch(f"{CF}.start_oauth_login", return_value=("http://auth", "v", "s")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.0.2.10"}
        )
    assert result["step_id"] == "oauth"

    mock = AsyncMock(return_value=CREDS)
    with (
        patch(f"{CF}.async_complete_oauth_flow", new=mock),
        patch(f"{CF}.credentials_to_json", return_value="{}"),
        patch("custom_components.bravia_tv_grpc.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": "ssh-app://signin?code=x"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (
        mock.call_args.kwargs["device_unique_id"]
        == "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
    )


async def test_user_flow_soundbar_ip_rejected(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    resolved = (55051, _SB_NAME, {"imName": "HT-A9M2"})
    with patch(f"{CF}.async_resolve_device_mdns", new=AsyncMock(return_value=resolved)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.0.2.20"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_a_tv"


async def test_duplicate_aborts(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(domain=DOMAIN, unique_id="dev-123", data={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with (
        patch(f"{CF}.async_resolve_device_mdns", new=AsyncMock(return_value=None)),
        patch(f"{CF}.scan_for_grpc_port", return_value=36547),
        patch(f"{CF}.start_oauth_login", return_value=("http://auth", "v", "s")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    result = await _finish_oauth(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


_TV_NAME = (
    "BRAVIA 8 II-0a1b2c3d4e5f60718293a4b5c6d7e8f901234567._sonysmarthome._tcp.local."
)
_SB_NAME = (
    "BRAVIA Theatre Quad-f9e8d7c6b5a4938271605f4e3d2c1b0a99887766"
    "._sonysmarthome._tcp.local."
)


def _zeroconf_info(name: str, host: str, port: int, properties: dict | None = None):
    from ipaddress import ip_address

    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        port=port,
        hostname="dev.local.",
        type="_sonysmarthome._tcp.local.",
        name=name,
        properties=properties or {},
    )


async def _init_zeroconf(hass: HomeAssistant, name: str, host: str, port: int):
    with patch(f"{CF}.start_oauth_login", return_value=("http://auth", "v", "s")):
        return await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_zeroconf_info(name, host, port),
        )


async def test_zeroconf_soundbar_suppressed_by_model(hass: HomeAssistant) -> None:
    """A soundbar (mDNS imName 'HT-*') is not even offered — the flow aborts
    before showing a card, so HA never lists it as configurable."""
    with patch(f"{CF}.start_oauth_login", return_value=("http://auth", "v", "s")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_zeroconf_info(_SB_NAME, "192.0.2.20", 55051, {"imName": "HT-A9M2"}),
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_a_tv"


async def test_zeroconf_soundbar_rejected(hass: HomeAssistant) -> None:
    """Backstop: a soundbar whose model the mDNS check can't see still aborts
    after sign-in, once the cloud device_type is known."""
    from custom_components.bravia_tv_grpc.grpc.credentials import GrpcNotATvError

    result = await _init_zeroconf(hass, _SB_NAME, "192.0.2.20", 55051)
    assert result["step_id"] == "oauth"

    mock = AsyncMock(side_effect=GrpcNotATvError("Speaker", "BRAVIA Theatre Quad"))
    with patch(f"{CF}.async_complete_oauth_flow", new=mock):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": "ssh-app://signin?code=x"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_a_tv"
    # The discovered device's unique id is passed so pairing binds to it.
    assert (
        mock.call_args.kwargs["device_unique_id"]
        == "f9e8d7c6b5a4938271605f4e3d2c1b0a99887766"
    )


async def test_zeroconf_tv_creates_entry(hass: HomeAssistant) -> None:
    result = await _init_zeroconf(hass, _TV_NAME, "192.0.2.10", 36547)
    assert result["step_id"] == "oauth"

    mock = AsyncMock(return_value=CREDS)
    with (
        patch(f"{CF}.async_complete_oauth_flow", new=mock),
        patch(f"{CF}.credentials_to_json", return_value="{}"),
        patch("custom_components.bravia_tv_grpc.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": "ssh-app://signin?code=x"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["host"] == "192.0.2.10"
    assert (
        mock.call_args.kwargs["device_unique_id"]
        == "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
    )


_TV_UID = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"


def _configured_tv(hass: HomeAssistant, **kwargs):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.bravia_tv_grpc.const import CONF_DEVICE_UNIQUE_ID

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-123",
        data={
            "host": "192.0.2.10",
            CONF_GRPC_PORT: 36547,
            CONF_DEVICE_UNIQUE_ID: _TV_UID,
        },
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


async def test_zeroconf_never_rewrites_a_tvs_address(hass: HomeAssistant) -> None:
    """An announcement carrying a configured TV's id (which the TV broadcasts,
    so anything on the network can send one) is a hint, not a fact: the stored
    address is not changed by it. A TV that is set up is told about it, and
    decides for itself."""
    from unittest.mock import MagicMock

    entry = _configured_tv(hass, state=config_entries.ConfigEntryState.LOADED)
    coordinator = MagicMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await _init_zeroconf(hass, _TV_NAME, "192.0.2.99", 40000)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.0.2.10"
    assert entry.data[CONF_GRPC_PORT] == 36547
    coordinator.async_note_discovery.assert_called_once_with("192.0.2.99", 40000)
    reload.assert_not_called()
    hass.data[DOMAIN].pop(entry.entry_id)


async def test_zeroconf_retries_a_tv_whose_setup_is_waiting(
    hass: HomeAssistant,
) -> None:
    """A TV whose setup keeps failing (it was off) is announced again: retry
    now instead of at Home Assistant's next backoff, and hand the attempt the
    announced address to try. The stored address stays as it is."""
    from custom_components.bravia_tv_grpc.const import DATA_ADDRESS_HINTS

    entry = _configured_tv(hass, state=config_entries.ConfigEntryState.SETUP_RETRY)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        # Same address as stored: it used to be ignored, leaving the TV
        # unavailable until the backoff ran out.
        result = await _init_zeroconf(hass, _TV_NAME, "192.0.2.10", 36547)
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    reload.assert_called_once_with(entry.entry_id)
    assert hass.data[DATA_ADDRESS_HINTS][entry.entry_id] == ("192.0.2.10", 36547)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await _init_zeroconf(hass, _TV_NAME, "192.0.2.99", 40000)
        await hass.async_block_till_done()
    reload.assert_called_once_with(entry.entry_id)
    assert hass.data[DATA_ADDRESS_HINTS][entry.entry_id] == ("192.0.2.99", 40000)
    assert entry.data["host"] == "192.0.2.10"
    assert entry.data[CONF_GRPC_PORT] == 36547


async def test_zeroconf_leaves_an_unloaded_tv_alone(hass: HomeAssistant) -> None:
    from custom_components.bravia_tv_grpc.const import DATA_ADDRESS_HINTS

    entry = _configured_tv(hass, state=config_entries.ConfigEntryState.NOT_LOADED)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await _init_zeroconf(hass, _TV_NAME, "192.0.2.99", 40000)
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    reload.assert_not_called()
    assert entry.entry_id not in hass.data.get(DATA_ADDRESS_HINTS, {})
    assert entry.data["host"] == "192.0.2.10"


# A TV with a long name: the announcement is cut to 60 characters, which
# leaves the first 36 of the id's 40.
_LONG_TV_NAME = f"Living Room BRAVIA 8 II-{_TV_UID[:36]}._sonysmarthome._tcp.local."


async def test_zeroconf_knows_a_configured_tv_by_the_beginning_of_its_id(
    hass: HomeAssistant,
) -> None:
    """It used to find no id in such a name, miss the entry, and offer the TV
    as a new discovery whenever its address changed."""
    from unittest.mock import MagicMock

    entry = _configured_tv(hass, state=config_entries.ConfigEntryState.LOADED)
    coordinator = MagicMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    result = await _init_zeroconf(hass, _LONG_TV_NAME, "192.0.2.99", 40000)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    coordinator.async_note_discovery.assert_called_once_with("192.0.2.99", 40000)
    assert entry.data["host"] == "192.0.2.10"
    hass.data[DOMAIN].pop(entry.entry_id)


async def test_zeroconf_knows_a_tv_stored_with_the_beginning_of_its_id(
    hass: HomeAssistant,
) -> None:
    """The other way round: paired while its name was long, announced whole
    after a rename. And another TV whose id begins differently is not it."""
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.bravia_tv_grpc.const import CONF_DEVICE_UNIQUE_ID

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-123",
        data={
            "host": "192.0.2.10",
            CONF_GRPC_PORT: 36547,
            CONF_DEVICE_UNIQUE_ID: _TV_UID[:36],
        },
        state=config_entries.ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    result = await _init_zeroconf(hass, _TV_NAME, "192.0.2.99", 40000)
    await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    coordinator.async_note_discovery.assert_called_once_with("192.0.2.99", 40000)

    other = f"BRAVIA 9-ff{_TV_UID[2:]}._sonysmarthome._tcp.local."
    result = await _init_zeroconf(hass, other, "192.0.2.77", 40001)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "oauth"
    coordinator.async_note_discovery.assert_called_once()
    hass.data[DOMAIN].pop(entry.entry_id)


async def test_zeroconf_pairs_a_long_named_tv_by_the_beginning_of_its_id(
    hass: HomeAssistant,
) -> None:
    result = await _init_zeroconf(hass, _LONG_TV_NAME, "192.0.2.10", 36547)
    assert result["step_id"] == "oauth"
    # The card names the TV without the piece of id.
    flow = next(iter(hass.config_entries.flow.async_progress()))
    assert flow["context"]["title_placeholders"] == {"name": "Living Room BRAVIA 8 II"}

    mock = AsyncMock(return_value=CREDS)
    with (
        patch(f"{CF}.async_complete_oauth_flow", new=mock),
        patch(f"{CF}.credentials_to_json", return_value="{}"),
        patch("custom_components.bravia_tv_grpc.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": "ssh-app://signin?code=x"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Pairing is bound to the device by what there is of its id, and the same
    # is stored for recognising the TV later.
    assert mock.call_args.kwargs["device_unique_id"] == _TV_UID[:36]
    assert result["data"]["device_unique_id"] == _TV_UID[:36]


async def test_user_flow_resolves_the_beginning_of_a_long_named_tvs_id(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    resolved = (36547, _LONG_TV_NAME, {})
    with (
        patch(f"{CF}.async_resolve_device_mdns", new=AsyncMock(return_value=resolved)),
        patch(f"{CF}.start_oauth_login", return_value=("http://auth", "v", "s")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.0.2.10"}
        )
    mock = AsyncMock(return_value=CREDS)
    with (
        patch(f"{CF}.async_complete_oauth_flow", new=mock),
        patch(f"{CF}.credentials_to_json", return_value="{}"),
        patch("custom_components.bravia_tv_grpc.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": "ssh-app://signin?code=x"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock.call_args.kwargs["device_unique_id"] == _TV_UID[:36]


async def test_options_flow_sets_manual_port(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="dev-123", data={"host": "1.2.3.4"}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_GRPC_PORT: 40000}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GRPC_PORT] == 40000


async def test_options_flow_stores_every_option(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="dev-123", data={"host": "1.2.3.4"}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    chosen = {
        CONF_GRPC_PORT: 41234,
        "agent_update_checks": False,
        "agent_manifest_url": "  https://example.com/agent.json ",
        "rest_psk": " 1234 ",
        "picture_poll_s": 30,
        "picture_event_refresh": False,
        "notify_icon": "alert",
        "notify_duration": 15,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], chosen
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_GRPC_PORT: 41234,
        "agent_update_checks": False,
        "agent_manifest_url": "https://example.com/agent.json",
        "rest_psk": "1234",
        "picture_poll_s": 30,
        "picture_event_refresh": False,
        "notify_icon": "alert",
        "notify_duration": 15,
    }


async def test_options_flow_refuses_a_manifest_url_that_is_not_one(
    hass: HomeAssistant,
) -> None:
    """The agent is told to download and install what this address names."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="dev-123", data={"host": "1.2.3.4"}
    )
    entry.add_to_hass(hass)
    for bad in ("file:///etc/passwd", "agent.json", "ftp://example.com/a", "https://"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"agent_manifest_url": bad, "rest_psk": "typed"}
        )
        assert result["type"] is FlowResultType.FORM, bad
        assert result["errors"] == {"agent_manifest_url": "invalid_url"}
        hass.config_entries.options.async_abort(result["flow_id"])
    assert "agent_manifest_url" not in entry.options
    # Out-of-range numbers never reach the handler.
    import voluptuous as vol

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(vol.Invalid):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_GRPC_PORT: 70000}
        )


async def _start_reauth(hass: HomeAssistant, entry):
    with patch(f"{CF}.start_oauth_login", return_value=("http://auth", "v", "s")):
        entry.async_start_reauth(hass)
        await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1 and flows[0]["step_id"] == "oauth"
    return flows[0]["flow_id"]


async def test_reauth_stores_fresh_keys_for_the_same_tv(hass: HomeAssistant) -> None:
    from custom_components.bravia_tv_grpc.const import CONF_GRPC_KEYS

    entry = _configured_tv(hass)
    flow_id = await _start_reauth(hass, entry)
    fresh = {"device_id": "dev-123", "hmac_key": "new", "refresh_token": "rt-2"}
    oauth = AsyncMock(return_value=fresh)
    with (
        patch(f"{CF}.async_complete_oauth_flow", new=oauth),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload,
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"redirect_url": "ssh-app://signin?code=x&state=s"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert '"rt-2"' in entry.data[CONF_GRPC_KEYS]
    assert entry.data[CONF_GRPC_DEVICE_ID] == "dev-123"
    assert entry.data["host"] == "192.0.2.10"  # everything else is kept
    reload.assert_awaited_once_with(entry.entry_id)
    # The sign-in was for this TV, checked against this attempt's state.
    assert oauth.await_args.kwargs["expected_state"] == "s"


async def test_reauth_refuses_another_tvs_keys(hass: HomeAssistant) -> None:
    from custom_components.bravia_tv_grpc.const import CONF_GRPC_KEYS

    entry = _configured_tv(hass)
    flow_id = await _start_reauth(hass, entry)
    with (
        patch(
            f"{CF}.async_complete_oauth_flow",
            new=AsyncMock(
                return_value={"device_id": "someone-elses-tv", "hmac_key": "k"}
            ),
        ),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload,
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"redirect_url": "ssh-app://signin?code=x"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert CONF_GRPC_KEYS not in entry.data
    reload.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "key"),
    [("oauth", "oauth_failed"), ("other", "pairing_failed")],
)
async def test_sign_in_errors_keep_the_form(hass: HomeAssistant, error, key) -> None:
    from custom_components.bravia_tv_grpc.grpc.credentials import GrpcOAuthError

    raised = (
        GrpcOAuthError("state mismatch") if error == "oauth" else OSError("HTTP 400")
    )
    result = await _init_zeroconf(hass, _TV_NAME, "192.0.2.10", 36547)
    with patch(f"{CF}.async_complete_oauth_flow", new=AsyncMock(side_effect=raised)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": "ssh-app://signin?code=x"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    hass.config_entries.flow.async_abort(result["flow_id"])
