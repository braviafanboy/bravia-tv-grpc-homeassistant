"""Picture-quality targets relayed by the HA TV Agent (Home Assistant free).

The agent serves every target the TV's REST IP control reports as
``{name: {value, available, candidates}}``. This module decides which entity
kind a target becomes and how it is named.
"""

from __future__ import annotations

import re
from typing import Any

KIND_NUMBER = "number"
KIND_SWITCH = "switch"
KIND_SELECT = "select"

# Owned by gRPC (pushed, already entities); never duplicated from REST.
EXCLUDED = frozenset({"brightness", "pictureMode"})

# Targets that only offer choices on an HDMI source (empty on apps), seen on a
# BRAVIA 8 II: their kind and choices when the TV does not report them.
_FALLBACK: dict[str, tuple[str, Any]] = {
    "wideMode": (
        KIND_SELECT,
        ["auto", "full", "horizontal_fit", "vertical_fit", "original"],
    ),
    "displayArea": (KIND_NUMBER, {"min": -5, "max": 5, "step": 1}),
    "hdmiVideoRange": (KIND_SELECT, ["auto", "full", "limited"]),
    "hdmiSignalFormatVrr": (KIND_SWITCH, ["off", "on"]),
    "hdmiSignalFormat": (KIND_SELECT, ["standard", "enhanced1", "enhanced2"]),
}

_LABELS = {
    "advContrastEnhancer": "Advanced contrast enhancer",
    "autoLocalDimming": "Local dimming",
    "autoLuminanceLevel": "Auto luminance level",
    "autoToneCurve": "Auto tone curve",
    "autoWhiteBalance": "Auto white balance",
    "cineMotion": "CineMotion",
    "colorSpace": "Color space",
    "colorTemperature": "Color temperature",
    "contentType": "Content type",
    "digitalNoiseReduction": "Digital noise reduction",
    "hdmiSignalFormat": "HDMI signal format",
    "hdmiSignalFormatVrr": "HDMI signal format/VRR",
    "hdmiVideoRange": "HDMI video range",
    "hdrMode": "HDR mode",
    "hdrToneMapping": "HDR tone mapping",
    "liveColor": "Live Color",
    "localDimmingDriveMode": "Local dimming drive mode",
    "motionflowClearness": "Motionflow clearness",
    "motionflowMode": "Motionflow",
    "motionflowSmoothnessCamera": "Motionflow smoothness (camera)",
    "motionflowSmoothnessFilm": "Motionflow smoothness (film)",
    "realityCreation": "Reality Creation",
    "realityCreationIntensity": "Reality Creation intensity",
    "realityCreationResolution": "Reality Creation resolution",
    "realityCreationType": "Reality Creation type",
    "sensorAutoAdjustment": "Ambient Optimization",
}


def label(target: str) -> str:
    """Human name: curated where the TV's id reads badly, else de-camelCased."""
    if target in _LABELS:
        return _LABELS[target]
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", target).lower()
    return words[:1].upper() + words[1:]


def _range(candidates: list[Any]) -> dict[str, Any] | None:
    first = candidates[0] if candidates else None
    if isinstance(first, dict) and "min" in first and "max" in first:
        return first
    return None


def _values(candidates: list[Any]) -> list[str]:
    return [
        str(c["value"])
        for c in candidates
        if isinstance(c, dict) and c.get("value") is not None
    ]


def kind_of(target: str, info: dict[str, Any] | None) -> str | None:
    """The entity kind for ``target``, or None while it cannot be told."""
    if target in EXCLUDED:
        return None
    candidates = (info or {}).get("candidates") or []
    if _range(candidates):
        return KIND_NUMBER
    values = _values(candidates)
    if values:
        return KIND_SWITCH if sorted(values) == ["off", "on"] else KIND_SELECT
    if target in _FALLBACK:
        return _FALLBACK[target][0]
    return None


def number_range(
    target: str, info: dict[str, Any] | None
) -> tuple[float, float, float]:
    """(min, max, step) for a number target."""
    rng = _range((info or {}).get("candidates") or [])
    if rng is None and target in _FALLBACK and isinstance(_FALLBACK[target][1], dict):
        rng = _FALLBACK[target][1]
    rng = rng or {}
    return (
        float(rng.get("min", 0)),
        float(rng.get("max", 100)),
        float(rng.get("step", 1) or 1),
    )


def select_options(target: str, info: dict[str, Any] | None) -> list[str]:
    """Choices for a select target."""
    values = _values((info or {}).get("candidates") or [])
    if not values and target in _FALLBACK and isinstance(_FALLBACK[target][1], list):
        values = list(_FALLBACK[target][1])
    return values


def numeric(value: Any) -> float | None:
    """The TV's string value as a number, or None (e.g. empty off-source)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
