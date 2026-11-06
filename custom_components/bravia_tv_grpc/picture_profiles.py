"""Backup and restore picture-setting profiles, for manual calibration.

A profile is a snapshot of every writable picture target's value for a picture
mode, captured over the agent's REST channel and restored by writing them all
back. This makes a manual calibration (basic settings through to the 2-point and
20-point adjustments) something you can save, restore and compare in a few
clicks, instead of nudging dozens of settings one at a time.

Profiles live as one YAML file per profile under
``<config>/bravia_picture_profiles/<tv>/`` -- the folder IS the store, so a
profile is backed up, hand-edited, version-controlled or shared just by copying
the file with a file-access add-on (File editor, Samba, SSH). The filename is
the profile name. Files are untrusted (a user may hand-edit one or drop in a
shared file), so reads are size-capped, alias-free and schema-validated, and a
bad file is skipped rather than parsed into the store.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.core import HomeAssistant
import yaml

_LOGGER = logging.getLogger(__name__)

# Guards for reading profile files: a profile is small, so anything larger or
# oddly-shaped is skipped rather than parsed/stored.
MAX_FILE_BYTES = 128 * 1024
MAX_SETTINGS = 200
MAX_KEY_LEN = 64
MAX_VALUE_LEN = 512
MAX_NAME_LEN = 128
# The picture mode is captured as metadata and restored first (settings are
# per-mode), not written as one of the profile's settings.
MODE_TARGET = "pictureMode"
# Written by the TV's own ambient-light adjustment; a restore is meaningless
# while it is on, so a restore flags it.
AUTO_LUMINANCE_TARGET = "autoLuminanceLevel"

# Picture settings the TV reports over REST but that are really owned by the
# local gRPC control plane (pushed live and authoritative). The REST mirror
# lags behind changes made with the remote, so a profile captures and restores
# these from gRPC instead of the REST batch (handled in the coordinator). The
# gRPC field paths:
GRPC_BRIGHTNESS_PATH = "display_setting.brightness"
GRPC_MODE_PATH = "display_setting.picture_mode"
# Their REST target names, skipped by ``capture`` so the stale REST value is
# never stored. ``pictureMode`` has its own branch below; ``brightness`` would
# otherwise be captured as an ordinary setting.
GRPC_OWNED_REST_TARGETS = frozenset({"brightness"})

# Picture-menu targets that are NOT per-picture-mode: they live in the picture
# REST API but belong to the whole TV, the panel, or the current input/signal,
# so a per-mode profile must not capture or restore them (it would silently
# change a setting that isn't part of the mode's calibration).
# Global / panel / light sensor:
# - sensorAutoAdjustment: Ambient Optimization (light-sensor adaptation).
# - autoLuminanceLevel: native auto-brightness-by-ambient-light toggle.
# - pixelShift: OLED pixel-shift panel protection.
# Input / app / signal specific:
# - contentType: auto-detected content type.
# - colorSpace, hdrMode: tied to the current input/signal, not the mode.
PROFILE_EXCLUDED = frozenset(
    {
        "sensorAutoAdjustment",
        "autoLuminanceLevel",
        "pixelShift",
        "contentType",
        "colorSpace",
        "hdrMode",
    }
)


def capture(targets: dict[str, Any] | None) -> tuple[str | None, dict[str, str]]:
    """From the agent's picture targets, return (picture_mode, {target: value})
    for every currently available, writable target except the mode itself.

    The gRPC-owned targets (brightness; and the mode, handled as metadata) are
    left out of ``settings`` here: their REST value lags, so the coordinator
    fills them from the authoritative gRPC state instead."""
    mode: str | None = None
    settings: dict[str, str] = {}
    for target, info in (targets or {}).items():
        if not isinstance(info, dict) or not info.get("available"):
            continue
        value = info.get("value")
        if value is None:
            continue
        if target == MODE_TARGET:
            mode = str(value)
            continue
        if target in GRPC_OWNED_REST_TARGETS or target in PROFILE_EXCLUDED:
            continue
        # Writable targets carry candidates (a range or a value list); read-only
        # status targets (e.g. the detected HDMI signal format) do not.
        if not info.get("candidates"):
            continue
        settings[target] = str(value)
    return mode, settings


def diff(a: dict[str, str], b: dict[str, str]) -> dict[str, dict[str, str | None]]:
    """Targets whose value differs between two settings dicts: {target: {a, b}}."""
    out: dict[str, dict[str, str | None]] = {}
    for target in sorted(set(a) | set(b)):
        if a.get(target) != b.get(target):
            out[target] = {"a": a.get(target), "b": b.get(target)}
    return out


class _SafeNoAliasLoader(yaml.SafeLoader):
    """SafeLoader that also forbids YAML aliases, so a crafted file cannot use
    anchor/alias expansion ("billion laughs") to exhaust memory on read."""

    def compose_node(self, parent, index):  # type: ignore[override]
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.constructor.ConstructorError(
                None, None, "YAML aliases are not allowed in picture profiles"
            )
        return super().compose_node(parent, index)


def is_valid_profile_name(name: Any) -> bool:
    """Whether ``name`` works as a profile name (and so a filename): non-empty,
    bounded, no path separators, dotted specials or control characters."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= MAX_NAME_LEN
        and "/" not in name
        and "\\" not in name
        and name not in (".", "..")
        and all(ord(c) >= 32 for c in name)
    )


