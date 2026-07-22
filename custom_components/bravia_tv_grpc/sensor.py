"""Sensor platform for the Bravia TV (gRPC) integration.

Diagnostic read-only sensors derived from any-typed gRPC fields: the current
video signal (resolution / HDR format / frame rate), the current audio track
(codec / channels), and the foreground app.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BraviaTvCoordinator
from .entity import BraviaTvEntity
from .util import parse_json_value, source_label

_SIGNAL_VIDEO = "playback_control.signal_info.video"
_SIGNAL_AUDIO = "playback_control.signal_info.audio"
_POWER = "power"
# BRAVIA Connect "combine": the operation-speaker (opspk) association. Reads
# "none" when the TV and soundbar are not combined, otherwise the association
# value. Surfaced diagnostically so the raw value is recorded in history -- it
# is both the definitive combine indicator and the value needed to reproduce a
# combine over gRPC. generic_command_in_use flags an in-progress opspk command.
_OPSPK_SETUP = "opspk.setup"
_OPSPK_IN_USE = "opspk.generic_command_in_use"
_APP = "system_setting.application"
_APP_LIST = "system_setting.application_list"
# Current external input (any-typed JSON); reads {"type":"none"} when an app,
# not an HDMI/external input, is in front.
_INPUT = "system_setting.tvapp.input"
# Connected Bluetooth audio device info (any-typed JSON, e.g. {"name":"..."};
# empty {} when nothing is paired).
_BT_DEVICE = "sound_setting.volume.bluetooth.device_info"

# Direct Connect wireless speakers: select BRAVIA Theatre rear speakers and
# subwoofers paired straight to the TV (no soundbar) -- rear-left, rear-right and
# subwoofer positions. connection_status is the primary value; the rest of the
# speaker_connection_setting.<field>.<pos> paths describe the paired unit and are
# surfaced as attributes (populated once a speaker is paired).
_SPEAKERS: dict[str, str] = {
    "rl": "rear_left_speaker",
    "rr": "rear_right_speaker",
    "sw": "subwoofer",
}
_SPK_STATUS = "speaker_connection_setting.connection_status.{pos}"

# Companion suffix a field carries when it can be conditionally restricted; its
# value is the reason (or "none" when fully available).
_UNAVAIL_SUFFIX = ".unavailable_reason"
_SPK_STATUS_LABELS = {
    "disconnected": "Disconnected",
    "connected": "Connected",
    "protected": "Protected",
}
# attribute name -> speaker_connection_setting field (suffixed with .<pos>)
_SPK_ATTRS = {
    "connected_before": "connection_history",
    "previous_model": "connection_history.modelname",
    "model": "modelname",
    "model_id": "identified_modelname",
    "firmware_version": "version",
    "serial_number": "serial_number",
    "mac_address": "wifi_mac_address",
}

# Friendly labels for the TV's hdr_type / codec strings (observed values +
# common Sony variants); unknown values fall back to a cleaned raw string.
_HDR_NAMES = {
    "sdr": "SDR",
    "hdr10": "HDR10",
    "hdr10plus": "HDR10+",
    "hlg": "HLG",
    # This TV reports Dolby Vision as the bare string "dolby" (verified live on
    # the K-65XR8M2 with Prime Video); keep the fuller variants for other models.
    "dolby": "Dolby Vision",
    "dolbyvision": "Dolby Vision",
    "dolby_vision": "Dolby Vision",
}
_CODEC_NAMES = {
    "dts_uhd_p2": "DTS:X",
    "dts_uhd": "DTS:X",
    "dts_hd": "DTS-HD",
    "dts": "DTS",
    "dolby_atmos": "Dolby Atmos",
    # Atmos carried over Dolby Digital+ (E-AC-3 JOC), as reported by Netflix.
    "dolby_atmos_digital_plus": "Dolby Atmos",
    "dolby_atmos_truehd": "Dolby Atmos",
    "dolby_truehd": "Dolby TrueHD",
    "dolby_digital_plus": "Dolby Digital+",
    "eac3": "Dolby Digital+",
    "dolby_digital": "Dolby Digital",
    "ac3": "Dolby Digital",
    "pcm": "PCM",
    "lpcm": "PCM",
    "aac": "AAC",
    "mpeg": "MPEG Audio",
}


def _first_entry(raw: object) -> dict | None:
    """Return the first dict of a JSON-list any-value, or None."""
    data = parse_json_value(raw)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def _label(value: object, table: dict[str, str]) -> str | None:
    """Friendly label for a device enum string; cleaned raw as fallback."""
    if not isinstance(value, str) or not value:
        return None
    return table.get(value.lower(), value.replace("_", " "))


def _frame_rate_label(value: object) -> str | None:
    """Format a frame rate as e.g. "50fps" (24.0 -> "24fps", 23.976 kept)."""
    if value is None:
        return None
    try:
        return f"{float(value):g}fps"
    except (TypeError, ValueError):
        return None


# Named channel layouts; multi-channel layouts (e.g. "5.1", "5.1.2") fall back
# to "<layout> Surround", anything else to the raw string.
_CHANNEL_NAMES = {
    "1.0": "Mono",
    "2.0": "Stereo",
}


def _channels_label(value: object) -> str | None:
    """Friendly channel layout, e.g. "5.1" -> "5.1 Surround", "2.0" -> "Stereo"."""
    if value is None:
        return None
    text = str(value)
    if text in _CHANNEL_NAMES:
        return _CHANNEL_NAMES[text]
    return f"{text} Surround" if text[:1].isdigit() and "." in text else text


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Bravia TV sensors from advertised capabilities."""
    coordinator: BraviaTvCoordinator = hass.data[DOMAIN][entry.entry_id]
    caps = coordinator.client.capabilities
    entities: list[SensorEntity] = []
    if _SIGNAL_VIDEO in caps:
        entities.append(BraviaTvVideoSignalSensor(coordinator))
    if _SIGNAL_AUDIO in caps:
        entities.append(BraviaTvAudioSignalSensor(coordinator))
    if _APP in caps:
        entities.append(BraviaTvAppSensor(coordinator))
    if _APP_LIST in caps:
        entities.append(BraviaTvAppListSensor(coordinator))
    entities.extend(
        BraviaTvSpeakerSensor(coordinator, pos, key)
        for pos, key in _SPEAKERS.items()
        if _SPK_STATUS.format(pos=pos) in caps
    )
    if any(p.endswith(_UNAVAIL_SUFFIX) for p in caps):
        entities.append(BraviaTvRestrictedControlsSensor(coordinator))
    if _BT_DEVICE in caps:
        entities.append(BraviaTvBluetoothDeviceSensor(coordinator))
    if _OPSPK_SETUP in caps:
        entities.append(BraviaTvOperationSpeakerSensor(coordinator))
    async_add_entities(entities)


