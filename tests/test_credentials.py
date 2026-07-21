"""Unit tests for pure helpers in grpc/credentials.py."""

from __future__ import annotations

from btvgrpc import credentials
import pytest

_TV = {
    "device_id": "tv-id",
    "device_type": "TV",
    "attributes": {"device_unique_id": "51b397cf"},
    "device_infos": {"model_name": "BRAVIA 8 II"},
}
_SPEAKER = {
    "device_id": "spk-id",
    "device_type": "Speaker",
    "attributes": {"device_unique_id": "bac76409"},
    "device_infos": {"model_name": "BRAVIA Theatre Quad"},
}


def test_select_tv_by_unique_id():
    got = credentials.select_tv_device([_TV, _SPEAKER], device_unique_id="51b397cf")
    assert got["device_id"] == "tv-id"


def test_select_unique_id_is_case_insensitive():
    got = credentials.select_tv_device([_TV, _SPEAKER], device_unique_id="51B397CF")
    assert got["device_id"] == "tv-id"


def test_select_soundbar_unique_id_rejected():
    # The reported bug: a soundbar shares the account and mDNS service.
    with pytest.raises(credentials.GrpcNotATvError):
        credentials.select_tv_device([_TV, _SPEAKER], device_unique_id="bac76409")


def test_select_falls_back_to_single_tv():
    # Manual flow (no unique id): the sole TV is chosen over the soundbar.
    assert credentials.select_tv_device([_TV, _SPEAKER])["device_id"] == "tv-id"


def test_select_by_explicit_device_id_reauth():
    # Re-auth passes the entry's device_id and gets exactly that device.
    assert credentials.select_tv_device([_TV, _SPEAKER], device_id="spk-id") is _SPEAKER


def test_select_unknown_unique_id_falls_back_to_tv():
    got = credentials.select_tv_device([_TV, _SPEAKER], device_unique_id="ffff")
    assert got["device_id"] == "tv-id"


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
