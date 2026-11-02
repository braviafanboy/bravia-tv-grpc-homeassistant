"""Remote hold_secs handling (press / release through the any-typed remote_key)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.remote import ATTR_HOLD_SECS  # noqa: E402

from custom_components.bravia_tv_grpc.remote import (  # noqa: E402
    _COMMANDS,
    BraviaTvRemote,
)


def _remote() -> BraviaTvRemote:
    coordinator = MagicMock()
    coordinator.config_entry.unique_id = "dev"
    coordinator.config_entry.data = {}
    coordinator.device_name = "TV"
    coordinator.data = {"power": True}
    coordinator.client.capabilities = {
        "remote_key": object(),
        "remote_key.d_pad": object(),
    }
    coordinator.async_send_key = AsyncMock(return_value=True)
    return BraviaTvRemote(coordinator, dict(_COMMANDS))


def _sent(remote: BraviaTvRemote) -> list[tuple[str, object]]:
    return [
        (c.args[0], json.loads(c.args[1]) if c.args[0] == "remote_key" else c.args[1])
        for c in remote.coordinator.async_send_key.await_args_list
    ]


async def test_plain_command_uses_enum_path() -> None:
    remote = _remote()
    await remote.async_send_command(["center"])
    assert _sent(remote) == [("remote_key.d_pad", "center")]


async def test_short_hold_is_press_then_release() -> None:
    remote = _remote()
    with patch(
        "custom_components.bravia_tv_grpc.remote.asyncio.sleep", new=AsyncMock()
    ):
        await remote.async_send_command(["center"], **{ATTR_HOLD_SECS: 0.3})
    assert _sent(remote) == [
        ("remote_key", {"action": "press", "key": "dpad_center"}),
        ("remote_key", {"action": "release", "key": "dpad_center"}),
    ]


async def test_long_hold_is_press_only() -> None:
    # The TV completes a held key itself after one second; a release sent after
    # that is rejected, so a long hold must not send one.
    remote = _remote()
    with patch(
        "custom_components.bravia_tv_grpc.remote.asyncio.sleep", new=AsyncMock()
    ):
        await remote.async_send_command(["home"], **{ATTR_HOLD_SECS: 3})
    assert _sent(remote) == [("remote_key", {"action": "press", "key": "home"})]


async def test_hold_without_any_path_falls_back() -> None:
    remote = _remote()
    remote.coordinator.client.capabilities = {"remote_key.d_pad": object()}
    with patch(
        "custom_components.bravia_tv_grpc.remote.asyncio.sleep", new=AsyncMock()
    ):
        await remote.async_send_command(["home"], **{ATTR_HOLD_SECS: 2})
    assert _sent(remote) == [("remote_key.d_pad", "home")]
