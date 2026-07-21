"""Local gRPC client for BRAVIA TVs (ControlDeviceService over h2c).

Intentionally HA-agnostic so it can be exercised standalone against a real TV.
Wraps the ported wire builders for the three calls verified working on a
BRAVIA 8 II / K-65XR8M2:

- ``GetCapabilities`` (unauthenticated) -> the device's full field-path schema.
- ``StartNotifyStates`` (server stream) -> live ``(path, value)`` state deltas.
- ``ExecCommandWithAuth`` (signed) -> set a field.

The unary ``GetStatesWithAuth`` snapshot is deliberately not used: its TV
request wire format is unresolved, and push + an initial snapshot from another
source cover reads.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import hmac
import logging
import threading
from typing import Any
import uuid

import grpc

from .grpc.application_list import (
    build_application_list_request,
    parse_application_list_response,
    parse_get_nonce_response,
)
from .grpc.bravia_control_pb2 import (
    ConfirmKeysRequest,
    ConfirmSigninRequest,
    GetSessionRandomRequest,
    StartNotifyStatesRequest,
)
from .grpc.bravia_control_pb2_grpc import ControlDeviceServiceStub
from .grpc.exec_command_request import (
    build_exec_command_with_auth_request,
    parse_exec_response,
    sign_exec_auth_token,
)
from .grpc.get_capabilities_response import (
    CapabilityMeta,
    decode_capabilities_json_text,
    get_capabilities_method,
    parse_capability_index,
    paths_for_safe_get_states,
)
from .grpc.get_nonce_request import build_get_nonce_request
from .grpc.get_states_auth import sign_get_states_request_body
from .grpc.get_states_request import build_get_states_with_auth_request
from .grpc.get_states_response import parse_get_states_response
from .grpc.notify_decode import parse_notify_message
from .grpc.resources import (
    build_get_resource_request,
    parse_get_resource_response,
)

_LOGGER = logging.getLogger(__name__)

_SERVICE = "jp.co.sony.hes.ssh.controldevice.v1.ControlDeviceService"
_EXEC_METHOD = f"/{_SERVICE}/ExecCommandWithAuth"
_GET_STATES_METHOD = f"/{_SERVICE}/GetStatesWithAuth"
_GET_RESOURCES_METHOD = f"/{_SERVICE}/GetResourcesWithAuth"
_GET_NONCE_METHOD = f"/{_SERVICE}/GetNonce"
_NOTIFY_METHOD = f"/{_SERVICE}/StartNotifyStates"

DeltaCallback = Callable[[str, Any], None]


class BraviaTvAuthError(Exception):
    """Authentication handshake failed."""


class BraviaTvConnectionError(Exception):
    """Channel could not be established."""


def _hmac_key_bytes(hmac_key_hex: str) -> bytes:
    if len(hmac_key_hex) == 64:
        return bytes.fromhex(hmac_key_hex)
    return hmac_key_hex.encode("utf-8")[:32].ljust(32, b"\x00")


class BraviaTvGrpcClient:
    """Synchronous gRPC client; run blocking calls in an executor from HA."""

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
        self.host = host
        self.port = port
        self._device_id = device_id
        self._hmac_key = hmac_key
        self._session_key = session_key  # AES key for encrypted (nonce-gated) reads
        self._session_id = key_id or str(uuid.uuid4())
        self._channel: grpc.Channel | None = None
        self._stub: ControlDeviceServiceStub | None = None
        self._session_random: bytes | None = None
        self._capabilities: dict[str, CapabilityMeta] = {}
        self._safe_get_states_paths: list[str] = []

        self._notify_thread: threading.Thread | None = None
        self._notify_stop = threading.Event()
        self._on_delta: DeltaCallback | None = None
        self._on_connection_lost: Callable[[], None] | None = None
        self._on_reconnect: Callable[[], None] | None = None

    # -- lifecycle ---------------------------------------------------------
    def connect(self, timeout: float = 5.0) -> None:
        """Open the channel and run the auth handshake."""
        self._channel = grpc.insecure_channel(f"{self.host}:{self.port}")
        try:
            grpc.channel_ready_future(self._channel).result(timeout=timeout)
        except grpc.FutureTimeoutError as err:
            raise BraviaTvConnectionError(
                f"gRPC channel to {self.host}:{self.port} not ready"
            ) from err
        self._stub = ControlDeviceServiceStub(self._channel)
        self._authenticate(timeout=timeout)

    def _authenticate(self, timeout: float = 5.0) -> None:
        assert self._stub is not None
        signin = ConfirmSigninRequest()
        signin.auth_data = hashlib.sha256(self._device_id.encode()).digest()
        try:
            self._stub.ConfirmSignin(signin, timeout=timeout)
            keys = ConfirmKeysRequest()
            keys.session_id = self._session_id
            keys.key_data = hmac.new(
                _hmac_key_bytes(self._hmac_key),
                self._session_id.encode(),
                hashlib.sha256,
            ).digest()
            self._stub.ConfirmKeys(keys, timeout=timeout)
            resp = self._stub.GetSessionRandom(
                GetSessionRandomRequest(session_id=self._session_id), timeout=timeout
            )
        except grpc.RpcError as err:
            raise BraviaTvAuthError(f"handshake failed: {err.code()}") from err
        self._session_random = resp.session_random
        if not self._session_random:
            raise BraviaTvAuthError("GetSessionRandom returned no session_random")

    def close(self) -> None:
        self.stop_notify()
        if self._channel is not None:
            self._channel.close()
            self._channel = None
            self._stub = None

    # -- raw calls ---------------------------------------------------------
    # All authenticated calls frame their own bytes, so the (de)serializers are
    # pass-through rather than the generated proto ones.
    def _raw_unary(self, method: str) -> Any:
        return self._channel.unary_unary(  # type: ignore[union-attr]
            method, request_serializer=lambda p: p, response_deserializer=lambda p: p
        )

    def _raw_stream(self, method: str) -> Any:
        return self._channel.unary_stream(  # type: ignore[union-attr]
            method, request_serializer=lambda p: p, response_deserializer=lambda p: p
        )

    # -- schema ------------------------------------------------------------
    def get_capabilities(self, timeout: float = 10.0) -> dict[str, CapabilityMeta]:
        """Fetch and cache the device field-path schema (unauthenticated)."""
        assert self._channel is not None
        call = self._raw_unary(get_capabilities_method())
        raw = call(b"", timeout=timeout)
        index = parse_capability_index(raw) or {}
        self._capabilities = index
        cap_json = decode_capabilities_json_text(raw)
        # Precompute the paths safe for a single bulk GetStates read (excludes
        # write-only, nonce-gated, and command-independent paths that would make
        # the whole batch fail).
        self._safe_get_states_paths = (
            paths_for_safe_get_states(cap_json) if cap_json else []
        )
        return index

    @property
    def capabilities(self) -> dict[str, CapabilityMeta]:
        return self._capabilities

    def get_states(
        self, paths: list[str] | None = None, timeout: float = 10.0
    ) -> dict[str, Any]:
        """Bulk on-demand read: return ``{field_path: value}``.

        With no ``paths`` argument, reads every capability safe for a single
        batch (see GetCapabilities). Requires the auth handshake to have run.
        """
        if self._channel is None or self._session_random is None:
            raise BraviaTvConnectionError("not connected")
        read_paths = paths if paths is not None else self._safe_get_states_paths
        if not read_paths:
            return {}
        # Build once to sign the body, then rebuild with the real auth token.
        req = build_get_states_with_auth_request(
            read_paths,
            session_random=self._session_random,
            session_id=self._session_id,
            auth_token=b"\x00" * 32,
        )
        token = sign_get_states_request_body(self._hmac_key, req)
        req = build_get_states_with_auth_request(
            read_paths,
            session_random=self._session_random,
            session_id=self._session_id,
            auth_token=token,
        )
        call = self._raw_unary(_GET_STATES_METHOD)
        raw = call(req, timeout=timeout)
        return parse_get_states_response(raw)

    def read_application_list(self, timeout: float = 8.0) -> list[dict]:
        """Read the installed-app list (nonce-gated, AES-GCM encrypted).

        GetNonce -> signed request with the nonce in the embedded field-2 slot
        -> AES-256-GCM response decrypted with the session_key. Returns
        ``[{"id": package, "label": name, "resources": [...]}, ...]``.
        """
        if self._channel is None or self._session_random is None:
            raise BraviaTvConnectionError("not connected")
        if not self._session_key:
            raise BraviaTvConnectionError("session_key required for application_list")
        nonce = self._get_nonce(timeout)
        req = build_application_list_request(
            session_random=self._session_random,
            session_id=self._session_id,
            hmac_key_hex=self._hmac_key,
            nonce=nonce,
        )
        call = self._raw_unary(_GET_STATES_METHOD)
        return parse_application_list_response(
            call(req, timeout=timeout), self._session_key
        )

    def read_resource(self, uri: str, timeout: float = 8.0) -> bytes:
        """Fetch + decrypt a resource (e.g. an app icon) by its iot-resource URI.

        Same nonce-gated flow as the app list, against GetResourcesWithAuth with
        the full resource URI as the path; the decrypted plaintext is the raw
        resource (an image) bytes.
        """
        if self._channel is None or self._session_random is None:
            raise BraviaTvConnectionError("not connected")
        if not self._session_key:
            raise BraviaTvConnectionError("session_key required for resources")
        nonce = self._get_nonce(timeout)
        req = build_get_resource_request(
            uri=uri,
            session_random=self._session_random,
            session_id=self._session_id,
            hmac_key_hex=self._hmac_key,
            nonce=nonce,
        )
        call = self._raw_unary(_GET_RESOURCES_METHOD)
        return parse_get_resource_response(
            call(req, timeout=timeout), self._session_key
        )

    def _get_nonce(self, timeout: float) -> bytes:
        """Fetch a fresh single-use nonce for a nonce-gated authenticated read."""
        nonce_call = self._raw_unary(_GET_NONCE_METHOD)
        return parse_get_nonce_response(
            nonce_call(build_get_nonce_request(self._session_id), timeout=timeout)
        )

    # -- control -----------------------------------------------------------
    def exec_command(self, path: str, value: Any, timeout: float = 8.0) -> bool:
        """Set *path* to *value*, choosing the wire type from the capability.

        Fields gated by a prerequisite (e.g. the EU ``lot5Agreement`` energy
        agreement on content_mode / theatre_mode / picture settings) silently
        no-op unless the prerequisite is confirmed first, so do that here.
        """
        if self._stub is None or self._session_random is None:
            raise BraviaTvConnectionError("not connected")
        self._confirm_prerequisites(path, timeout)
        return self._exec(path, timeout, **self._value_kwargs(path, value))

    def _exec(self, path: str, timeout: float, **value_kwargs: Any) -> bool:
        token = sign_exec_auth_token(
            self._hmac_key,
            path,
            session_random=self._session_random,
            session_id=self._session_id,
            **value_kwargs,
        )
        req = build_exec_command_with_auth_request(
            path,
            session_random=self._session_random,
            session_id=self._session_id,
            auth_token=token,
            **value_kwargs,
        )
        call = self._raw_unary(_EXEC_METHOD)
        return parse_exec_response(call(req, timeout=timeout))

    def _confirm_prerequisites(self, path: str, timeout: float) -> None:
        """Confirm any prerequisites (e.g. lot5Agreement) gating a write on *path*."""
        meta = self._capabilities.get(f"{path}.confirm_prerequisite")
        if meta is None or not meta.values:
            return
        for prerequisite in meta.values:
            try:
                self._exec(
                    f"{path}.confirm_prerequisite", timeout, string_value=prerequisite
                )
            except grpc.RpcError as err:
                _LOGGER.debug(
                    "confirm_prerequisite %s for %s failed: %s",
                    prerequisite,
                    path,
                    err.code(),
                )

    def _value_kwargs(self, path: str, value: Any) -> dict[str, Any]:
        """Map a Python value to the exec value kwarg for *path*'s type."""
        meta = self._capabilities.get(path)
        cap_type = meta.type if meta else None
        if cap_type == "bool" or isinstance(value, bool):
            return {"bool_value": bool(value)}
        if cap_type == "int" or isinstance(value, int):
            return {"int_value": int(value)}
        # any-typed fields (application/input/...) need the field-7 encoding;
        # enum/string fields use field 6.
        if cap_type == "any":
            return {"any_value": str(value)}
        return {"string_value": str(value)}

    # -- push --------------------------------------------------------------
    def start_notify(
        self,
        on_delta: DeltaCallback,
        on_connection_lost: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
    ) -> None:
        """Start (or restart) the StartNotifyStates worker thread.

        ``on_connection_lost`` is invoked (from the worker thread) when the
        stream can't be re-established after several rapid failures — typically
        the TV rebooting onto a new dynamic port, which only a reload recovers.
        ``on_reconnect`` fires when the stream re-subscribes after a drop, so the
        caller can reconcile any state that changed while disconnected.
        """
        self._on_delta = on_delta
        self._on_connection_lost = on_connection_lost
        self._on_reconnect = on_reconnect
        self._notify_stop.clear()
        self._notify_thread = threading.Thread(
            target=self._notify_worker, name="bravia_tv_notify", daemon=True
        )
        self._notify_thread.start()

    def stop_notify(self) -> None:
        self._notify_stop.set()
        thread = self._notify_thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._notify_thread = None

    def _notify_worker(self) -> None:
        backoff = 1.0
        failures = 0
        had_drop = False
        while not self._notify_stop.is_set():
            if self._stub is None:
                break
            try:
                # Raw deserializer: the installed-app-list change is pushed as a
                # large AES-GCM blob that the generated StartNotifyStatesResponse
                # can't parse (it would break the stream), so decode bytes ourselves.
                notify = self._raw_stream(_NOTIFY_METHOD)
                stream = notify(
                    StartNotifyStatesRequest(
                        session_id=self._session_id
                    ).SerializeToString()
                )
                for raw in stream:
                    if self._notify_stop.is_set():
                        stream.cancel()
                        break
                    if had_drop:
                        # Re-subscribed after a drop: reconcile state (incl. the
                        # app list) that may have changed while disconnected.
                        had_drop = False
                        if self._on_reconnect is not None:
                            try:
                                self._on_reconnect()
                            except Exception:  # noqa: BLE001
                                _LOGGER.exception("notify reconnect handler failed")
                    failures = 0  # a message proves the connection is alive
                    path, value = parse_notify_message(raw)
                    if path and self._on_delta is not None:
                        try:
                            self._on_delta(path, value)
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception("notify callback failed for %s", path)
                # The stream was established and ended without error: the port is
                # alive, so this is a clean re-subscribe, not a lost connection.
                failures = 0
                backoff = 1.0
                had_drop = True  # re-read the app list after re-subscribing
            except grpc.RpcError as err:
                if self._notify_stop.is_set():
                    break
                had_drop = True  # re-read the app list once we re-subscribe
                # Consecutive errors without an intervening delta mean the
                # connection can't be re-established (dead/changed port). Count
                # them straight up — no time-based reset, which previously let a
                # growing backoff space failures out and never reach threshold.
                failures += 1
                _LOGGER.debug(
                    "notify stream ended (%s); reconnect attempt %d",
                    err.code(),
                    failures,
                )
                if failures >= 3 and self._on_connection_lost is not None:
                    _LOGGER.warning(
                        "Notify connection lost after %d attempts; requesting reload",
                        failures,
                    )
                    self._on_connection_lost()
                    break
            # brief backoff before re-subscribing (capped low for fast detection)
            self._notify_stop.wait(backoff)
            backoff = min(backoff * 2, 5.0)
