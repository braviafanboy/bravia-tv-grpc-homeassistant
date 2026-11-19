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
    _device_entry_ids,
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
    a.async_restore_picture_profile.assert_awaited_once_with(
        "Calibrated", True, picture_mode=None
    )
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


def test_device_entry_ids_across_cores():
    """HA 2026.8+ devices have one config_entry_id; older cores only a set."""
    from types import SimpleNamespace

    assert _device_entry_ids(SimpleNamespace(config_entry_id="e1")) == {"e1"}
    assert _device_entry_ids(SimpleNamespace(config_entries={"e1", "e2"})) == {
        "e1",
        "e2",
    }


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("show_dashboard", {}),  # no dashboard
        ("show_dashboard", {"dashboard": "lovelace/0", "mode": "sideways"}),
        ("show_dashboard", {"dashboard": "lovelace/0", "position": "middle"}),
        ("show_dashboard", {"dashboard": "lovelace/0", "size": 19}),
        ("show_dashboard", {"dashboard": "lovelace/0", "size": 101}),
        ("show_dashboard", {"dashboard": "lovelace/0", "duration": -1}),
        ("show_dashboard", {"dashboard": "lovelace/0", "duration": 3601}),
        ("show_dashboard", {"dashboard": "lovelace/0", "kiosk": "maybe"}),
        ("show_dashboard", {"dashboard": "lovelace/0", "extra": 1}),
        ("save_picture_profile", {}),  # no name
        ("restore_picture_profile", {"name": "a", "force": "perhaps"}),
        ("set_ambient_curves", {"curves": "not an object"}),
        ("set_ambient_curves", {}),
    ],
)
async def test_actions_refuse_bad_input(hass: HomeAssistant, tvs, service, data):
    """Nothing malformed reaches the TV: the action's own schema says no."""
    import voluptuous as vol

    (first, device), _ = tvs
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, service, {"device_id": device.id, **data}, blocking=True
        )
    for method in (
        first.async_show_dashboard,
        first.async_save_picture_profile,
        first.async_restore_picture_profile,
        first.async_ambient,
    ):
        method.assert_not_called()


async def test_every_action_needs_a_tv(hass: HomeAssistant, tvs):
    from homeassistant.core import SupportsResponse
    import voluptuous as vol

    services = hass.services.async_services()[DOMAIN]
    assert len(services) == 9
    for service in services:
        only = hass.services.supports_response(DOMAIN, service) is SupportsResponse.ONLY
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, service, {}, blocking=True, return_response=only
            )
