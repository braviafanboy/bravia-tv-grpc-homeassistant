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
        patch(f"{CF}.discover_grpc_port", return_value=36547),
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
        patch(f"{CF}.discover_grpc_port", return_value=None),
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
        == "51b397cf985b8b061839034fef909670e29196d2"
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
        patch(f"{CF}.discover_grpc_port", return_value=36547),
        patch(f"{CF}.start_oauth_login", return_value=("http://auth", "v", "s")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    result = await _finish_oauth(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


_TV_NAME = (
    "BRAVIA 8 II-51b397cf985b8b061839034fef909670e29196d2._sonysmarthome._tcp.local."
)
_SB_NAME = (
    "BRAVIA Theatre Quad-bac76409bb5c6c22c700fe9795c0e17d91022acc"
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
        == "bac76409bb5c6c22c700fe9795c0e17d91022acc"
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
        == "51b397cf985b8b061839034fef909670e29196d2"
    )


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
