"""A fake TV on loopback: enough of the TV's gRPC service for the real client
(and anything built on it) to run against, checking every signature it is
sent.

It keeps no app list (the encrypted read is tested on its own), so a client
sees a TV with no apps and no inputs.
"""

from __future__ import annotations

from concurrent import futures
import hashlib
import hmac
import json
from pathlib import Path
import queue
import threading

from btvgrpc.wire import parse_fields  # the pure package set up in conftest
import grpc
from pb_helpers import ld, vint

SERVICE = "/jp.co.sony.hes.ssh.controldevice.v1.ControlDeviceService/"
CAPS_TEXT = json.dumps(
    json.loads(
        (
            Path(__file__).resolve().parents[1] / "schema" / "grpc_capabilities.json"
        ).read_text()
    )
)
KEY = "ab" * 32
_STOP = object()


def state_reply(states: dict) -> bytes:
    entries = b""
    for path, value in states.items():
        if isinstance(value, bool):
            field = ld(3, vint(1, 1) if value else b"")
        elif isinstance(value, int):
            field = ld(2, vint(1, value))
        else:
            field = ld(4, ld(1, str(value).encode()))
        entries += ld(1, ld(1, path.encode()) + field)
    return ld(2, ld(1, entries) + ld(2, b"\x00" * 32) + ld(3, b"k"))


class FakeTvServer(grpc.GenericRpcHandler):
    """Speaks just enough of the TV's service, and checks every signature."""

    def __init__(self) -> None:
        self.key = bytes.fromhex(KEY)
        self.session_random = bytes(range(64))
        self.states = {"power": True, "volume": 18}
        self.writes: list[bytes] = []
        self.handshakes = 0
        self.handshake_error: grpc.StatusCode | None = None
        self.keys_error: grpc.StatusCode | None = None
        self.reject_next_read = False
        self.read_error: grpc.StatusCode | None = None
        self.pushes: queue.Queue = queue.Queue()
        self.subscriptions = 0
        self.stream_error: grpc.StatusCode | None = None
        self._pool = futures.ThreadPoolExecutor(max_workers=8)
        self._server = grpc.server(self._pool)
        self._server.add_generic_rpc_handlers((self,))
        self.port = self._server.add_insecure_port("127.0.0.1:0")
        before = set(threading.enumerate())
        self._server.start()
        # grpc's own serving thread(s), so stop() can wait them out: the test
        # harness fails a test that leaves a thread behind.
        self._threads = [t for t in threading.enumerate() if t not in before]
        self._stopped = False

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.pushes.put(_STOP)
        self._server.stop(0).wait(5)
        self._pool.shutdown(wait=True)
        for thread in self._threads:
            thread.join(5)

    # -- dispatch ----------------------------------------------------------
    def service(self, details):
        name = details.method.removeprefix(SERVICE)
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return None
        if name == "StartNotifyStates":
            return grpc.unary_stream_rpc_method_handler(handler)
        return grpc.unary_unary_rpc_method_handler(handler)

    def _ConfirmSignin(self, request, context):
        if self.handshake_error is not None:
            context.abort(self.handshake_error, "no")
        return b"\x08\x01"

    def _ConfirmKeys(self, request, context):
        if self.keys_error is not None:
            context.abort(self.keys_error, "no")
        fields = parse_fields(request)
        session_id, proof = fields[1][0], fields[2][0]
        expected = hmac.new(self.key, session_id, hashlib.sha256).digest()
        if proof != expected:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bad key")
        return b""

    def _GetSessionRandom(self, request, context):
        self.handshakes += 1
        return ld(1, self.session_random)

    def _GetCapabilities(self, request, context):
        return ld(1, ld(1, CAPS_TEXT.encode()))

    def _signed_body(self, request, context) -> bytes:
        """Field 1 of a signed request, after checking the HMAC in field 2
        covers exactly those bytes."""
        fields = parse_fields(request)
        body, token = fields[1][0], fields[2][0]
        if token != hmac.new(self.key, body, hashlib.sha256).digest():
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bad signature")
        if self.session_random not in body:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "stale session")
        return body

    def _GetStatesWithAuth(self, request, context):
        if self.reject_next_read:
            self.reject_next_read = False
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "session forgotten")
        if self.read_error is not None:
            context.abort(self.read_error, "no")
        body = self._signed_body(request, context)
        wanted = {p: v for p, v in self.states.items() if p.encode() in body}
        return state_reply(wanted)

    def _ExecCommandWithAuth(self, request, context):
        outer = parse_fields(request)[1][0]
        self.writes.append(self._signed_body(outer, context))
        return b"\x08\x01"

    def _StartNotifyStates(self, request, context):
        self.subscriptions += 1
        if self.stream_error is not None:
            context.abort(self.stream_error, "no stream")
        while context.is_active():
            try:
                item = self.pushes.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is _STOP:
                return
            yield item
