"""Small shared helpers for the Bravia TV (gRPC) integration."""

from __future__ import annotations

import json
import re
from typing import Any

# Acronyms that should be upper-cased rather than title-cased.
_ACRONYMS = {
    "imax": "IMAX",
    "fps": "FPS",
    "rts": "RTS",
    "pc": "PC",
    "cn": "CN",
    "hdr": "HDR",
}
# Short words kept lowercase (except as the first word).
_SMALL_WORDS = {"for", "and", "of", "the"}


def friendly_enum_label(value: str) -> str:
    """Turn a device enum value (camelCase) into a display label.

    The TV reports select options as raw camelCase strings (``customForPro1``,
    ``dolbyVisionDark``, ``movieMovieNightStandard``). Home Assistant's select
    option translations require lowercase-slug keys, so these can't be
    translated in strings.json; instead we humanise them here. A leading
    ``movie`` qualifier on the long compound picture modes is dropped (it just
    repeats the content mode), e.g. ``movieDaytimeStandard`` -> "Daytime
    Standard", while the short ``movieNight`` stays "Movie Night".
    """
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    text = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    tokens = text.split()
    if len(tokens) >= 3 and tokens[0] == "movie":
        tokens = tokens[1:]
    out = []
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low in _ACRONYMS:
            out.append(_ACRONYMS[low])
        elif low in _SMALL_WORDS and i > 0:
            out.append(low)
        else:
            out.append(tok[:1].upper() + tok[1:])
    return " ".join(out) or value


def parse_json_value(raw: Any) -> Any | None:
    """Parse an ``any``-typed gRPC value into Python, or None.

    The device's ``any``-typed fields (inputs, audio devices, signal info, …)
    arrive as JSON-encoded strings; this centralises the string-check + tolerant
    parse that every platform otherwise repeats."""
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def source_label(value: object) -> str | None:
    """Extract a display source label from a ``tvapp.input`` value.

    Accepts the gRPC any-typed JSON payload (e.g.
    ``{"type":"hdmi","label":"HDMI 3 (eARC/ARC)"}``) or a plain cloud label
    string. Returns None when no external input is active (``{"type":"none"}``).
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if not text.startswith("{"):
        return text
    data = parse_json_value(text)
    if isinstance(data, dict):
        label = data.get("label") or data.get("sub_label")
        return label if isinstance(label, str) else None
    return None
