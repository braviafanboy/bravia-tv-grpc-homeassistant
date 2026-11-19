"""The real gRPC client against a fake TV on loopback: the handshake, signed
calls, the push thread, and what each kind of failure turns into."""

from __future__ import annotations

import threading
import time

import pytest

grpc = pytest.importorskip("grpc")
pytest.importorskip("homeassistant")  # the client lives in the integration package

from fake_tv_server import CAPS_TEXT, KEY, FakeTvServer  # noqa: E402
from pb_helpers import ld, vint  # noqa: E402

from custom_components.bravia_tv_grpc import bravia_tv_client as client_mod  # noqa: E402
from custom_components.bravia_tv_grpc.bravia_tv_client import (  # noqa: E402
    BraviaTvAuthError,
    BraviaTvCallError,
    BraviaTvConnectionError,
    BraviaTvGrpcClient,
)


@pytest.fixture
def tv():
    server = FakeTvServer()
    yield server
    server.stop()


@pytest.fixture
def client(tv):
    c = BraviaTvGrpcClient("127.0.0.1", tv.port, "device-1", KEY, key_id="key-1")
    yield c
    c.close()


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def test_handshake_schema_read_and_write(tv, client):
    client.connect()
    assert client.connected and tv.handshakes == 1
    caps = client.get_capabilities()
    assert len(caps) == 118 and caps["power"].type == "bool"
    assert client.capabilities_json == CAPS_TEXT
    # The fake checks the HMAC over the exact bytes it received.
    assert client.get_states() == {"power": True, "volume": 18}
    assert client.get_states(["volume"]) == {"volume": 18}
    assert client.exec_command("volume", 21) is True
    assert b"volume" in tv.writes[-1]


def test_wrong_key_is_an_auth_error_and_leaves_nothing_open(tv):
    c = BraviaTvGrpcClient("127.0.0.1", tv.port, "device-1", "cd" * 32, key_id="k")
    with pytest.raises(BraviaTvAuthError):
        c.connect()
    assert not c.connected and c._channel is None
    # An unusable stored key counts the same: fresh keys are what fixes it.
    bad = BraviaTvGrpcClient("127.0.0.1", tv.port, "device-1", "", key_id="k")
    with pytest.raises(BraviaTvAuthError):
        bad.connect()
    assert bad._channel is None


def test_nothing_listening_is_a_connection_error(tv):
    port = tv.port
    tv.stop()
    c = BraviaTvGrpcClient("127.0.0.1", port, "device-1", KEY, key_id="k")
    with pytest.raises(BraviaTvConnectionError):
        c.connect(timeout=0.5)
    assert c._channel is None


def test_handshake_without_an_answer_is_not_an_auth_error(tv, client):
    """No answer to the first, key-less step says the TV is not there, not
    that it refused the keys: fetching new keys from Sony would not help."""
    tv.handshake_error = grpc.StatusCode.UNAVAILABLE
    with pytest.raises(BraviaTvConnectionError):
        client.connect()
    tv.handshake_error = grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(BraviaTvAuthError):
        client.connect()


@pytest.mark.parametrize(
    "status",
    [
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.UNKNOWN,
    ],
)
def test_any_failure_once_the_keys_are_in_play_asks_for_fresh_keys(tv, client, status):
    """However the TV says no to the key step (it need not be a tidy
    "unauthenticated"), the answer is the same: new keys."""
    tv.keys_error = status
    with pytest.raises(BraviaTvAuthError):
        client.connect()
    assert not client.connected


def test_forgotten_session_is_renewed_in_place(tv, client):
    client.connect()
    tv.reject_next_read = True
    assert client.get_states(["power"]) == {"power": True}
    assert tv.handshakes == 2


def test_calls_report_their_failure_without_grpc_types(tv, client):
    client.connect()
    client.get_capabilities()
    # The TV answers, with an error status that is not about the session.
    tv.read_error = grpc.StatusCode.FAILED_PRECONDITION
    with pytest.raises(BraviaTvCallError) as caught:
        client.get_states(["power"])
    assert caught.value.code is grpc.StatusCode.FAILED_PRECONDITION
    tv.read_error = None
    # The TV no longer accepts our keys: the in-place renewal fails too.
    tv.key = b"\x01" * 32
    with pytest.raises(BraviaTvAuthError):
        client.get_states(["power"])
    client.close()
    for call in (
        lambda: client.get_states(["power"]),
        lambda: client.exec_command("volume", 1),
        client.get_capabilities,
    ):
        with pytest.raises(BraviaTvConnectionError):
            call()


