"""Setup / recovery tests for async_setup_entry (require HA test harness).

Focus: a TV reboot invalidates the gRPC session keys. Recovery must retry
transient failures (HA backs off and self-heals) and only force the user
through re-authentication when the Sony refresh token is genuinely rejected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.const import CONF_HOST  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.bravia_tv_grpc.bravia_tv_client import (  # noqa: E402
    BraviaTvAuthError,
)
from custom_components.bravia_tv_grpc.const import (  # noqa: E402
    CONF_GRPC_DEVICE_ID,
    CONF_GRPC_KEYS,
    DOMAIN,
)
from custom_components.bravia_tv_grpc.grpc.credentials import (  # noqa: E402
    GrpcCredentialsRefreshError,
)

INTG = "custom_components.bravia_tv_grpc"


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    with patch("homeassistant.components.zeroconf.async_setup", return_value=True):
        yield


def _entry(hass: HomeAssistant):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev",
        title="Bravia TV (x)",
        data={
            CONF_HOST: "192.0.2.1",
            CONF_GRPC_KEYS: "{}",
            CONF_GRPC_DEVICE_ID: "dev",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _run_setup(hass, entry, *, connect_side_effect, refresh_side_effect):
    with (
        patch(f"{INTG}._resolve_port", new=AsyncMock(return_value=12345)),
        patch(
            f"{INTG}.cred.parse_credentials_json",
            return_value={"device_id": "dev", "hmac_key": "ab"},
        ),
        patch(f"{INTG}._connect", new=AsyncMock(side_effect=connect_side_effect)),
        patch(
            f"{INTG}._refresh_credentials",
            new=AsyncMock(side_effect=refresh_side_effect),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_transient_refresh_failure_retries(hass: HomeAssistant) -> None:
    """A reset connection during the cloud key refresh is transient: HA should
    retry, not demand re-authentication."""
    entry = _entry(hass)
    await _run_setup(
        hass,
        entry,
        connect_side_effect=BraviaTvAuthError("handshake failed"),
        # No HTTP status -> looks like a dropped connection.
        refresh_side_effect=GrpcCredentialsRefreshError("connection reset"),
    )
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not list(entry.async_get_active_flows(hass, {"reauth"}))


async def test_rejected_refresh_token_triggers_reauth(hass: HomeAssistant) -> None:
    """A 401 from Sony means the refresh token is dead: only re-authentication
    recovers, so the reauth flow must start."""
    entry = _entry(hass)
    await _run_setup(
        hass,
        entry,
        connect_side_effect=BraviaTvAuthError("handshake failed"),
        refresh_side_effect=GrpcCredentialsRefreshError("HTTP 401", status=401),
    )
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert list(entry.async_get_active_flows(hass, {"reauth"}))


async def test_refreshed_keys_still_rejected_retries(hass: HomeAssistant) -> None:
    """Fresh, valid cloud keys rejected by the TV (still booting) is transient:
    retry rather than force a re-login that would yield equivalent keys."""
    entry = _entry(hass)
    await _run_setup(
        hass,
        entry,
        # First connect fails; refresh succeeds; second connect fails again.
        connect_side_effect=[
            BraviaTvAuthError("handshake failed"),
            BraviaTvAuthError("handshake failed: INVALID_ARGUMENT"),
        ],
        refresh_side_effect=None,  # AsyncMock returns a MagicMock (fresh creds)
    )
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not list(entry.async_get_active_flows(hass, {"reauth"}))