def _validate(data: Any) -> dict[str, Any] | None:
    """Coerce a parsed file into a profile dict, or None if it is not one."""
    if not isinstance(data, dict):
        return None
    settings = data.get("settings")
    if not isinstance(settings, dict) or not settings or len(settings) > MAX_SETTINGS:
        return None
    clean: dict[str, str] = {}
    for key, value in settings.items():
        skey, sval = str(key), str(value)
        if len(skey) > MAX_KEY_LEN or len(sval) > MAX_VALUE_LEN:
            return None
        clean[skey] = sval
    out: dict[str, Any] = {"settings": clean}
    for field in ("created", "picture_mode", "hdr_class", "model", "model_id"):
        val = data.get(field)
        out[field] = None if val is None else str(val)
    return out


class PictureProfileStore:
    """Picture profiles as one YAML file per profile in a folder. The folder is
    the source of truth; reads re-scan it, so files added or edited outside Home
    Assistant are picked up. All disk I/O runs in the executor."""

    def __init__(self, hass: HomeAssistant, directory: str) -> None:
        self._hass = hass
        self._dir = directory

    @property
    def directory(self) -> str:
        return self._dir

    async def async_ensure_dir(self) -> None:
        """Create the profile folder (called when the TV is linked)."""
        await self._hass.async_add_executor_job(self._ensure_dir)

    def _ensure_dir(self) -> None:
        os.makedirs(self._dir, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self._dir, f"{name}.yaml")

    # ---- reads ----
    def _read_file(self, path: str) -> dict[str, Any] | None:
        try:
            size = os.path.getsize(path)
        except OSError:
            return None  # missing / unstattable -> absent, no warning
        if size > MAX_FILE_BYTES:
            _LOGGER.warning("Picture profile %s is too large; skipping", path)
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                data = yaml.load(handle, Loader=_SafeNoAliasLoader)
        except (OSError, yaml.YAMLError) as err:
            _LOGGER.warning("Skipping unreadable picture profile %s: %s", path, err)
            return None
        profile = _validate(data)
        if profile is None:
            _LOGGER.warning("Skipping malformed picture profile %s", path)
        return profile

    def _scan(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        try:
            entries = sorted(os.listdir(self._dir))
        except FileNotFoundError:
            return result
        for filename in entries:
            if not filename.endswith(".yaml"):
                continue
            name = filename[:-5]
            if not is_valid_profile_name(name):
                continue
            profile = self._read_file(os.path.join(self._dir, filename))
            if profile is not None:
                result[name] = profile
        return result

    def _signature(self) -> tuple:
        """Cheap fingerprint of the folder (name, size, mtime per file) to
        detect external changes without re-parsing every file."""
        try:
            with os.scandir(self._dir) as entries:
                return tuple(
                    sorted(
                        (e.name, st.st_size, st.st_mtime_ns)
                        for e in entries
                        if e.name.endswith(".yaml") and e.is_file()
                        for st in (e.stat(),)
                    )
                )
        except FileNotFoundError:
            return ()

    async def async_signature(self) -> tuple:
        return await self._hass.async_add_executor_job(self._signature)

    async def all(self) -> dict[str, dict[str, Any]]:
        return await self._hass.async_add_executor_job(self._scan)

    async def names(self) -> list[str]:
        return sorted(await self.all())

    async def get(self, name: str) -> dict[str, Any] | None:
        if not is_valid_profile_name(name):
            return None
        return await self._hass.async_add_executor_job(
            self._read_file, self._path(name)
        )

    # ---- writes (atomic) ----
    def _write(self, name: str, profile: dict[str, Any]) -> None:
        os.makedirs(self._dir, exist_ok=True)
        path = self._path(name)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                profile,
                handle,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=True,
            )
        os.replace(tmp, path)

    async def put(self, name: str, profile: dict[str, Any]) -> None:
        await self._hass.async_add_executor_job(self._write, name, profile)

    def _delete(self, name: str) -> bool:
        try:
            os.remove(self._path(name))
            return True
        except FileNotFoundError:
            return False

    async def delete(self, name: str) -> bool:
        if not is_valid_profile_name(name):
            return False
        return await self._hass.async_add_executor_job(self._delete, name)