def test_reconnects_on_the_same_client(tv, client):
    client.connect()
    client.get_capabilities()
    client.close()
    assert not client.connected
    assert client.capabilities  # the schema outlives the connection
    client.connect()
    assert client.get_states(["volume"]) == {"volume": 18}
    assert tv.handshakes == 2


def test_push_delivers_and_stops_at_once(tv, client):
    client.connect()
    seen: list = []
    client.start_notify(lambda path, value: seen.append((path, value)))
    _wait(lambda: tv.subscriptions == 1)
    tv.pushes.put(ld(2, ld(1, ld(1, ld(1, b"volume") + ld(2, vint(1, 30))))))
    _wait(lambda: seen == [("volume", 30)])
    worker = client._notify_thread
    time.sleep(0.2)  # the stream is now idle, waiting for the TV
    start = time.monotonic()
    client.close()
    # It used to wait out a 2 s join on every unload: the idle stream was only
    # cut after the wait.
    assert time.monotonic() - start < 1.0
    assert not worker.is_alive()


def test_one_undecodable_push_does_not_end_the_stream(tv, client, monkeypatch):
    client.connect()
    seen: list = []
    real = client_mod.parse_notify_message

    def flaky(raw):
        if raw == b"boom":
            raise RuntimeError("decoder bug")
        return real(raw)

    monkeypatch.setattr(client_mod, "parse_notify_message", flaky)
    client.start_notify(lambda path, value: seen.append((path, value)))
    tv.pushes.put(b"boom")
    tv.pushes.put(ld(2, ld(1, ld(1, ld(1, b"mute") + ld(3, b"")))))
    _wait(lambda: seen == [("mute", False)])
    assert client._notify_thread.is_alive()
    assert tv.subscriptions == 1


def test_lost_stream_is_reported_once_and_the_worker_ends(tv, client, monkeypatch):
    client.connect()
    monkeypatch.setattr(
        threading.Event, "wait", lambda self, timeout=None: self.is_set()
    )
    lost: list[int] = []
    tv.stream_error = grpc.StatusCode.UNAVAILABLE
    client.start_notify(
        lambda path, value: None, on_connection_lost=lambda: lost.append(1)
    )
    _wait(lambda: lost == [1])
    worker = client._notify_thread
    worker.join(2)
    assert not worker.is_alive()
    assert tv.subscriptions == 3  # three attempts, then it gives up


def test_unexpected_worker_error_is_reported_not_swallowed(tv, client, monkeypatch):
    """Anything but a gRPC error used to end the thread without a word: state
    then went stale with the connection still looking healthy."""
    client.connect()
    monkeypatch.setattr(
        threading.Event, "wait", lambda self, timeout=None: self.is_set()
    )
    lost: list[int] = []

    def broken(method):
        raise RuntimeError("stub exploded")

    monkeypatch.setattr(client, "_raw_stream", broken)
    client.start_notify(
        lambda path, value: None, on_connection_lost=lambda: lost.append(1)
    )
    _wait(lambda: lost == [1])


def test_rejected_stream_session_is_renewed_and_resubscribed(tv, client, monkeypatch):
    client.connect()
    monkeypatch.setattr(
        threading.Event, "wait", lambda self, timeout=None: self.is_set()
    )
    seen: list = []
    reconnects: list[int] = []
    tv.stream_error = grpc.StatusCode.UNAUTHENTICATED

    def renewed_handshake(request, context, original=tv._GetSessionRandom):
        tv.stream_error = None  # after the new handshake the stream is accepted
        return original(request, context)

    tv._GetSessionRandom = renewed_handshake
    client.start_notify(
        lambda path, value: seen.append((path, value)),
        on_reconnect=lambda: reconnects.append(1),
    )
    tv.pushes.put(ld(2, ld(1, ld(1, ld(1, b"volume") + ld(2, vint(1, 5))))))
    _wait(lambda: seen == [("volume", 5)])
    assert tv.handshakes == 2
    assert reconnects == [1]  # state is re-read after the gap
