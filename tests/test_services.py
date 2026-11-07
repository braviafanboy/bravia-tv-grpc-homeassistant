"""TV-wide actions: routed to the TV named by device_id."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import (  # noqa: E402
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr  # noqa: E402

from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.services import (  # noqa: E402
    async_setup_services,
)


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _tv(hass: HomeAssistant, uid: str):
    """A stand-in coordinator stored like async_setup_entry does, with its device."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id=uid)
    entry.add_to_hass(hass)
    c = MagicMock()
    c.device_name = f"TV {uid}"
    c.async_save_picture_profile = AsyncMock(return_value={"saved": uid})
    c.async_restore_picture_profile = AsyncMock(return_value={"restored": uid})
    c.async_delete_picture_profile = AsyncMock(return_value={})
    c.async_list_picture_profiles = AsyncMock(return_value={"profiles": [uid]})
    c.async_compare_picture_profile = AsyncMock(return_value={"diff": uid})
    c.async_ambient = AsyncMock()
    c.agent_value = MagicMock(
        return_value={"anchors_lux": [0, 10], "curves": {"default": [1]}}
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = c
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, uid)}
    )
    return c, device


@pytest.fixture
def tvs(hass: HomeAssistant):
    a = _tv(hass, "a")
    b = _tv(hass, "b")
    async_setup_services(hass)
    return a, b


async def test_routes_to_the_targeted_tv(hass: HomeAssistant, tvs):
    (a, dev_a), (b, _) = tvs
    await hass.services.async_call(
        DOMAIN,
        "restore_picture_profile",
        {"device_id": dev_a.id, "name": "Calibrated", "force": True},
        blocking=True,
    )
    a.async_restore_picture_profile.assert_awaited_once_with("Calibrated", True)
    b.async_restore_picture_profile.assert_not_awaited()

    await hass.services.async_call(
        DOMAIN,
        "set_ambient_curves",
        {"device_id": dev_a.id, "curves": {"default": [1, 2, 3, 4, 5, 6]}},
        blocking=True,
    )
    a.async_ambient.assert_awaited_once_with("curves", {"default": [1, 2, 3, 4, 5, 6]})


async def test_responses(hass: HomeAssistant, tvs):
    (a, dev_a), _ = tvs
    assert await hass.services.async_call(
        DOMAIN,
        "list_picture_profiles",
        {"device_id": dev_a.id},
        blocking=True,
        return_response=True,
    ) == {"profiles": ["a"]}
    assert await hass.services.async_call(
        DOMAIN,
        "compare_picture_profiles",
        {"device_id": dev_a.id, "name": "x", "other": "y"},
        blocking=True,
        return_response=True,
    ) == {"diff": "a"}
    a.async_compare_picture_profile.assert_awaited_once_with("x", "y")
    assert await hass.services.async_call(
        DOMAIN,
        "save_picture_profile",
        {"device_id": dev_a.id, "name": "n"},
        blocking=True,
        return_response=True,
    ) == {"saved": "a"}
    assert await hass.services.async_call(
        DOMAIN,
        "get_ambient_curves",
        {"device_id": dev_a.id},
        blocking=True,
        return_response=True,
    ) == {"anchors_lux": [0, 10], "curves": {"default": [1]}}


async def test_unknown_device(hass: HomeAssistant, tvs):
    (a, _), _ = tvs
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "list_picture_profiles",
            {"device_id": "not-a-device"},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "no_tv_targeted"
    a.async_list_picture_profiles.assert_not_awaited()


async def test_ambient_unavailable(hass: HomeAssistant, tvs):
    (a, dev_a), _ = tvs
    a.agent_value.return_value = None
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "get_ambient_curves",
            {"device_id": dev_a.id},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "ambient_unavailable"
