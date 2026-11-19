"""Home Assistant dashboards on the TV, through the HA TV Agent.

The TV's WebView signs in to Home Assistant the way the official Android app
does (the frontend's external-auth interface), with short-lived access tokens
this integration issues and sends over the local, pinned agent link. Nothing
goes through Sony's cloud.

Tokens belong to dedicated, non-admin, local-only users shared by every TV,
created the first time a dashboard is shown:

- "HA TV Agent display" (the ordinary users group) for a full-screen
  dashboard, which is operated with the remote;
- "HA TV Agent display (view only)" (the read-only group) for an overlay,
  which is only ever looked at: its token cannot call a single action.

Home Assistant has no per-dashboard token scope, so the TV can see what those
users can see. It never holds a long-lived credential, only a 30-minute access
token kept in memory, and it only ever gets one while a dashboard that Home
Assistant itself opened is showing (a "session"): a token is never handed out
just because the other end of the agent link asks for one.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.auth.const import GROUP_ID_READ_ONLY, GROUP_ID_USER
from homeassistant.auth.models import RefreshToken, User
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DISPLAY_USER_NAME = "HA TV Agent display"
VIEW_USER_NAME = "HA TV Agent display (view only)"
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
# An overlay without a duration stays up this long (the agent's default).
_OVERLAY_DEFAULT_S = 30
# Slack on a session's end, for the page's last token request.
_SESSION_GRACE_S = 60
# A "not showing" report this soon after opening may predate the open.
SESSION_SETTLE_S = 5.0

_STORE_KEY = f"{DOMAIN}.dashboard_user"
_STORE_VERSION = 1
_DATA_LOCK = f"{DOMAIN}_dashboard_user_lock"
_DATA_STORE = f"{DOMAIN}_dashboard_store"
# Store keys of the two users, by whether the dashboard is operated.
_USER_KEYS = {True: "user_id", False: "view_user_id"}


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


def interactive_mode(mode: str | None) -> bool:
    """Whether a dashboard in ``mode`` is operated (full screen) rather than
    only looked at (an overlay; the remote keeps controlling the app behind)."""
    return mode != "overlay"


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    lock = hass.data.get(_DATA_LOCK)
    if lock is None:
        lock = hass.data[_DATA_LOCK] = asyncio.Lock()
    return lock


async def _async_load(hass: HomeAssistant) -> tuple[Store[dict[str, Any]], dict]:
    """The store and its data, read from disk once per run. Call with the
    lock held."""
    cached = hass.data.get(_DATA_STORE)
    if cached is None:
        store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, _STORE_KEY)
        data = await store.async_load()
        cached = hass.data[_DATA_STORE] = (
            store,
            data if isinstance(data, dict) else {},
        )
    return cached


async def async_display_user(hass: HomeAssistant, *, interactive: bool = True) -> User:
    """A dedicated display user, created on first use: the ordinary one for
    a dashboard that is operated, the view-only one otherwise."""
    key = _USER_KEYS[interactive]
    async with _lock(hass):
        store, data = await _async_load(hass)
        user_id = data.get(key)
        if user_id:
            user = await hass.auth.async_get_user(user_id)
            if user is not None:
                return user
        # Home Assistant makes the first user ever created its owner; the TV's
        # user must never be that, so wait until onboarding has made one.
        if not any(not u.system_generated for u in await hass.auth.async_get_users()):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="dashboard_no_owner"
            )
        name = DISPLAY_USER_NAME if interactive else VIEW_USER_NAME
        user = await hass.auth.async_create_user(
            name,
            group_ids=[GROUP_ID_USER if interactive else GROUP_ID_READ_ONLY],
            local_only=True,
        )
        data[key] = user.id
        await store.async_save(data)
        _LOGGER.info("Created the %s user for dashboards on the TV", name)
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


async def async_access_token(
    hass: HomeAssistant, *, interactive: bool = True
) -> tuple[str, int]:
    """A fresh access token for a display user and its lifetime in seconds."""
    user = await async_display_user(hass, interactive=interactive)
    refresh = await _async_refresh_token(hass, user)
    token = hass.auth.async_create_access_token(refresh)
    return token, int(refresh.access_token_expiration.total_seconds())


# ---- sessions: which TV has a dashboard that Home Assistant opened ----------


def session_alive(session: Any, now: float | None = None) -> bool:
    """Whether a dashboard session exists and has not run out."""
    now = time.time() if now is None else now
    if not isinstance(session, dict):
        return False
    expires = session.get("expires")
    return expires is None or (isinstance(expires, int | float) and now < expires)


async def async_open_session(
    hass: HomeAssistant, entry_id: str, mode: str, duration: int | None
) -> dict[str, Any]:
    """Record that Home Assistant opened a dashboard on a TV. Tokens are only
    sent to that TV while this stands."""
    now = time.time()
    if duration:
        expires: float | None = now + duration + _SESSION_GRACE_S
    elif interactive_mode(mode):
        expires = None  # full screen without a duration: until it is closed
    else:
        expires = now + _OVERLAY_DEFAULT_S + _SESSION_GRACE_S
    session = {"mode": mode, "opened": now, "expires": expires}
    async with _lock(hass):
        store, data = await _async_load(hass)
        data.setdefault("sessions", {})[entry_id] = session
        await store.async_save(data)
    return session


async def async_get_session(
    hass: HomeAssistant, entry_id: str
) -> dict[str, Any] | None:
    """The TV's open dashboard session, if it has one that has not run out."""
    async with _lock(hass):
        _, data = await _async_load(hass)
        session = (data.get("sessions") or {}).get(entry_id)
    return session if session_alive(session) else None


async def async_close_session(hass: HomeAssistant, entry_id: str) -> None:
    """Forget a TV's dashboard session (closed, timed out, or the TV went)."""
    async with _lock(hass):
        store, data = await _async_load(hass)
        sessions = data.get("sessions") or {}
        if entry_id in sessions:
            del sessions[entry_id]
            await store.async_save(data)


async def async_remove_display_user(hass: HomeAssistant) -> None:
    """Remove the display users (the last TV was removed)."""
    async with _lock(hass):
        store, data = await _async_load(hass)
        for key, name in (
            ("user_id", DISPLAY_USER_NAME),
            ("view_user_id", VIEW_USER_NAME),
        ):
            user_id = data.get(key)
            if not user_id:
                continue
            user = await hass.auth.async_get_user(user_id)
            if user is not None:
                await hass.auth.async_remove_user(user)
                _LOGGER.info("Removed the %s user", name)
        await store.async_remove()
        hass.data.pop(_DATA_STORE, None)


__all__ = [
    "MAX_DURATION",
    "MAX_SIZE",
    "MIN_SIZE",
    "MODES",
    "POSITIONS",
    "DashboardPathError",
    "NoURLAvailableError",
    "async_access_token",
    "async_close_session",
    "async_display_user",
    "async_get_session",
    "async_open_session",
    "async_remove_display_user",
    "dashboard_url",
    "interactive_mode",
    "session_alive",
]
