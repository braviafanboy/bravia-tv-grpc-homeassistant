"""Unit tests for pure helpers in grpc/credentials.py."""

from __future__ import annotations

from btvgrpc import credentials


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
