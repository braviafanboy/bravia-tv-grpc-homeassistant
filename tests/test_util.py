"""Unit tests for the pure helpers in util.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_UTIL = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "bravia_tv_grpc"
    / "util.py"
)
_spec = importlib.util.spec_from_file_location("bravia_util", _UTIL)
util = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(util)


def test_friendly_enum_label_basic():
    assert util.friendly_enum_label("standard") == "Standard"
    assert util.friendly_enum_label("off") == "Off"
    assert util.friendly_enum_label("multiView") == "Multi View"


def test_friendly_enum_label_acronyms_and_numbers():
    assert util.friendly_enum_label("imax") == "IMAX"
    assert util.friendly_enum_label("gameFps") == "Game FPS"
    assert util.friendly_enum_label("pcStandard") == "PC Standard"
    assert util.friendly_enum_label("customForPro1") == "Custom for Pro 1"
    assert util.friendly_enum_label("dolbyVisionDark") == "Dolby Vision Dark"


def test_friendly_enum_label_movie_prefix_rule():
    # 2-token movie* keeps the qualifier; 3+ tokens drop the redundant "movie".
    assert util.friendly_enum_label("movieNight") == "Movie Night"
    assert util.friendly_enum_label("movieDaytimeStandard") == "Daytime Standard"
    assert util.friendly_enum_label("movieMovieNightStandard") == "Movie Night Standard"
    assert (
        util.friendly_enum_label("movieFilmmakerExperienceCalibrated")
        == "Filmmaker Experience Calibrated"
    )


def test_parse_json_value():
    assert util.parse_json_value('[{"a": 1}]') == [{"a": 1}]
    assert util.parse_json_value('{"x": 2}') == {"x": 2}
    assert util.parse_json_value("not json") is None
    assert util.parse_json_value(None) is None
    assert util.parse_json_value(123) is None
