"""Media player platform for the Bravia TV (gRPC) integration.

Composes the push-backed control paths (``power``, ``volume``, ``mute``) plus
source selection into a single media_player. Features are added only for paths
the device advertises, so the entity degrades gracefully on models that lack
one. SELECT_SOURCE switches external inputs (by their native id) and launches
apps, both via the verified any-typed write. Playback transport, seek and the
``url`` / ``theme`` / ``channel`` play_media types, the ``search`` service and
the standby power-on fallback go through the Sony cloud command channel and
are offered only when the TV advertises those cloud capabilities.
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import voluptuous as vol

from .const import DOMAIN
from .coordinator import BraviaTvCoordinator, _icon_uri, agent_path, cloud_path
from .entity import BraviaTvEntity
from .grpc import cloud_commands as cloud
from .grpc.resources import image_content_type
from .util import deep_link_package, parse_json_value, source_label

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

# Playback transport has no local gRPC path (the remote_key enums carry no
# play/pause). It is a cloud-only capability: ``playback`` set to one of
# playing / paused / stopped / previous / next, delivered via the Sony cloud.
_PLAYBACK = "playback"
# Cloud-only absolute seek: ``set`` takes seconds; the TV reports the resulting
# position in milliseconds. Needs an app with an active media session.
_SEEK = "seek"
# Cloud-only tuner channel selection by number (``post {"number": ...}``);
# surfaced through play_media with media type ``channel``.
_CHANNEL = "channel"
# Cloud-only deep link into an app (``post {"package_name", "uri"}``); surfaced
# through play_media with media type ``url`` (package guessed from the host, or
# given as ``extra.package_name``). Verified with YouTube.
_DEEP_LINK = "application.deep_link"
# Cloud-only Living Decor theme selection (``post {"contentId", "packageName"}``);
# surfaced through play_media with media type ``theme`` and a ``theme://`` id.
_SELECT_MEDIA = "select_media"
_LIVING_DECOR = "com.sony.dtv.livingfit"
# Cloud-only on-screen search (Sony's voice-search results page); surfaced as
# the ``search`` entity service. Takes over the screen and audio focus.
_SEARCH = "search"
# Apps whose media sessions are audio-only, for the media_content_type hint.
_MUSIC_APPS = {
    "com.spotify.tv.android",
    "com.google.android.youtube.tvmusic",
    "com.aspiro.tidal",
    "com.apple.android.music",
    "com.amazon.music.tv",
    "deezer.android.tv",
    "com.sony.dtv.osat.music.toast",
}
_SEARCH_DISPLAY = "ACTION_SEARCH_AND_DISPLAY_RESULTS"
_SEARCH_PLAY = "ACTION_SEARCH_AND_PLAY"
_MEDIA_TYPE_THEME = "theme"


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
    if coordinator.cloud_supports(_SEARCH, "post"):
        entity_platform.async_get_current_platform().async_register_entity_service(
            "search",
            {
                vol.Required("query"): cv.string,
                vol.Optional("play", default=False): cv.boolean,
            },
            "async_search",
        )


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
        # async_get_browse_image); PLAY_MEDIA launches the chosen app, or tunes
        # a channel by number through the cloud when the TV advertises that.
        if _APP in caps:
            features |= (
                MediaPlayerEntityFeature.BROWSE_MEDIA
                | MediaPlayerEntityFeature.PLAY_MEDIA
            )
        if any(
            coordinator.cloud_supports(name, "post")
            for name in (_CHANNEL, _DEEP_LINK, _SELECT_MEDIA)
        ):
            features |= MediaPlayerEntityFeature.PLAY_MEDIA
        # Transport controls go through the Sony cloud command channel; only
        # offered when the TV advertised the playback capability there.
        if coordinator.cloud_supports(_PLAYBACK):
            features |= (
                MediaPlayerEntityFeature.PLAY
                | MediaPlayerEntityFeature.PAUSE
                | MediaPlayerEntityFeature.STOP
                | MediaPlayerEntityFeature.NEXT_TRACK
                | MediaPlayerEntityFeature.PREVIOUS_TRACK
            )
        if coordinator.cloud_supports(_SEEK):
            features |= MediaPlayerEntityFeature.SEEK
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
        if not power:
            return MediaPlayerState.OFF
        # With the agent, the foreground app's media session gives the real
        # transport state; without it the TV is simply "on".
        media = self._agent_media
        if media and media.get("active"):
            return {
                "playing": MediaPlayerState.PLAYING,
                "paused": MediaPlayerState.PAUSED,
                "buffering": MediaPlayerState.BUFFERING,
            }.get(str(media.get("state")), MediaPlayerState.ON)
        return MediaPlayerState.ON

    # -- now playing (BRAVIA TV agent) --------------------------------------
    @property
    def _agent_media(self) -> dict | None:
        """The agent's media block while the TV is on and the agent is online."""
        if self._state.get(_POWER) is False or not self.coordinator.agent_online:
            return None
        media = self._state.get(agent_path("media"))
        return media if isinstance(media, dict) else None

    @property
    def app_name(self) -> str | None:
        media = self._agent_media
        if media and media.get("active") and isinstance(media.get("app"), str):
            return self.coordinator.app_label(media["app"]) or media["app"]
        return None

    @property
    def media_title(self) -> str | None:
        media = self._agent_media
        return media.get("title") if media and media.get("active") else None

    @property
    def media_artist(self) -> str | None:
        media = self._agent_media
        return media.get("artist") if media and media.get("active") else None

    @property
    def media_album_name(self) -> str | None:
        media = self._agent_media
        return media.get("album") if media and media.get("active") else None

    @property
    def media_duration(self) -> int | None:
        media = self._agent_media
        value = media.get("duration_s") if media and media.get("active") else None
        return int(value) if isinstance(value, int | float) else None

    @property
    def media_content_type(self) -> MediaType | str | None:
        media = self._agent_media
        if not media or not media.get("active"):
            return None
        return MediaType.MUSIC if media.get("app") in _MUSIC_APPS else MediaType.VIDEO

    @property
    def media_position_updated_at(self):
        media = self._agent_media
        if media and media.get("active") and media.get("position_s") is not None:
            return self._state.get(agent_path("updated_at"))
        return None

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

        In networked standby the TV keeps the last foreground app in its state,
        so gate on power to avoid reporting a phantom source while it's off.
        """
        if self._state.get(_POWER) is False:
            return None
        label = source_label(self._state.get(_INPUT))
        if label:
            return label
        return self.coordinator.app_label(self._state.get(_APP))

    @property
    def _current_app_package(self) -> str | None:
        """Foreground Android app package, if one is running (and the TV is on)."""
        if self._state.get(_POWER) is False:
            return None
        pkg = self._state.get(_APP)
        return pkg if isinstance(pkg, str) and pkg else None

    @property
    def media_image_url(self) -> str | None:
        """Artwork for the card: the agent's now-playing art when a session is
        active, else the foreground app's icon URI.

        Returned only when something has an image (external inputs and the
        built-in tuner have none). HA fetches the actual bytes server-side via
        ``async_get_media_image``; this URL just supplies a stable hash that
        changes with the artwork, so the card refreshes.
        """
        media = self._agent_media
        agent = self.coordinator.agent
        if media and media.get("active") and media.get("artwork_url") and agent:
            # The agent's artwork_url carries a revision (?v=n) that changes
            # with every new image, so the URL itself is the cache key.
            return agent.url(str(media["artwork_url"]))
        return self.coordinator.app_icon_uri(self._current_app_package)

    @property
    def media_image_remotely_accessible(self) -> bool:
        # The iot-resource URI isn't an HTTP URL; HA must fetch via our hook.
        return False

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Serve the now-playing artwork (agent) or the foreground app's icon."""
        media = self._agent_media
        if media and media.get("active") and media.get("artwork_url"):
            art = await self.coordinator.async_get_agent_artwork()
            if art:
                return art, image_content_type(art)
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
        """Launch an app (media_id is the package id), or via the cloud:
        ``channel`` tunes a channel number, ``url`` opens a deep link in the
        matching app, ``theme`` shows a Living Decor theme (``theme://…``)."""
        if media_type == MediaType.CHANNEL:
            await self.coordinator.async_cloud_command(
                _CHANNEL, [cloud.post_command(_CHANNEL, {"number": str(media_id)})]
            )
            return
        if media_type == MediaType.URL:
            extra = kwargs.get("extra")
            package = (
                (extra or {}).get("package_name") if isinstance(extra, dict) else None
            )
            package = package or deep_link_package(media_id)
            if not package:
                raise HomeAssistantError(
                    f"No app known for {media_id}; pass extra.package_name"
                )
            await self.coordinator.async_cloud_command(
                _DEEP_LINK,
                [
                    cloud.post_command(
                        _DEEP_LINK, {"package_name": package, "uri": media_id}
                    )
                ],
            )
            return
        if media_type == _MEDIA_TYPE_THEME:
            await self.coordinator.async_cloud_command(
                _SELECT_MEDIA,
                [
                    cloud.post_command(
                        _SELECT_MEDIA,
                        {"contentId": media_id, "packageName": _LIVING_DECOR},
                    )
                ],
            )
            return
        await self.coordinator.async_set_field(_APP, media_id)

    async def async_search(self, query: str, play: bool = False) -> None:
        """Show Sony's on-screen search results for ``query`` (entity service)."""
        await self.coordinator.async_cloud_command(
            _SEARCH,
            [
                cloud.post_command(
                    _SEARCH,
                    {
                        "action": _SEARCH_PLAY if play else _SEARCH_DISPLAY,
                        "search_text": query,
                    },
                )
            ],
        )

    # -- commands ----------------------------------------------------------
    async def async_turn_on(self) -> None:
        await self.coordinator.async_turn_on_power()

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

    # -- transport (cloud) --------------------------------------------------
    async def _async_playback(self, value: str) -> None:
        await self.coordinator.async_cloud_command(
            _PLAYBACK, [cloud.set_command(_PLAYBACK, value)]
        )

    async def async_media_play(self) -> None:
        await self._async_playback("playing")

    async def async_media_pause(self) -> None:
        await self._async_playback("paused")

    async def async_media_stop(self) -> None:
        await self._async_playback("stopped")

    async def async_media_next_track(self) -> None:
        await self._async_playback("next")

    async def async_media_previous_track(self) -> None:
        await self._async_playback("previous")

    @property
    def media_position(self) -> int | None:
        """Position (s): the agent's live session position when available,
        else what the TV last reported after a seek; None otherwise."""
        media = self._agent_media
        if (
            media
            and media.get("active")
            and isinstance(media.get("position_s"), int | float)
        ):
            return int(media["position_s"])
        value = self._state.get(cloud_path(_SEEK))
        return int(value) // 1000 if isinstance(value, int | float) else None

    async def async_media_seek(self, position: float) -> None:
        await self.coordinator.async_cloud_command(
            _SEEK, [cloud.set_command(_SEEK, int(position))]
        )
