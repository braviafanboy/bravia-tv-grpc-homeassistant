"""Integration actions that act on a TV as a whole rather than one of its entities.

Dashboards on the TV, picture profiles and the ambient brightness curves are
about the TV (and mostly need the HA TV Agent), not about media playback, so
these actions take the TV as a ``device_id`` field with a device selector
limited to this integration. (Home Assistant does not allow device filters on
an action's target.) One TV per call. Registered once in ``async_setup``.
"""

from __future__ import annotations

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from . import dashboard as dashboards
from .const import DOMAIN

ATTR_DEVICE_ID = "device_id"
SERVICE_SHOW_DASHBOARD = "show_dashboard"
SERVICE_CLOSE_DASHBOARD = "close_dashboard"

_TV = {vol.Required(ATTR_DEVICE_ID): cv.string}

SHOW_DASHBOARD_SCHEMA = vol.Schema(
    {
        **_TV,
        vol.Required("dashboard"): cv.string,
        vol.Optional("mode", default="full"): vol.In(dashboards.MODES),
        vol.Optional("position", default="right"): vol.In(dashboards.POSITIONS),
        vol.Optional("size", default=40): vol.All(
            vol.Coerce(int),
            vol.Range(min=dashboards.MIN_SIZE, max=dashboards.MAX_SIZE),
        ),
        vol.Optional("duration"): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=dashboards.MAX_DURATION)
        ),
        # Omitted: hidden title bar for an overlay, shown for full screen.
        vol.Optional("kiosk"): cv.boolean,
    }
)
_TV_ONLY = vol.Schema(_TV)
_NAME = vol.Schema({**_TV, vol.Required("name"): cv.string})
_RESTORE = vol.Schema(
    {
        **_TV,
        vol.Required("name"): cv.string,
        vol.Optional("force", default=False): cv.boolean,
    }
)
_COMPARE = vol.Schema(
    {**_TV, vol.Required("name"): cv.string, vol.Optional("other"): cv.string}
)
_CURVES = vol.Schema({**_TV, vol.Required("curves"): dict})


def _device_entry_ids(device: dr.DeviceEntry) -> set[str]:
    """The config entry IDs a device belongs to.

    HA 2026.8 gave a device a single ``config_entry_id`` and deprecated the
    ``config_entries`` set (removed in 2027.10); older cores only have the set.
    """
    entry_id = getattr(device, "config_entry_id", None)
    return {entry_id} if entry_id else set(device.config_entries)


def _coordinator(hass: HomeAssistant, call: ServiceCall):
    """The TV (its coordinator) named by the call's device_id."""
    loaded = hass.data.get(DOMAIN, {})
    device = dr.async_get(hass).async_get(call.data[ATTR_DEVICE_ID])
    if device is not None:
        for entry_id in _device_entry_ids(device):
            if entry_id in loaded:
                return loaded[entry_id]
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="no_tv_targeted"
    )


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the TV-wide actions."""

    async def show_dashboard(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_show_dashboard(
            call.data["dashboard"],
            mode=call.data["mode"],
            position=call.data["position"],
            size=call.data["size"],
            duration=call.data.get("duration"),
            kiosk=call.data.get("kiosk"),
        )

    async def close_dashboard(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_close_dashboard()

    async def save_picture_profile(call: ServiceCall) -> ServiceResponse:
        return await _coordinator(hass, call).async_save_picture_profile(
            call.data["name"]
        )

    async def restore_picture_profile(call: ServiceCall) -> ServiceResponse:
        return await _coordinator(hass, call).async_restore_picture_profile(
            call.data["name"], call.data["force"]
        )

    async def delete_picture_profile(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_delete_picture_profile(call.data["name"])

    async def list_picture_profiles(call: ServiceCall) -> ServiceResponse:
        return await _coordinator(hass, call).async_list_picture_profiles()

    async def compare_picture_profiles(call: ServiceCall) -> ServiceResponse:
        return await _coordinator(hass, call).async_compare_picture_profile(
            call.data["name"], call.data.get("other")
        )

    async def set_ambient_curves(call: ServiceCall) -> None:
        # {"default": [6 values], "modes": {"<picture mode>": [6 values], ...}}
        await _coordinator(hass, call).async_ambient("curves", call.data["curves"])

    async def get_ambient_curves(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        status = coordinator.agent_value("ambient")
        if not isinstance(status, dict):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ambient_unavailable",
                translation_placeholders={"name": coordinator.device_name},
            )
        return {
            "anchors_lux": status.get("anchors_lux"),
            "curves": status.get("curves"),
        }

    register = hass.services.async_register
    register(
        DOMAIN, SERVICE_SHOW_DASHBOARD, show_dashboard, schema=SHOW_DASHBOARD_SCHEMA
    )
    register(DOMAIN, SERVICE_CLOSE_DASHBOARD, close_dashboard, schema=_TV_ONLY)
    register(
        DOMAIN,
        "save_picture_profile",
        save_picture_profile,
        schema=_NAME,
        supports_response=SupportsResponse.OPTIONAL,
    )
    register(
        DOMAIN,
        "restore_picture_profile",
        restore_picture_profile,
        schema=_RESTORE,
        supports_response=SupportsResponse.OPTIONAL,
    )
    register(DOMAIN, "delete_picture_profile", delete_picture_profile, schema=_NAME)
    register(
        DOMAIN,
        "list_picture_profiles",
        list_picture_profiles,
        schema=_TV_ONLY,
        supports_response=SupportsResponse.ONLY,
    )
    register(
        DOMAIN,
        "compare_picture_profiles",
        compare_picture_profiles,
        schema=_COMPARE,
        supports_response=SupportsResponse.ONLY,
    )
    register(DOMAIN, "set_ambient_curves", set_ambient_curves, schema=_CURVES)
    register(
        DOMAIN,
        "get_ambient_curves",
        get_ambient_curves,
        schema=_TV_ONLY,
        supports_response=SupportsResponse.ONLY,
    )
