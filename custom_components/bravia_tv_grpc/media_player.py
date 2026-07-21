"""Media player platform for the Bravia TV (gRPC) integration.

Composes the push-backed control paths (``power``, ``volume``, ``mute``) plus
source selection into a single media_player. Features are added only for paths
the device advertises, so the entity degrades gracefully on models that lack
one. SELECT_SOURCE switches external inputs (by their native id) and launches
apps, both via the verified any-typed write.
"""

from __future__ import annotations

import logging

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BraviaTvCoordinator, _icon_uri
from .entity import BraviaTvEntity
from .grpc.resources import image_content_type
from .util import parse_json_value, source_label

_LOGGER = logging.getLogger(__name__)

_POWER = "power"
_VOLUME = "volume"
_MUTE = "mute"
# Readable/push current-input path; its value is any-typed JSON like
# {"type":"hdmi","label":"HDMI 3 (eARC/ARC)","sub_label":...} over gRPC (or a
# plain label string when seeded from the Sony IoT cloud snapshot). Reads
# {"type":"none"} when a foreground app, not an external input, is active.
_INPUT = "system_setting.tvapp.input"
# Foreground Android app package id, e.g. tv.twitch.android.app.
_APP = "system_setting.application"
# Write path to switch external input: takes the raw input `id` string.
_INPUT_SET = "system_setting.input"
# JSON list of {id, type, label, is_connected, ...} for available inputs.
_AVAILABLE_INPUTS = "system_setting.available_inputs"

# Fraction of full range moved per volume-step press.
_VOLUME_STEP = 0.02


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Bravia TV media player if the core paths are advertised."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    caps = coordinator.client.capabilities
    if _POWER in caps:
        async_add_entities([BraviaTvMediaPlayer(coordinator)])


