"""Local gRPC client for BRAVIA TVs (ControlDeviceService over h2c).

Intentionally HA-agnostic so it can be exercised standalone against a real TV.
Wraps the ported wire builders for the calls verified working on a
BRAVIA 8 II / K-65XR8M2:

- ``GetCapabilities`` (unauthenticated) -> the device's full field-path schema.
- ``StartNotifyStates`` (server stream) -> live ``(path, value)`` state deltas.
- ``GetStatesWithAuth`` (signed, unary) -> bulk snapshot of every readable
  path; also used as a single-path liveness probe and, nonce-gated, for the
  AES-GCM encrypted application list.
- ``GetResourcesWithAuth`` (signed, nonce-gated) -> encrypted resources such
  as app icons.
- ``ExecCommandWithAuth`` (signed) -> set a field.

The unsigned twins ``GetStates`` / ``ExecCommand`` / ``GetResources`` are
hard-coded ``UNAVAILABLE`` stubs on the TV, and ``GenericCommandWithAuth`` is
the wireless-speaker pairing channel, so neither is used here.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import hmac
import json
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
    capability_index_from_json,
    decode_capabilities_json_text,
    get_capabilities_method,
    paths_for_safe_get_states,
)
from .grpc.get_nonce_request import build_get_nonce_request
from .grpc.get_states_auth import sign_get_states_auth_token
from .grpc.get_states_request import build_get_states_body, wrap_signed_request
from .grpc.get_states_response import parse_get_states_response
from .grpc.keys import hmac_key_bytes
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


class BraviaTvError(Exception):
    """The TV could not be reached, or refused the session."""


class BraviaTvAuthError(BraviaTvError):
    """Authentication handshake failed."""


class BraviaTvConnectionError(BraviaTvError):
    """Channel could not be established, or a call did not get through."""


class BraviaTvCallError(BraviaTvError):
    """The TV answered a call with an error status (``code``): it is there,
    but refused this request (a value it does not take, a session it will not
    renew)."""

    def __init__(self, what: str, code: Any) -> None:
        super().__init__(f"{what} failed: {code}")
        self.code = code


# Statuses that say "the TV did not answer", as opposed to "the TV answered
# with a refusal".
_UNREACHABLE = (
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.CANCELLED,
)


def _code(err: BaseException) -> Any:
    """The status code of a gRPC error, or its type name when it has none."""
    code = getattr(err, "code", None)
    try:
        return code() if callable(code) else type(err).__name__
    except Exception:  # noqa: BLE001 - only used to describe the error
        return type(err).__name__


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
        self._capabilities_json: str | None = None
        self._safe_get_states_paths: list[str] = []

        self._notify_thread: threading.Thread | None = None
        self._notify_stop = threading.Event()
        # The open push stream, so stop_notify can cancel it instead of
        # waiting for the TV's next message.
        self._notify_call: Any = None
        # Nonce-gated reads must not interleave: the TV hands out single-use
        # nonces and only the latest is valid, so GetNonce + the read that
        # consumes it run under one lock (HA fires app-icon reads in parallel).
        self._nonce_lock = threading.Lock()
        # Writes go out in call order: HA runs entity services on executor
        # threads, so two quick writes to one field (a dragged slider) could
        # otherwise reach the TV reversed and leave the earlier value in place.
        self._write_lock = threading.Lock()
        # The TV keeps sessions in memory and drops them by age (the key's
        # expiresIn) or when its Seeds service restarts; a rejected session
        # is re-established in place rather than through a reload.
        self._auth_lock = threading.Lock()
        self._on_delta: DeltaCallback | None = None
        self._on_connection_lost: Callable[[], None] | None = None
        self._on_reconnect: Callable[[], None] | None = None

    # -- lifecycle ---------------------------------------------------------
    @property
    def connected(self) -> bool:
        """Whether a channel is open and the handshake has run."""
        return self._channel is not None and self._session_random is not None

    @property
    def has_session_key(self) -> bool:
        """Whether the key for encrypted reads (app list, icons) is known."""
        return bool(self._session_key)

    def connect(self, timeout: float = 5.0) -> None:
        """Open the channel and run the auth handshake.

        Replaces any earlier channel, and leaves none behind when it fails:
        the client is either connected or closed, never half-open."""
        self.close()
        channel = grpc.insecure_channel(f"{self.host}:{self.port}")
        try:
            grpc.channel_ready_future(channel).result(timeout=timeout)
        except grpc.FutureTimeoutError as err:
            channel.close()
            raise BraviaTvConnectionError(
                f"gRPC channel to {self.host}:{self.port} not ready"
            ) from err
        self._channel = channel
        self._stub = ControlDeviceServiceStub(channel)
        try:
            self._authenticate(timeout=timeout)
        except BaseException:
            self.close()
            raise

    def _authenticate(self, timeout: float = 5.0) -> None:
        assert self._stub is not None
        signin = ConfirmSigninRequest()
        signin.auth_data = hashlib.sha256(self._device_id.encode()).digest()
        try:
            key = hmac_key_bytes(self._hmac_key)
        except ValueError as err:
            # An unusable stored key is a rejected key: fresh ones fix it.
            raise BraviaTvAuthError(f"stored key unusable: {err}") from err
        try:
            self._stub.ConfirmSignin(signin, timeout=timeout)
        except grpc.RpcError as err:
            # The first step involves no key. If it gets no answer at all,
            # the TV is not there to refuse anything, and fetching new keys
            # from Sony would not help.
            code = _code(err)
            if code in _UNREACHABLE:
                raise BraviaTvConnectionError(
                    f"handshake got no answer: {code}"
                ) from err
            raise BraviaTvAuthError(f"handshake failed: {code}") from err
        try:
            keys = ConfirmKeysRequest()
            keys.session_id = self._session_id
            keys.key_data = hmac.new(
                key, self._session_id.encode(), hashlib.sha256
            ).digest()
            self._stub.ConfirmKeys(keys, timeout=timeout)
            resp = self._stub.GetSessionRandom(
                GetSessionRandomRequest(session_id=self._session_id), timeout=timeout
            )
        except grpc.RpcError as err:
            # From here on the keys are in play: whatever the status (the TV
            # has been seen to refuse stale keys in more than one way), it is
            # treated as a refusal, which fresh keys answer.
            raise BraviaTvAuthError(f"handshake failed: {_code(err)}") from err
        self._session_random = resp.session_random
        if not self._session_random:
            raise BraviaTvAuthError("GetSessionRandom returned no session_random")

    _SESSION_REJECTED = (
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.PERMISSION_DENIED,
    )

    def _with_session_retry(self, attempt: Any, timeout: float) -> Any:
        """Run ``attempt``; if the TV rejects the session, re-authenticate in
        place and run it once more."""
        try:
            return attempt()
        except grpc.RpcError as err:
            if _code(err) not in self._SESSION_REJECTED:
                raise
            _LOGGER.info(
                "TV rejected the session (%s); re-authenticating in place", _code(err)
            )
            self._reauthenticate(timeout)
            return attempt()

    def _reauthenticate(self, timeout: float = 5.0) -> None:
        with self._auth_lock:
            if self._stub is None:
                raise BraviaTvConnectionError("not connected")
            self._authenticate(timeout=timeout)

    def rekey(
        self,
        hmac_key: str,
        key_id: str | None,
        session_key: str | None,
        timeout: float = 5.0,
    ) -> None:
        """Switch to freshly issued session keys and re-run the handshake.

        The TV learns a key a moment after Sony issues it (about two seconds,
        measured) and refuses it until then, while it goes on accepting the
        keys before. So when the handshake fails on an open channel the
        earlier keys are put back, the session carries on with them, and the
        caller tries again shortly."""
        with self._auth_lock:
            before = (self._hmac_key, self._session_key, self._session_id)
            self._hmac_key = hmac_key
            self._session_key = session_key
            if key_id:
                self._session_id = key_id
            if self._stub is not None:
                try:
                    self._authenticate(timeout=timeout)
                except BraviaTvError:
                    self._hmac_key, self._session_key, self._session_id = before
                    raise

    def close(self) -> None:
        """Stop the push stream and drop the channel. The schema is kept, so
        the client can connect again (same or another port)."""
        self.stop_notify()
        channel, self._channel, self._stub = self._channel, None, None
        self._session_random = None
        if channel is not None:
            channel.close()

    def _session(self) -> bytes:
        """The session random of the current handshake (it changes with every
        re-authentication, so it is read when a request is built)."""
        session_random = self._session_random
        if session_random is None:
            raise BraviaTvConnectionError("not connected")
        return session_random

    def _encryption_key(self) -> str:
        if not self._session_key:
            raise BraviaTvConnectionError("no session key for encrypted reads")
        return self._session_key

    def _transport(self, what: str, call: Callable[[], Any]) -> Any:
        """Run a call and translate gRPC's own error type, so callers never
        have to know about it: no answer is BraviaTvConnectionError, an error
        status from the TV is BraviaTvCallError."""
        try:
            return call()
        except grpc.RpcError as err:
            code = _code(err)
            if code in _UNREACHABLE:
                raise BraviaTvConnectionError(f"{what} failed: {code}") from err
            raise BraviaTvCallError(what, code) from err

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
        """Fetch and cache the device field-path schema (unauthenticated).

        Raises BraviaTvConnectionError when the call fails or the reply holds
        no usable schema: without one there is nothing to build entities from,
        and loading with none would look like a TV that has no controls."""
        if self._channel is None:
            raise BraviaTvConnectionError("not connected")
        call = self._raw_unary(get_capabilities_method())
        raw = self._transport("GetCapabilities", lambda: call(b"", timeout=timeout))
        cap_json = decode_capabilities_json_text(raw)
        try:
            index = self.load_capabilities(cap_json) if cap_json else {}
        except (TypeError, ValueError, RecursionError) as err:
            raise BraviaTvConnectionError(
                "the TV's capability schema could not be read"
            ) from err
        if not index:
            raise BraviaTvConnectionError("the TV sent no capability schema")
        return index

    def load_capabilities(self, cap_json: str) -> dict[str, CapabilityMeta]:
        """Use ``cap_json`` (the schema's JSON text) as the device schema.

        Called with the TV's reply, and with the stored copy of the last one
        when the TV is unreachable at start-up."""
        index = capability_index_from_json(cap_json)
        self._capabilities = index
        self._capabilities_json = cap_json if index else None
        # Precompute the paths safe for a single bulk GetStates read (excludes
        # write-only, nonce-gated, and command-independent paths that would make
        # the whole batch fail).
        self._safe_get_states_paths = paths_for_safe_get_states(cap_json)
        return index

    @property
    def capabilities(self) -> dict[str, CapabilityMeta]:
        return self._capabilities

    @property
    def capabilities_json(self) -> str | None:
        """The schema's JSON text as last served (or loaded), for storing."""
        return self._capabilities_json

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

        def attempt() -> dict[str, Any]:
            # Sign the body, then send exactly those bytes with the token.
            body = build_get_states_body(
                read_paths,
                session_random=self._session(),
                session_id=self._session_id,
            )
            token = sign_get_states_auth_token(self._hmac_key, body)
            call = self._raw_unary(_GET_STATES_METHOD)
            raw = call(wrap_signed_request(body, token), timeout=timeout)
            return parse_get_states_response(raw)

        return self._transport(
            "GetStates", lambda: self._with_session_retry(attempt, timeout)
        )

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
        with self._nonce_lock:
            return self._transport(
                "application list read",
                lambda: self._nonce_gated(self._read_application_list_once, timeout),
            )

    def _read_application_list_once(self, timeout: float) -> list[dict]:
        nonce = self._get_nonce(timeout)
        req = build_application_list_request(
            session_random=self._session(),
            session_id=self._session_id,
            hmac_key_hex=self._hmac_key,
            nonce=nonce,
        )
        call = self._raw_unary(_GET_STATES_METHOD)
        return parse_application_list_response(
            call(req, timeout=timeout), self._encryption_key()
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
        with self._nonce_lock:
            return self._transport(
                "resource read",
                lambda: self._nonce_gated(
                    lambda t: self._read_resource_once(uri, t), timeout
                ),
            )

    def _read_resource_once(self, uri: str, timeout: float) -> bytes:
        nonce = self._get_nonce(timeout)
        req = build_get_resource_request(
            uri=uri,
            session_random=self._session(),
            session_id=self._session_id,
            hmac_key_hex=self._hmac_key,
            nonce=nonce,
        )
        call = self._raw_unary(_GET_RESOURCES_METHOD)
        return parse_get_resource_response(
            call(req, timeout=timeout), self._encryption_key()
        )

    def _nonce_gated(self, read: Any, timeout: float) -> Any:
        """Run a nonce-gated read, retrying once with a fresh nonce when the TV
        answers INVALID_ARGUMENT (its reply to a stale or consumed nonce)."""
        try:
            return read(timeout)
        except grpc.RpcError as err:
            if _code(err) != grpc.StatusCode.INVALID_ARGUMENT:
                raise
            _LOGGER.debug("nonce-gated read rejected, retrying with a fresh nonce")
            return read(timeout)

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
        with self._write_lock:

            def attempt() -> bool:
                self._confirm_prerequisites(path, timeout)
                return self._exec(path, timeout, **self._value_kwargs(path, value))

            return self._transport(
                f"write to {path}", lambda: self._with_session_retry(attempt, timeout)
            )

    def _exec(self, path: str, timeout: float, **value_kwargs: Any) -> bool:
        token = sign_exec_auth_token(
            self._hmac_key,
            path,
            session_random=self._session(),
            session_id=self._session_id,
            **value_kwargs,
        )
        req = build_exec_command_with_auth_request(
            path,
            session_random=self._session(),
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
                    _code(err),
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
        # enum/string fields use field 6. A structured value goes as JSON.
        if cap_type == "any":
            if isinstance(value, dict | list):
                return {"any_value": json.dumps(value)}
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
        the TV going to standby, or rebooting onto a new dynamic port. The
        worker then ends; the caller reconnects and starts a new one.
        ``on_reconnect`` fires when the stream re-subscribes after a drop, so the
        caller can reconcile any state that changed while disconnected.
        """
        self.stop_notify()
        self._on_delta = on_delta
        self._on_connection_lost = on_connection_lost
        self._on_reconnect = on_reconnect
        # A stop flag per worker: an earlier worker that is slow to leave keeps
        # seeing its own (set) flag and can never be revived by a restart.
        self._notify_stop = stop = threading.Event()
        self._notify_thread = threading.Thread(
            target=self._notify_worker,
            args=(stop,),
            name="bravia_tv_notify",
            daemon=True,
        )
        self._notify_thread.start()

    def stop_notify(self) -> None:
        """Stop the push worker. Cancels the open stream first, so the worker
        leaves at once instead of waiting for the TV's next message."""
        self._notify_stop.set()
        call = self._notify_call
        if call is not None:
            try:
                call.cancel()
            except Exception:  # noqa: BLE001 - already finished or torn down
                _LOGGER.debug("cancelling the notify stream failed", exc_info=True)
        thread = self._notify_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        self._notify_thread = None

    def _notify_worker(self, stop: threading.Event) -> None:
        backoff = 1.0
        failures = 0
        had_drop = False
        while not stop.is_set():
            if self._stub is None or self._channel is None:
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
                self._notify_call = stream
                if stop.is_set():  # stop_notify ran before the stream existed
                    stream.cancel()
                    break
                for raw in stream:
                    if stop.is_set():
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
                    try:
                        path, value = parse_notify_message(raw)
                    except Exception:  # noqa: BLE001 - one bad message, not the stream
                        _LOGGER.debug(
                            "undecodable notify message (%d bytes)",
                            len(raw),
                            exc_info=True,
                        )
                        continue
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
                if stop.is_set():
                    break
                had_drop = True  # re-read the app list once we re-subscribe
                # Consecutive errors without an intervening delta mean the
                # connection can't be re-established (dead/changed port). Count
                # them straight up — no time-based reset, which previously let a
                # growing backoff space failures out and never reach threshold.
                failures += 1
                code = _code(err)
                _LOGGER.debug(
                    "notify stream ended (%s); reconnect attempt %d", code, failures
                )
                if code in self._SESSION_REJECTED:
                    # The TV forgot our session (keys lapsed or its control
                    # service restarted): re-handshake before re-subscribing
                    # instead of counting towards a lost connection.
                    try:
                        self._reauthenticate()
                        failures = 0
                        _LOGGER.info(
                            "re-authenticated after the TV rejected the session"
                        )
                    except Exception as reauth_err:  # noqa: BLE001
                        _LOGGER.debug("re-authentication failed: %s", reauth_err)
            except Exception:  # noqa: BLE001 - the push thread must not die silently
                if stop.is_set():
                    break
                had_drop = True
                failures += 1
                _LOGGER.exception(
                    "notify worker failed unexpectedly; attempt %d", failures
                )
            finally:
                self._notify_call = None
            if failures >= 3:
                _LOGGER.debug("notify connection lost after %d attempts", failures)
                if self._on_connection_lost is not None and not stop.is_set():
                    try:
                        self._on_connection_lost()
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("connection-lost handler failed")
                break
            # brief backoff before re-subscribing (capped low for fast detection)
            stop.wait(backoff)
            backoff = min(backoff * 2, 5.0)