class BraviaTvVideoSignalSensor(BraviaTvEntity, SensorEntity):
    """Current video signal, e.g. "4K 24fps Dolby Vision" (quality + fps + format).

    Composes quality/frame rate/format into the state; the raw components
    (incl. resolution) stay available as attributes for templating.
    """

    _attr_translation_key = "video_signal"
    _attr_icon = "mdi:high-definition"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _SIGNAL_VIDEO)

    @property
    def native_value(self) -> str | None:
        info = _first_entry(self.state_value(_SIGNAL_VIDEO))
        if not info:
            return None
        quality = (
            str(info["quality"]).upper() if info.get("quality") is not None else None
        )
        frame_rate = _frame_rate_label(info.get("frame_rate"))
        fmt = (
            _label(info["hdr_type"], _HDR_NAMES)
            if info.get("hdr_type") is not None
            else None
        )
        # "Quality FPS Format", e.g. "4K 24fps Dolby Vision"; drop missing parts.
        parts = [p for p in (quality, frame_rate, fmt) if p]
        if parts:
            return " ".join(parts)
        # Nothing descriptive available — fall back to the raw resolution.
        width, height = info.get("width"), info.get("height")
        return f"{width}x{height}" if width and height else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = _first_entry(self.state_value(_SIGNAL_VIDEO)) or {}
        attrs: dict[str, Any] = {}
        if info.get("hdr_type") is not None:
            attrs["format"] = _label(info["hdr_type"], _HDR_NAMES)
        if info.get("frame_rate") is not None:
            attrs["frame_rate"] = info["frame_rate"]
        if info.get("quality") is not None:
            attrs["quality"] = str(info["quality"]).upper()
        if info.get("width") and info.get("height"):
            attrs["resolution"] = f"{info['width']}x{info['height']}"
        return attrs


