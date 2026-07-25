"""HA sync façade over pybravia-connect for BRAVIA TVs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pybravia_connect import AuthError, BraviaConnectClient, CapabilityMeta
from pybravia_connect import ConnectionError as BraviaConnectionError

__all__ = [
    "AuthError",
    "BraviaConnectBackend",
    "BraviaConnectionError",
    "CapabilityMeta",
]

DeltaCallback = Callable[[str, Any], None]


class BraviaConnectBackend:
    """Thin sync wrapper around ``BraviaConnectClient`` for the TV coordinator."""

    def __init__(
        self,
        host: str,
        port: int,
        device_id: str,
        hmac_key: str,
        *,
        key_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """Build the library client (call ``connect`` before RPCs)."""
        self.host = host
        self.port = port
        self._lib = BraviaConnectClient(
            host,
            port,
            device_id=device_id,
            hmac_key=hmac_key,
            key_id=key_id,
            session_key=session_key,
        )

    def connect(self, timeout: float = 5.0) -> None:
        """Open the channel and run the auth handshake."""
        self._lib.connect(timeout=timeout)

    def close(self) -> None:
        """Stop notify and close the library channel."""
        self._lib.close()

    def get_capabilities(self, timeout: float = 10.0) -> dict[str, CapabilityMeta]:
        """Fetch and cache the device field-path schema."""
        return self._lib.get_capabilities(timeout=timeout)

    @property
    def capabilities(self) -> dict[str, CapabilityMeta]:
        """Cached GetCapabilities index."""
        return self._lib.capabilities

    def get_states(
        self, paths: list[str] | None = None, timeout: float = 10.0
    ) -> dict[str, Any]:
        """Signed bulk GetStatesWithAuth."""
        return self._lib.get_states(paths, timeout=timeout)

    def read_application_list(self, timeout: float = 8.0) -> list[dict]:
        """Read the installed-app list (nonce-gated, AES-GCM)."""
        return self._lib.read_application_list(timeout=timeout)

    def read_resource(self, uri: str, timeout: float = 8.0) -> bytes:
        """Fetch and decrypt a resource (e.g. an app icon)."""
        return self._lib.read_resource(uri, timeout=timeout)

    def exec_command(self, path: str, value: Any, timeout: float = 8.0) -> bool:
        """Set *path* to *value*; returns False when the TV rejects the write."""
        return self._lib.exec_command(path, value, timeout=timeout)

    def start_notify(
        self,
        on_delta: DeltaCallback,
        on_connection_lost: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
    ) -> None:
        """Start the StartNotifyStates worker thread."""
        self._lib.start_notify(
            on_delta,
            on_connection_lost=on_connection_lost,
            on_reconnect=on_reconnect,
        )

    def stop_notify(self) -> None:
        """Stop the notify worker."""
        self._lib.stop_notify()
