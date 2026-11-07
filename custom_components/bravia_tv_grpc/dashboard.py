"""Home Assistant dashboards on the TV, through the HA TV Agent.

The TV's WebView signs in to Home Assistant the way the official Android app
does (the frontend's external-auth interface), with short-lived access tokens
this integration issues and sends over the local, pinned agent link. Nothing
goes through Sony's cloud.

Tokens belong to one dedicated, non-admin, local-only user ("HA TV Agent
display") created on first use and shared by every TV. Home Assistant has no
per-dashboard token scope, so the TV can see what that user can see; it never
holds a long-lived credential, only a 30-minute access token kept in memory.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.models import RefreshToken, User
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DISPLAY_USER_NAME = "HA TV Agent display"
CLIENT_ID = "https://github.com/braviafanboy/bravia-tv-agent"
CLIENT_NAME = "HA TV Agent dashboards"
TOKEN_LIFETIME = timedelta(minutes=30)

MODES = ("full", "overlay")
POSITIONS = (
    "right",
    "left",
    "full",
    "top_right",
    "top_left",
    "bottom_right",
    "bottom_left",
)
MIN_SIZE = 20
MAX_SIZE = 100
MAX_DURATION = 3600

_STORE_KEY = f"{DOMAIN}.dashboard_user"
_STORE_VERSION = 1
_DATA_LOCK = f"{DOMAIN}_dashboard_user_lock"


class DashboardPathError(ValueError):
    """The dashboard is not a plain Home Assistant path."""


def dashboard_path(value: str) -> str:
    """Normalise a dashboard path ("lovelace/0", "/energy"). Full URLs are
    refused: the TV hands its token to whatever origin it loads, so only
    this Home Assistant's own pages are allowed."""
    path = (value or "").strip()
    if not path or "://" in path or path.startswith("//") or "\\" in path:
        raise DashboardPathError(value)
    return path.lstrip("/")


def base_url(hass: HomeAssistant) -> str:
    """Home Assistant's address as the TV on the LAN should reach it."""
    return get_url(hass, allow_cloud=False, prefer_external=False).rstrip("/")


def dashboard_url(hass: HomeAssistant, path: str) -> str:
    """Absolute URL of a dashboard path. Raises NoURLAvailableError."""
    return f"{base_url(hass)}/{dashboard_path(path)}"


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    lock = hass.data.get(_DATA_LOCK)
    if lock is None:
        lock = hass.data[_DATA_LOCK] = asyncio.Lock()
    return lock


async def async_display_user(hass: HomeAssistant) -> User:
    """The dedicated display user, created on first use."""
    async with _lock(hass):
        store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, _STORE_KEY)
        data = await store.async_load() or {}
        user_id = data.get("user_id")
        if user_id:
            user = await hass.auth.async_get_user(user_id)
            if user is not None:
                return user
        user = await hass.auth.async_create_user(
            DISPLAY_USER_NAME, group_ids=[GROUP_ID_USER], local_only=True
        )
        await store.async_save({"user_id": user.id})
        _LOGGER.info("Created the %s user for dashboards on the TV", DISPLAY_USER_NAME)
        return user


async def _async_refresh_token(hass: HomeAssistant, user: User) -> RefreshToken:
    for token in user.refresh_tokens.values():
        if token.client_id == CLIENT_ID:
            return token
    return await hass.auth.async_create_refresh_token(
        user,
        client_id=CLIENT_ID,
        client_name=CLIENT_NAME,
        access_token_expiration=TOKEN_LIFETIME,
    )


async def async_access_token(hass: HomeAssistant) -> tuple[str, int]:
    """A fresh access token for the display user and its lifetime in seconds."""
    user = await async_display_user(hass)
    refresh = await _async_refresh_token(hass, user)
    token = hass.auth.async_create_access_token(refresh)
    return token, int(refresh.access_token_expiration.total_seconds())


async def async_remove_display_user(hass: HomeAssistant) -> None:
    """Remove the display user (the last TV was removed)."""
    store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, _STORE_KEY)
    data = await store.async_load() or {}
    user_id = data.get("user_id")
    if user_id:
        user = await hass.auth.async_get_user(user_id)
        if user is not None:
            await hass.auth.async_remove_user(user)
            _LOGGER.info("Removed the %s user", DISPLAY_USER_NAME)
    await store.async_remove()


__all__ = [
    "MAX_DURATION",
    "MAX_SIZE",
    "MIN_SIZE",
    "MODES",
    "POSITIONS",
    "DashboardPathError",
    "NoURLAvailableError",
    "async_access_token",
    "async_display_user",
    "async_remove_display_user",
    "dashboard_url",
]