class BraviaTvAudioSignalSensor(BraviaTvEntity, SensorEntity):
    """Current audio track, e.g. "5.1 Surround Dolby Atmos" (channels + codec).

    Bitstream sources (e.g. Netflix) report a codec + channels; app-decoded
    sources report only a package name, which we surface as "PCM".
    """

    _attr_translation_key = "audio_signal"
    _attr_icon = "mdi:surround-sound"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _SIGNAL_AUDIO)

    @property
    def native_value(self) -> str | None:
        info = _first_entry(self.state_value(_SIGNAL_AUDIO))
        if not info:
            # Routing audio to an external system over eARC (e.g. the soundbar)
            # leaves signal_info.audio empty even while an app is decoding audio
            # to PCM — whereas TV-speaker output reports a package-only entry.
            # When media is actively playing (a video signal is present), report
            # PCM to match BRAVIA Connect. Bitstream sources (Dolby etc.) still
            # carry their codec here regardless of output, so this branch only
            # ever covers app-decoded PCM, never a mislabelled Atmos/DD+ track.
            if _first_entry(self.state_value(_SIGNAL_VIDEO)):
                return "PCM"
            return None
        codec = info.get("codec")
        # Apps that decode audio internally (YouTube, Twitch, browsers, music)
        # report an entry with only the package name and no codec — the audio
        # reaching the TV is linear PCM. The gRPC API exposes neither channel
        # count nor sample rate here (unlike the BRAVIA Connect app), so "PCM"
        # is the most we can report.
        codec_label = _label(codec, _CODEC_NAMES) if codec else "PCM"
        channels = _channels_label(info.get("channels"))
        # "Channels Codec", e.g. "5.1 Surround Dolby Atmos"; drop channels when
        # absent.
        return f"{channels} {codec_label}" if channels else codec_label

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = _first_entry(self.state_value(_SIGNAL_AUDIO)) or {}
        attrs: dict[str, Any] = {}
        if info.get("channels") is not None:
            attrs["channels"] = _channels_label(info["channels"])
        if info.get("codec") is not None:
            attrs["codec"] = _label(info["codec"], _CODEC_NAMES)
        return attrs


class BraviaTvAppSensor(BraviaTvEntity, SensorEntity):
    """Current source: the active external input label, else the foreground app.

    ``tvapp.input`` reads ``{"type":"none"}`` while an Android app is in front,
    so the input label wins only when an HDMI/external input is actually active
    (matching the media_player's source). Mirrors media_player.source.
    """

    _attr_translation_key = "app"
    _attr_icon = "mdi:apps"

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _APP)

    @property
    def native_value(self) -> str | None:
        # In networked standby the TV keeps pushing state but never clears the
        # foreground-app field, so it would otherwise report a stale app. When
        # power is off there is no active app/source.
        if self.state_value(_POWER) is False:
            return None
        label = source_label(self.state_value(_INPUT))
        if label:
            return label
        return self.coordinator.app_label(self.state_value(_APP))


class BraviaTvAppListSensor(BraviaTvEntity, SensorEntity):
    """Installed apps: count as state, the full list (id + label) as attribute."""

    _attr_translation_key = "app_list"
    _attr_icon = "mdi:apps-box"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _APP_LIST)

    @property
    def native_value(self) -> int | None:
        apps = self.coordinator.app_list
        return len(apps) if apps else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "apps": [
                {"id": a.get("id"), "label": a.get("label")}
                for a in self.coordinator.app_list
            ]
        }


def _speaker_attr_value(raw: object) -> Any:
    """Normalise an any-typed speaker_connection_setting field for an attribute.

    The fields arrive as any-typed JSON strings (model/serial/mac/version) or, in
    the case of connection_history, a bare bool. Decode JSON when present; keep a
    non-JSON string as-is; drop empties.
    """
    if raw is None or not isinstance(raw, str):
        return raw  # bool (connection_history) or already-native value
    parsed = parse_json_value(raw)
    value = raw if parsed is None else parsed
    if isinstance(value, str) and not value.strip():
        return None
    return value


