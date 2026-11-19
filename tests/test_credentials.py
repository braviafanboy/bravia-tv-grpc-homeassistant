"""Unit tests for pure helpers in grpc/credentials.py."""

from __future__ import annotations

from btvgrpc import credentials
import pytest

_TV = {
    "device_id": "tv-id",
    "device_type": "TV",
    "attributes": {"device_unique_id": "0a1b2c3d"},
    "device_infos": {"model_name": "BRAVIA 8 II"},
}
_SPEAKER = {
    "device_id": "spk-id",
    "device_type": "Speaker",
    "attributes": {"device_unique_id": "f9e8d7c6"},
    "device_infos": {"model_name": "BRAVIA Theatre Quad"},
}


def test_select_tv_by_unique_id():
    got = credentials.select_tv_device([_TV, _SPEAKER], device_unique_id="0a1b2c3d")
    assert got["device_id"] == "tv-id"


def test_select_unique_id_is_case_insensitive():
    # mDNS and the cloud do not agree on the case of the id. With a second TV
    # on the account there is no single-TV fallback to hide a missed match.
    other = {**_TV, "device_id": "tv-2", "attributes": {"device_unique_id": "99887766"}}
    devices = [other, _TV, _SPEAKER]
    got = credentials.select_tv_device(devices, device_unique_id="0A1B2C3D")
    assert got["device_id"] == "tv-id"
    upper = {**_TV, "attributes": {"device_unique_id": "0A1B2C3D"}}
    got = credentials.select_tv_device([other, upper], device_unique_id="0a1b2c3d")
    assert got["device_id"] == "tv-id"
    # No match and more than one TV: say so instead of guessing.
    with pytest.raises(credentials.GrpcOAuthError):
        credentials.select_tv_device(devices, device_unique_id="ffff")


def test_select_soundbar_unique_id_rejected():
    # The reported bug: a soundbar shares the account and mDNS service.
    with pytest.raises(credentials.GrpcNotATvError):
        credentials.select_tv_device([_TV, _SPEAKER], device_unique_id="f9e8d7c6")


def test_select_falls_back_to_single_tv():
    # Manual flow (no unique id): the sole TV is chosen over the soundbar.
    assert credentials.select_tv_device([_TV, _SPEAKER])["device_id"] == "tv-id"


def test_select_by_explicit_device_id_reauth():
    # Re-auth passes the entry's device_id and gets exactly that device.
    assert credentials.select_tv_device([_TV, _SPEAKER], device_id="spk-id") is _SPEAKER


def test_select_unknown_unique_id_falls_back_to_tv():
    got = credentials.select_tv_device([_TV, _SPEAKER], device_unique_id="ffff")
    assert got["device_id"] == "tv-id"


_UID_A = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
_UID_B = "0a1b2c3d4e5f60718293a4b5c6d7e8f9aaaaaaaa"  # begins like A for 32 characters
_UID_BAR = "f9e8d7c6b5a4938271605f4e3d2c1b0a99887766"


def _device(device_id: str, unique_id: str, device_type: str = "TV") -> dict:
    return {
        "device_id": device_id,
        "device_type": device_type,
        "attributes": {"device_unique_id": unique_id},
        "device_infos": {"model_name": "x"},
    }


def test_select_by_the_beginning_of_the_unique_id():
    """A TV with a long name announces only the beginning of its id (the name
    is cut to fit); the account lists the whole one."""
    tv_a, tv_b = _device("tv-a", _UID_A), _device("tv-b", _UID_B)
    bar = _device("bar", _UID_BAR, "Speaker")
    select = credentials.select_tv_device
    # 36 characters: what a 23-character name leaves room for.
    assert select([tv_b, tv_a, bar], device_unique_id=_UID_A[:36])["device_id"] == (
        "tv-a"
    )
    assert select([tv_b, tv_a], device_unique_id=_UID_B[:33].upper())["device_id"] == (
        "tv-b"
    )
    # A soundbar is recognised the same way, and refused.
    with pytest.raises(credentials.GrpcNotATvError):
        select([tv_a, bar], device_unique_id=_UID_BAR[:39])


def test_select_a_beginning_two_devices_share_settles_nothing():
    tv_a, tv_b = _device("tv-a", _UID_A), _device("tv-b", _UID_B)
    with pytest.raises(credentials.GrpcOAuthError, match="Multiple"):
        credentials.select_tv_device([tv_a, tv_b], device_unique_id=_UID_A[:32])


def test_select_ignores_a_beginning_too_short_to_mean_anything():
    tv_a, bar = _device("tv-a", _UID_A), _device("bar", _UID_BAR, "Speaker")
    # Seven characters of the soundbar's id are not taken for the soundbar.
    got = credentials.select_tv_device([tv_a, bar], device_unique_id=_UID_BAR[:7])
    assert got["device_id"] == "tv-a"


def test_select_the_whole_id_wins_over_one_that_begins_alike():
    whole = _device("tv-a", "0a1b2c3d")
    longer = _device("tv-b", "0a1b2c3d99")
    got = credentials.select_tv_device([longer, whole], device_unique_id="0a1b2c3d")
    assert got["device_id"] == "tv-a"


def test_select_survives_a_device_without_a_unique_id():
    odd = {"device_id": "odd", "device_type": "TV", "attributes": None}
    none = {
        "device_id": "none",
        "device_type": "TV",
        "attributes": {"device_unique_id": None},
    }
    tv_a = _device("tv-a", _UID_A)
    got = credentials.select_tv_device([odd, none, tv_a], device_unique_id=_UID_A[:30])
    assert got["device_id"] == "tv-a"


def test_select_no_tv_raises():
    with pytest.raises(credentials.GrpcOAuthError):
        credentials.select_tv_device([_SPEAKER])


def test_device_hardware_info():
    device = {
        "device_id": "x",
        "device_infos": {
            "model_name": "BRAVIA 8 II",
            "name": "BRAVIA 8 II",
            "firmware_version": "114.602.080.1EUA",
            "software_version": "4.1.4",
        },
        "attributes": {"identified_model_name": "K-65XR8M2"},
    }
    assert credentials.device_hardware_info(device) == {
        "model": "BRAVIA 8 II",
        "model_id": "K-65XR8M2",
        "sw_version": "114.602.080.1EUA",
    }


def test_device_hardware_info_missing():
    assert credentials.device_hardware_info({}) == {
        "model": None,
        "model_id": None,
        "sw_version": None,
    }


def test_refresh_error_carries_http_status():
    """A cloud error response records its status so callers can tell a rejected
    refresh token (400/401) from a transient failure."""
    err = credentials.GrpcCredentialsRefreshError("HTTP 401", status=401)
    assert err.status == 401
    assert str(err) == "HTTP 401"


def test_refresh_error_status_defaults_none():
    """A refresh error raised without an HTTP response (e.g. a reset connection)
    has no status, so it is treated as transient rather than auth-fatal."""
    err = credentials.GrpcCredentialsRefreshError("connection reset")
    assert err.status is None
