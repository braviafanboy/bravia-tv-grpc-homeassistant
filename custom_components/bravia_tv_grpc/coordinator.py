"""State coordinator for the Bravia TV (gRPC) integration.

Owns the gRPC client, seeds an initial snapshot from the Sony IoT cloud (the
gRPC StartNotifyStates stream only emits on change, not on subscribe), and
turns push deltas into Home Assistant state updates. All blocking gRPC work is
run in the executor; the notify worker is a background thread that hands values
back to the event loop thread-safely.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bravia_tv_client import BraviaTvGrpcClient
from .const import CONF_FW_PENDING, CONF_GRPC_KEYS, DOMAIN
from .grpc import credentials as cred
from .grpc.application_list import APPLICATION_LIST_PATH

_LOGGER = logging.getLogger(__name__)

# Push (StartNotifyStates) is primary; this periodic GetStates is a safety net
# to reconcile missed deltas and to detect (and recover from) a dead connection
# even if the push worker fails to notice — e.g. a stream that hangs on an
# unreachable host after a reboot instead of erroring.
_REFRESH_INTERVAL = timedelta(seconds=60)

# Reportable Sony IoT cloud state name -> gRPC field path, for seeding the
# initial snapshot. Only clean, unambiguous mappings; everything else becomes
# known on the first push delta.
_CLOUD_TO_GRPC_PATH = {
    "power": "power",
    "volume": "volume",
    "brightness": "display_setting.brightness",
    "picture_mode": "display_setting.picture_mode",
    # Cloud reports the current input as a clean label string; the gRPC push
    # (system_setting.tvapp.input) later refreshes it as JSON. The media_player
    # source property accepts either form.
    "input": "system_setting.tvapp.input",
}


def _snake_to_camel(value: str) -> str:
    """`custom_for_pro1` -> `customForPro1` (cloud enum -> gRPC enum vocab)."""
    head, *rest = value.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


# Built-in TV inputs aren't installed apps, so they never appear in the fetched
# application_list and would otherwise surface as a raw package id. The DVB tuner
# input hosts live broadcast, Freeview Play and HbbTV apps (e.g. BBC iPlayer runs
# under it even for on-demand content), so the label reflects all of those.
_SYSTEM_APP_LABELS = {
    "com.sony.dtv.tvinput.dvbtuner": "DVB Tuner / HbbTV",
}


def _icon_uri(app: dict) -> str | None:
    """The app-icon resource URI for an app_list entry, if it has one.

    Prefers a resource explicitly typed ``app-icon``; falls back to the first
    resource carrying a URI.
    """
    resources = app.get("resources")
    if not isinstance(resources, list):
        return None
    typed = next(
        (r for r in resources if isinstance(r, dict) and r.get("type") == "app-icon"),
        None,
    )
    chosen = typed or next(
        (r for r in resources if isinstance(r, dict) and r.get("uri")), None
    )
    uri = chosen.get("uri") if chosen else None
    return uri if isinstance(uri, str) and uri else None


class BraviaTvCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push-driven coordinator; data is a ``{grpc_path: value}`` dict."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: BraviaTvGrpcClient,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.client = client
        self._entry = entry
        self._state: dict[str, Any] = {}
        self._cancel_refresh: Any = None
        self._refresh_failures = 0
        self._app_list: list[dict] = []
        self._app_list_refreshing = False
        # The manual gRPC port applied at setup; the update listener compares it
        # so runtime data writes (e.g. the firmware-pending flag) don't reload.
        self.applied_manual_port: int | None = None
        # Decrypted app icons, keyed by their content-addressed resource URI (the
        # hash changes when an app's icon changes, so entries never go stale).
        self._icon_cache: dict[str, bytes] = {}

    async def async_start(self) -> None:
        """Connect, fetch schema, seed initial state, and start the push stream."""
        await self.hass.async_add_executor_job(self.client.connect)
        await self.hass.async_add_executor_job(self.client.get_capabilities)
        # Prefer a full on-demand snapshot straight from the TV (GetStates);
        # fall back to the Sony cloud snapshot if that read fails.
        if not await self._seed_from_device():
            await self._seed_from_cloud()
        # Deltas arrive on a worker thread; marshal them onto the loop.
        self.client.start_notify(
            self._on_delta_threadsafe,
            on_connection_lost=self._on_connection_lost,
            on_reconnect=self._on_reconnect,
        )
        self.async_set_updated_data(dict(self._state))
        # Always-on safety net: reconciles missed pushes and, on persistent
        # failure, reloads to recover from a reboot even if the push worker
        # never sees an error.
        self._cancel_refresh = async_track_time_interval(
            self.hass, self._async_periodic_refresh, _REFRESH_INTERVAL
        )
        await self._async_refresh_app_list()

    async def _async_refresh_app_list(self) -> None:
        """Fetch + cache the installed-app list (best-effort).

        Triggered on (re)connect of the push stream and by a push on the
        application_list path (an app was installed/removed). No periodic poll:
        each read is a full list, so any missed change self-heals on the next
        reconnect or app change.
        """
        if self._app_list_refreshing:
            return
        self._app_list_refreshing = True
        try:
            apps = await self.hass.async_add_executor_job(
                self.client.read_application_list
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("application_list read failed", exc_info=True)
            return
        finally:
            self._app_list_refreshing = False
        if apps and apps != self._app_list:
            self._app_list = apps
            # Drop cached icons for apps/URIs no longer present (uninstalled or
            # icon changed) so the cache tracks the live list.
            live_uris = {uri for app in apps if (uri := _icon_uri(app))}
            self._icon_cache = {
                uri: data for uri, data in self._icon_cache.items() if uri in live_uris
            }
            _LOGGER.debug("app list: %d installed apps", len(apps))
            self.async_update_listeners()

    def app_icon_uri(self, package: str | None) -> str | None:
        """The content-addressed icon URI for an installed app package, or None.

        Used as the media_player's ``media_image_url`` so the artwork hash tracks
        the foreground app (and busts when its icon changes)."""
        if not package:
            return None
        for app in self._app_list:
            if app.get("id") == package:
                return _icon_uri(app)
        return None

    async def async_get_app_icon(self, package: str | None) -> bytes | None:
        """Return the decrypted icon bytes for an app package, or None.

        Cached by content-addressed URI, so each icon is fetched from the TV at
        most once (until the app's icon changes). Fetched lazily when Home
        Assistant first renders the app's browse thumbnail.
        """
        if not package:
            return None
        uri = next(
            (_icon_uri(app) for app in self._app_list if app.get("id") == package), None
        )
        if not uri:
            return None
        cached = self._icon_cache.get(uri)
        if cached is not None:
            return cached
        try:
            data = await self.hass.async_add_executor_job(
                self.client.read_resource, uri
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("icon fetch failed for %s", package, exc_info=True)
            return None
        if data:
            self._icon_cache[uri] = data
        return data

    @property
    def app_list(self) -> list[dict]:
        """Installed apps: ``[{"id": package, "label": name, ...}, ...]``."""
        return self._app_list

    @property
    def device_name(self) -> str:
        """Device display name: the TV's own friendly name, else the entry title.

        Read at entity setup, so it tracks a rename on the next reload; a user's
        Home Assistant rename (name_by_user) still takes precedence."""
        name = self._state.get("system_setting.friendly_name")
        return name if isinstance(name, str) and name else self._entry.title

    def app_label(self, package: str | None) -> str | None:
        """Friendly label for an app package: the live app list first, then known
        built-in inputs (never in that list), else the raw package id."""
        if not package:
            return None
        for app in self._app_list:
            if app.get("id") == package and app.get("label"):
                return app["label"]
        return _SYSTEM_APP_LABELS.get(package, package)

    def _on_connection_lost(self) -> None:
        """Notify-thread entrypoint: the stream died (likely a TV reboot onto a
        new port). Reload the entry to re-discover the port and refresh keys."""
        self.hass.loop.call_soon_threadsafe(self._schedule_reload)

    @callback
    def _schedule_reload(self) -> None:
        _LOGGER.warning("Bravia TV push connection lost; reloading to reconnect")
        # Entities go unavailable immediately while the reload reconnects.
        self.async_set_update_error(UpdateFailed("Bravia TV push connection lost"))
        self.hass.config_entries.async_schedule_reload(self._entry.entry_id)

    def _on_reconnect(self) -> None:
        """Notify-thread entrypoint: the push stream re-subscribed after a drop.
        Reconcile anything that changed while we were disconnected."""
        self.hass.loop.call_soon_threadsafe(self._schedule_reconnect_resync)

    @callback
    def _schedule_reconnect_resync(self) -> None:
        self.hass.async_create_task(
            self._async_reconnect_resync(), name="bravia_tv_reconnect_resync"
        )

    async def _async_reconnect_resync(self) -> None:
        """Full re-read after a push reconnect: reconciles any deltas missed
        while disconnected (all state + the installed-app list). gRPC streams are
        reliable in-flight, so a drop is the only time a delta can be missed —
        which is exactly here."""
        if await self._seed_from_device():
            self.async_set_updated_data(dict(self._state))
        await self._async_refresh_app_list()

    async def _async_periodic_refresh(self, _now: Any) -> None:
        """Lightweight liveness probe (single-path read).

        Push keeps state current and a reconnect reconciles it, so this no longer
        re-reads the full state — it just confirms the connection is alive.
        Persistent failure means the connection is dead — typically a TV reboot
        that changed the gRPC port and invalidated the keys — so reload to
        re-discover the port and refresh keys.
        """
        if await self._async_liveness():
            self._refresh_failures = 0
            # Connection is back: restore entity availability if a prior failure
            # (or a lost push stream) had marked the coordinator unsuccessful.
            if not self.last_update_success:
                self.async_set_updated_data(dict(self._state))
            return
        self._refresh_failures += 1
        # Reflect the dead connection on entities now (unavailable) rather than
        # letting them show stale state until the reload below.
        self.async_set_update_error(UpdateFailed("Bravia TV liveness probe failed"))
        if self._refresh_failures >= 2:
            self._refresh_failures = 0
            _LOGGER.warning(
                "Bravia TV unreachable; reloading to re-discover port and keys"
            )
            self.hass.config_entries.async_schedule_reload(self._entry.entry_id)

    async def _async_liveness(self) -> bool:
        """Cheap single-path GetStates to confirm the connection is alive."""
        try:
            states = await self.hass.async_add_executor_job(
                self.client.get_states, ["power"]
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("liveness probe failed", exc_info=True)
            return False
        return bool(states)

    async def _seed_from_device(self) -> bool:
        """Seed all fields from a single bulk GetStates read. True on success."""
        try:
            states = await self.hass.async_add_executor_job(self.client.get_states)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("device GetStates seed failed", exc_info=True)
            return False
        if not states:
            return False
        self._state.update(states)
        _LOGGER.debug("seeded %d fields from device GetStates", len(states))
        return True

    async def _seed_from_cloud(self) -> None:
        """Best-effort initial snapshot from the Sony IoT cloud states API."""
        keys = self._entry.data.get(CONF_GRPC_KEYS)
        if not keys:
            return
        try:
            creds = cred.parse_credentials_json(keys)
            snapshot = await self.hass.async_add_executor_job(
                self._fetch_cloud_states, creds
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("cloud seed snapshot unavailable", exc_info=True)
            return
        caps = self.client.capabilities
        for item in snapshot.get("states", []):
            name = item.get("name")
            path = _CLOUD_TO_GRPC_PATH.get(name)
            if not path:
                continue
            value = item.get("value")
            if item.get("type") == "enum" and isinstance(value, str):
                camel = _snake_to_camel(value)
                meta = caps.get(path)
                # The cloud enum vocabulary is not always a clean snake_case of
                # the gRPC one (e.g. fps_game vs gameFps). Only trust the seed
                # when it lands in the advertised enum; otherwise leave the path
                # unknown and let the first push delta set the real value.
                if meta is not None and meta.values and camel not in meta.values:
                    continue
                value = camel
            self._state[path] = value

    def _fetch_cloud_states(self, creds: dict[str, Any]) -> dict[str, Any]:
        token = cred.refresh_access_token(creds["refresh_token"])["access_token"]
        return cred.get_device_states(creds["device_id"], token)

    @callback
    def _apply_delta(self, path: str, value: Any) -> None:
        if path == APPLICATION_LIST_PATH:
            # The installed-app list changed (an app was installed or removed).
            # The pushed value is the encrypted blob, so re-read it via the
            # nonce flow rather than storing it.
            self.hass.async_create_task(
                self._async_refresh_app_list(),
                name="bravia_tv_app_list_refresh",
            )
            return
        self._state[path] = value
        if (
            path == "fw_update.update_exist"
            and value
            and not self._entry.data.get(CONF_FW_PENDING)
        ):
            # An update became available; remember it so setup can re-fetch the
            # firmware version once it's applied (see __init__._ensure_device_info).
            self.hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, CONF_FW_PENDING: True}
            )
        self.async_set_updated_data(dict(self._state))

    def _on_delta_threadsafe(self, path: str, value: Any) -> None:
        """Notify-thread entrypoint: hop to the event loop before touching state."""
        self.hass.loop.call_soon_threadsafe(self._apply_delta, path, value)

    async def async_set_field(self, path: str, value: Any) -> None:
        """Send an ExecCommand and optimistically reflect the new value."""
        ok = await self.hass.async_add_executor_job(
            self.client.exec_command, path, value
        )
        if ok:
            self._apply_delta(path, value)
        else:
            _LOGGER.warning("ExecCommand for %s=%s was rejected", path, value)

    async def async_send_key(self, path: str, value: Any) -> bool:
        """Fire a momentary action (e.g. a remote key) with no state write.

        Unlike ``async_set_field`` these paths are not readable state, so nothing
        is stored; only the ExecCommand is sent. Returns whether it was accepted.
        """
        ok = await self.hass.async_add_executor_job(
            self.client.exec_command, path, value
        )
        if not ok:
            _LOGGER.warning("Remote command %s=%s was rejected", path, value)
        return ok

    async def async_shutdown(self) -> None:
        if self._cancel_refresh is not None:
            self._cancel_refresh()
            self._cancel_refresh = None
        await self.hass.async_add_executor_job(self.client.close)
        await super().async_shutdown()