class BraviaTvSpeakerSensor(BraviaTvEntity, SensorEntity):
    """A Direct Connect wireless speaker/subwoofer.

    Direct Connect pairs select BRAVIA Theatre rear speakers and subwoofers
    straight to the TV without a soundbar. State is the connection status; the
    paired unit's details (model, firmware, serial, MAC, connection history) are
    surfaced as attributes and populate once a speaker is paired to that position.
    """

    _attr_icon = "mdi:speaker-wireless"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: BraviaTvCoordinator, pos: str, translation_key: str
    ) -> None:
        super().__init__(coordinator, _SPK_STATUS.format(pos=pos))
        self._pos = pos
        self._attr_translation_key = translation_key

    @property
    def native_value(self) -> str | None:
        raw = self._value
        if not isinstance(raw, str) or not raw:
            return None
        return _SPK_STATUS_LABELS.get(raw, raw.replace("_", " ").title())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        for name, field in _SPK_ATTRS.items():
            value = _speaker_attr_value(
                self.state_value(f"speaker_connection_setting.{field}.{self._pos}")
            )
            if value is not None:
                attrs[name] = value
        return attrs


class BraviaTvRestrictedControlsSensor(BraviaTvEntity, SensorEntity):
    """Which controls are currently restricted, and why.

    Home Assistant hides an unavailable entity's attributes, so a gated control's
    ``<path>.unavailable_reason`` can't usefully live on that entity -- it would
    vanish exactly when the control is restricted. This always-on diagnostic
    sensor aggregates them instead: the state is the number of restricted
    controls and the ``restrictions`` attribute maps each control to its reason.
    """

    _attr_translation_key = "restricted_controls"
    _attr_icon = "mdi:cancel"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        # Synthetic path (no gRPC field): the sensor derives its value from every
        # *.unavailable_reason, and has no availability companions of its own.
        super().__init__(coordinator, "restricted_controls")

    def _restrictions(self) -> dict[str, str]:
        data = self.coordinator.data or {}
        restricted = {
            key[: -len(_UNAVAIL_SUFFIX)]: reason.replace("_", " ").capitalize()
            for key, reason in data.items()
            if key.endswith(_UNAVAIL_SUFFIX)
            and isinstance(reason, str)
            and reason not in ("", "none")
        }
        return dict(sorted(restricted.items()))

    @property
    def native_value(self) -> int:
        return len(self._restrictions())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"restrictions": self._restrictions()}


class BraviaTvOperationSpeakerSensor(BraviaTvEntity, SensorEntity):
    """BRAVIA Connect "combine" state: the operation-speaker (opspk) setup value.

    ``opspk.setup`` reads ``"none"`` when the TV and soundbar are not combined,
    otherwise the (any-typed) association value the app writes to combine them.
    Exposed raw and diagnostic so history records the exact value at the moment
    of a (random) recombine -- the value needed to reproduce a combine on demand.
    """

    _attr_translation_key = "operation_speaker"
    _attr_icon = "mdi:speaker-multiple"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _OPSPK_SETUP)

    @property
    def native_value(self) -> str | None:
        raw = self._value
        if raw is None:
            return None
        text = raw if isinstance(raw, str) else str(raw)
        # HA caps a state at 255 chars; keep the full value in an attribute.
        return text[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        raw = self._value
        if isinstance(raw, str) and len(raw) > 255:
            attrs["raw"] = raw
        in_use = self.state_value(_OPSPK_IN_USE)
        if in_use is not None:
            attrs["command_in_use"] = bool(in_use)
        return attrs


class BraviaTvBluetoothDeviceSensor(BraviaTvEntity, SensorEntity):
    """Name of the connected Bluetooth audio device (if any).

    Reads ``sound_setting.volume.bluetooth.device_info`` (any-typed JSON); state
    is the device name, unknown when nothing is paired. Any further device_info
    fields are surfaced as attributes.
    """

    _attr_translation_key = "bluetooth_device"
    _attr_icon = "mdi:bluetooth-audio"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BraviaTvCoordinator) -> None:
        super().__init__(coordinator, _BT_DEVICE)

    def _info(self) -> dict[str, Any]:
        data = parse_json_value(self._value)
        return data if isinstance(data, dict) else {}

    @property
    def native_value(self) -> str | None:
        name = self._info().get("name")
        return name if isinstance(name, str) and name else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {k: v for k, v in self._info().items() if k != "name"}