class BraviaTvMediaPlayer(BraviaTvEntity, MediaPlayerEntity):
    """TV media player built from advertised gRPC capabilities."""

    _attr_name = None  # use the device name for the primary entity

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _POWER)
        caps = coordinator.client.capabilities

        features = MediaPlayerEntityFeature(0)
        if _POWER in caps:
            features |= (
                MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF
            )
        if _VOLUME in caps:
            features |= (
                MediaPlayerEntityFeature.VOLUME_SET
                | MediaPlayerEntityFeature.VOLUME_STEP
            )
        if _MUTE in caps:
            features |= MediaPlayerEntityFeature.VOLUME_MUTE
        # SELECT_SOURCE covers both launching apps (system_setting.application)
        # and switching external inputs (system_setting.input, value = the raw
        # input id from available_inputs) — both verified any-typed writes.
        if _APP in caps or _INPUT_SET in caps:
            features |= MediaPlayerEntityFeature.SELECT_SOURCE
        # BROWSE_MEDIA exposes the installed apps as a thumbnail grid (icons via
        # async_get_browse_image); PLAY_MEDIA launches the chosen app.
        if _APP in caps:
            features |= (
                MediaPlayerEntityFeature.BROWSE_MEDIA
                | MediaPlayerEntityFeature.PLAY_MEDIA
            )
        self._attr_supported_features = features

        vol_meta = caps.get(_VOLUME)
        self._vol_max = float(vol_meta.max) if vol_meta and vol_meta.max else 100.0
        self._vol_min = float(vol_meta.min) if vol_meta and vol_meta.min else 0.0

    # -- state -------------------------------------------------------------
    @property
    def _state(self) -> dict:
        return self.coordinator.data or {}

    @property
    def state(self) -> MediaPlayerState | None:
        power = self._state.get(_POWER)
        if power is None:
            return None
        return MediaPlayerState.ON if power else MediaPlayerState.OFF

    @property
    def volume_level(self) -> float | None:
        raw = self._state.get(_VOLUME)
        if raw is None:
            return None
        span = self._vol_max - self._vol_min or 1.0
        return max(0.0, min(1.0, (float(raw) - self._vol_min) / span))

    @property
    def is_volume_muted(self) -> bool | None:
        muted = self._state.get(_MUTE)
        return None if muted is None else bool(muted)

    @property
    def source(self) -> str | None:
        """Current source: the external input label if one is active, else the
        foreground app.

        ``tvapp.input`` reads ``{"type":"none"}`` when an Android app (not an
        HDMI/external input) is in front, so fall back to the app name then.
        """
        label = source_label(self._state.get(_INPUT))
        if label:
            return label
        return self.coordinator.app_label(self._state.get(_APP))

    @property
    def _current_app_package(self) -> str | None:
        """Foreground Android app package, if one is running."""
        pkg = self._state.get(_APP)
        return pkg if isinstance(pkg, str) and pkg else None

    @property
    def media_image_url(self) -> str | None:
        """The foreground app's icon URI, driving the card artwork.

        Returned only when the foreground app has an icon (external inputs and
        the built-in tuner have none). HA fetches the actual bytes server-side
        via ``async_get_media_image``; this URI just supplies a stable hash that
        changes when the foreground app does, so the artwork refreshes.
        """
        return self.coordinator.app_icon_uri(self._current_app_package)

    @property
    def media_image_remotely_accessible(self) -> bool:
        # The iot-resource URI isn't an HTTP URL; HA must fetch via our hook.
        return False

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Serve the foreground app's icon bytes as the current-media artwork."""
        data = await self.coordinator.async_get_app_icon(self._current_app_package)
        if not data:
            return None, None
        return data, image_content_type(data)

    def _available_inputs(self) -> list[dict]:
        """Parse the available_inputs JSON list from current state."""
        data = parse_json_value(self._state.get(_AVAILABLE_INPUTS))
        if not isinstance(data, list):
            return []
        return [i for i in data if isinstance(i, dict) and i.get("label")]

    @property
    def source_list(self) -> list[str]:
        """External input labels (currently available) followed by the TV's live
        installed-app list (empty of apps until that list has first loaded)."""
        inputs = [i["label"] for i in self._available_inputs()]
        apps = [a["label"] for a in self.coordinator.app_list if a.get("label")]
        return inputs + sorted(dict.fromkeys(apps))

    async def async_select_source(self, source: str) -> None:
        """Switch to an external input, or launch an app, by display name."""
        # An available external input? Switch to it by its native id.
        for inp in self._available_inputs():
            if inp.get("label") == source:
                await self.coordinator.async_set_field(_INPUT_SET, inp["id"])
                return
        # An installed app (from the live list)?
        for app in self.coordinator.app_list:
            if app.get("label") == source:
                await self.coordinator.async_set_field(_APP, app["id"])
                return
        _LOGGER.warning(
            "Unknown source (not an available input or known app): %s", source
        )

    # -- app browser -------------------------------------------------------
    async def async_browse_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Present the installed apps as a browsable grid of icon tiles."""
        children = [
            BrowseMedia(
                title=app["label"],
                media_class=MediaClass.APP,
                media_content_type=MediaType.APP,
                media_content_id=app["id"],
                can_play=True,
                can_expand=False,
                thumbnail=(
                    self.get_browse_image_url(MediaType.APP, app["id"])
                    if _icon_uri(app)
                    else None
                ),
            )
            for app in self.coordinator.app_list
            if app.get("id") and app.get("label")
        ]
        children.sort(key=lambda item: item.title.casefold())
        return BrowseMedia(
            title="Apps",
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.APPS,
            media_content_id="apps",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.APP,
        )

    async def async_get_browse_image(
        self,
        media_content_type: str,
        media_content_id: str,
        media_image_id: str | None = None,
    ) -> tuple[bytes | None, str | None]:
        """Serve an app's icon bytes for its browse thumbnail (HA proxies this)."""
        data = await self.coordinator.async_get_app_icon(media_content_id)
        if not data:
            return None, None
        return data, image_content_type(data)

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: object
    ) -> None:
        """Launch an app chosen from the browser (media_id is the package id)."""
        await self.coordinator.async_set_field(_APP, media_id)

    # -- commands ----------------------------------------------------------
    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_field(_POWER, True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_field(_POWER, False)

    async def async_mute_volume(self, mute: bool) -> None:
        await self.coordinator.async_set_field(_MUTE, mute)

    async def async_set_volume_level(self, volume: float) -> None:
        target = round(self._vol_min + volume * (self._vol_max - self._vol_min))
        await self.coordinator.async_set_field(_VOLUME, int(target))

    async def async_volume_up(self) -> None:
        await self._nudge_volume(_VOLUME_STEP)

    async def async_volume_down(self) -> None:
        await self._nudge_volume(-_VOLUME_STEP)

    async def _nudge_volume(self, delta: float) -> None:
        current = self.volume_level
        if current is None:
            return
        await self.async_set_volume_level(max(0.0, min(1.0, current + delta)))
