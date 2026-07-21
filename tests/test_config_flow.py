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
    with patch(f"{CF}.discover_grpc_port", return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_grpc_service"}


async def test_duplicate_aborts(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(domain=DOMAIN, unique_id="dev-123", data={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with (
        patch(f"{CF}.discover_grpc_port", return_value=36547),
        patch(f"{CF}.start_oauth_login", return_value=("http://auth", "v", "s")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    result = await _finish_oauth(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


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
