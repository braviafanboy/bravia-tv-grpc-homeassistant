"""A fake TV for the Home Assistant tests: the real ``BraviaTvGrpcClient`` with
the network taken out.

It serves the capability schema captured from a BRAVIA 8 II (committed under
``schema/``), so a full setup creates the same entities a real TV would, and
its reachability, key acceptance and identity can be switched to act out a TV
that goes to standby, reboots, or is impersonated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.bravia_tv_grpc.bravia_tv_client import (
    BraviaTvAuthError,
    BraviaTvConnectionError,
    BraviaTvGrpcClient,
)

_SCHEMA = Path(__file__).resolve().parents[1] / "schema"
# The schema as the TV serves it (the JSON text of GetCapabilities).
CAPS_TEXT = json.dumps(json.loads((_SCHEMA / "grpc_capabilities.json").read_text()))
CLOUD_DEVICE = json.loads((_SCHEMA / "cloud_device.json").read_text())
CLOUD_STATES = json.loads((_SCHEMA / "cloud_states.json").read_text())

STATES: dict[str, Any] = {
    "power": True,
    "volume": 18,
    "mute": False,
    "display_setting.brightness": 30,
    "display_setting.picture_mode": "professional",
    "system_setting.friendly_name": "BRAVIA 8 II",
    "system_setting.application": "com.netflix.ninja",
}
CREDS = {
    "device_id": "dev",
    "hmac_key": "ab" * 32,
    "key_id": "k1",
    "session_key": "cd" * 32,
    "refresh_token": "rt",
    "access_token": "at",
    "access_token_expires_at": 4102444800,
}


class FakeTv(BraviaTvGrpcClient):
    """``BraviaTvGrpcClient`` whose calls answer from this object."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reachable = True  # anything listening at host:port
        self.reject_keys = False  # the handshake refuses our keys
        self.genuine = True  # can produce the encrypted app list
        # Addresses where something else answers in the TV's place: it speaks
        # the protocol, but cannot produce the encrypted app list.
        self.impostor_hosts: set[str] = set()
        self.caps_text: str | None = CAPS_TEXT
        self.states = dict(STATES)
        self.apps = [{"id": "com.netflix.ninja", "label": "Netflix"}]
        self.writes: list[tuple[str, Any]] = []
        self.rekeys: list[tuple[Any, ...]] = []
        self.connects: list[tuple[str, int]] = []
        self.pushes_started = 0
        self.closed = 0

    # -- lifecycle ---------------------------------------------------------
    def connect(self, timeout: float = 5.0) -> None:
        self.close()
        self.connects.append((self.host, self.port))
        if not self.reachable:
            raise BraviaTvConnectionError(
                f"gRPC channel to {self.host}:{self.port} not ready"
            )
        if self.reject_keys:
            raise BraviaTvAuthError("handshake failed: INVALID_ARGUMENT")
        self._channel = object()
        self._stub = object()
        self._session_random = b"r" * 64

    def close(self) -> None:
        self.closed += 1
        self.stop_notify()
        self._channel = self._stub = self._session_random = None

    def rekey(self, hmac_key, key_id, session_key, timeout: float = 5.0) -> None:
        self.rekeys.append((hmac_key, key_id, session_key))
        self._hmac_key, self._session_key = hmac_key, session_key

    # -- reads -------------------------------------------------------------
    def _need(self) -> None:
        if not self.connected or not self.reachable:
            raise BraviaTvConnectionError("not connected")

    def get_capabilities(self, timeout: float = 10.0):
        self._need()
        if not self.caps_text:
            raise BraviaTvConnectionError("the TV sent no capability schema")
        return self.load_capabilities(self.caps_text)

    def get_states(self, paths=None, timeout: float = 10.0):
        self._need()
        if paths is None:
            return dict(self.states)
        return {p: self.states[p] for p in paths if p in self.states}

    def read_application_list(self, timeout: float = 8.0):
        self._need()
        if not self.genuine or self.host in self.impostor_hosts:
            raise ValueError("encrypted application_list block not found in response")
        return list(self.apps)

    def read_resource(self, uri: str, timeout: float = 8.0) -> bytes:
        self._need()
        return b""

    # -- writes ------------------------------------------------------------
    def exec_command(self, path: str, value: Any, timeout: float = 8.0) -> bool:
        self._need()
        self.writes.append((path, value))
        return True

    # -- push --------------------------------------------------------------
    def start_notify(self, on_delta, on_connection_lost=None, on_reconnect=None):
        self.pushes_started += 1
        self._on_delta = on_delta
        self._on_connection_lost = on_connection_lost
        self._on_reconnect = on_reconnect

    def stop_notify(self) -> None:
        self._on_delta = None
