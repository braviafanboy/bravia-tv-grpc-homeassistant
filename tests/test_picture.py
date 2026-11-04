"""Pure tests for the picture-target classification (no Home Assistant)."""

from custom_components.bravia_tv_grpc import picture


def test_kind_from_candidates():
    rng = {"candidates": [{"min": 0, "max": 100, "step": 1}]}
    onoff = {"candidates": [{"value": "on"}, {"value": "off"}]}
    values = {"candidates": [{"value": "cool"}, {"value": "warm"}]}
    assert picture.kind_of("contrast", rng) == picture.KIND_NUMBER
    assert picture.kind_of("autoToneCurve", onoff) == picture.KIND_SWITCH
    assert picture.kind_of("colorTemperature", values) == picture.KIND_SELECT


def test_gRPC_owned_targets_are_excluded():
    rng = {"candidates": [{"min": 0, "max": 50, "step": 1}]}
    assert picture.kind_of("brightness", rng) is None
    assert picture.kind_of("pictureMode", {"candidates": [{"value": "x"}]}) is None


def test_hdmi_only_targets_fall_back_when_empty():
    empty = {"value": "", "available": False, "candidates": []}
    assert picture.kind_of("wideMode", empty) == picture.KIND_SELECT
    assert picture.kind_of("displayArea", empty) == picture.KIND_NUMBER
    assert picture.kind_of("hdmiSignalFormatVrr", empty) == picture.KIND_SWITCH
    assert picture.number_range("displayArea", empty) == (-5, 5, 1)
    assert picture.select_options("hdmiVideoRange", empty) == [
        "auto",
        "full",
        "limited",
    ]
    assert picture.kind_of("someNewTarget", empty) is None


def test_labels():
    assert picture.label("sensorAutoAdjustment") == "Ambient Optimization"
    assert picture.label("hdrToneMapping") == "HDR tone mapping"
    assert picture.label("blueGain") == "Blue gain"
    assert picture.label("magentaSaturation") == "Magenta saturation"


def test_numeric():
    assert picture.numeric("90") == 90
    assert picture.numeric("") is None
    assert picture.numeric(None) is None


def test_point_lists_are_their_own_kind():
    offsets = {
        "value": "[-2, 0, -2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]",
        "available": True,
        "candidates": [{"min": -50, "max": 50, "step": 1}],
    }
    assert picture.kind_of("redOffset", offsets) == picture.KIND_POINTS
    assert picture.points(offsets["value"])[:3] == [-2.0, 0.0, -2.0]
    assert len(picture.points(offsets["value"])) == 20
    assert picture.range_of(offsets) == {"min": -50, "max": 50, "step": 1}
    # A plain number stays a number; junk and non-numeric lists are not points.
    assert (
        picture.kind_of("contrast", {**offsets, "value": "90"}) == picture.KIND_NUMBER
    )
    assert picture.points("[1, 2") is None
    assert picture.points('["a"]') is None
    assert picture.points(None) is None
