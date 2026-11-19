"""The gRPC client serialises writes and nonce-gated reads."""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("grpc")
pytest.importorskip("homeassistant")  # the client lives in the integration package

from custom_components.bravia_tv_grpc.bravia_tv_client import (  # noqa: E402
    BraviaTvAuthError,
    BraviaTvCallError,
    BraviaTvConnectionError,
    BraviaTvGrpcClient,
)


class _Overlap:
    """Counts how many fake RPCs are in flight at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.order: list[str] = []

    def __call__(self, req: bytes, timeout: float = 0) -> bytes:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.03)
        with self.lock:
            self.active -= 1
            self.order.append(req[:4].hex())
        return b"\x08\x01"  # parse_exec_response: field 1 = true


def _client(monkeypatch, probe: _Overlap) -> BraviaTvGrpcClient:
    client = BraviaTvGrpcClient("tv", 1, "dev", "ab" * 32, key_id="k")
    client._stub = object()
    client._channel = object()
    client._session_random = b"r" * 8
    client._capabilities = {}
    monkeypatch.setattr(client, "_raw_unary", lambda method: probe)
    monkeypatch.setattr(
        client, "_value_kwargs", lambda path, value: {"string_value": str(value)}
    )
    monkeypatch.setattr(
        "custom_components.bravia_tv_grpc.bravia_tv_client.sign_exec_auth_token",
        lambda *a, **k: b"t" * 32,
    )
    monkeypatch.setattr(
        "custom_components.bravia_tv_grpc.bravia_tv_client.build_exec_command_with_auth_request",
        lambda path, **k: path.encode(),
    )
    monkeypatch.setattr(
        "custom_components.bravia_tv_grpc.bravia_tv_client.parse_exec_response",
        lambda raw: True,
    )
    return client


def test_concurrent_writes_never_overlap(monkeypatch):
    probe = _Overlap()
    client = _client(monkeypatch, probe)
    threads = [
        threading.Thread(target=client.exec_command, args=("volume", i))
        for i in range(6)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert probe.peak == 1
    assert len(probe.order) == 6


class _RejectOnce:
    """First call: the TV rejects the session; afterwards it answers."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, req: bytes, timeout: float = 0) -> bytes:
        self.calls += 1
        if self.calls == 1:
            import grpc

            err = grpc.RpcError()
            err.code = lambda: grpc.StatusCode.INVALID_ARGUMENT  # type: ignore[attr-defined]
            raise err
        return b"\x08\x01"


def test_write_reauthenticates_and_retries_once(monkeypatch):
    probe = _RejectOnce()
    client = _client(monkeypatch, probe)
    reauths: list[int] = []
    monkeypatch.setattr(client, "_authenticate", lambda timeout=5.0: reauths.append(1))
    assert client.exec_command("volume", 3) is True
    assert probe.calls == 2
    assert reauths == [1]


def test_write_gives_up_after_second_rejection(monkeypatch):
    import grpc

    class AlwaysReject:
        def __call__(self, req: bytes, timeout: float = 0) -> bytes:
            err = grpc.RpcError()
            err.code = lambda: grpc.StatusCode.INVALID_ARGUMENT  # type: ignore[attr-defined]
            raise err

    client = _client(monkeypatch, AlwaysReject())
    monkeypatch.setattr(client, "_authenticate", lambda timeout=5.0: None)
    # The TV answered, with an error: reported as such, not as gRPC's own type.
    with pytest.raises(BraviaTvCallError) as caught:
        client.exec_command("volume", 3)
    assert caught.value.code is grpc.StatusCode.INVALID_ARGUMENT


def test_write_with_no_answer_is_a_connection_error(monkeypatch):
    import grpc

    class Unavailable:
        def __call__(self, req: bytes, timeout: float = 0) -> bytes:
            err = grpc.RpcError()
            err.code = lambda: grpc.StatusCode.UNAVAILABLE  # type: ignore[attr-defined]
            raise err

    client = _client(monkeypatch, Unavailable())
    with pytest.raises(BraviaTvConnectionError):
        client.exec_command("volume", 3)


def test_rekey_switches_keys_and_rehandshakes(monkeypatch):
    client = _client(monkeypatch, _Overlap())
    calls: list[str] = []
    monkeypatch.setattr(
        client, "_authenticate", lambda timeout=5.0: calls.append("auth")
    )
    client.rekey("cd" * 32, "newkey", "ef" * 32)
    assert client._session_id == "newkey"
    assert client._hmac_key == "cd" * 32
    assert calls == ["auth"]


def test_rekey_keeps_the_old_keys_when_the_tv_refuses_the_new(monkeypatch):
    """The TV refuses a key Sony has only just issued, and goes on accepting
    the ones before: the session must carry on with those."""
    client = _client(monkeypatch, _Overlap())
    before = (client._hmac_key, client._session_key, client._session_id)
    random_before = client._session_random

    def refused(timeout=5.0):
        raise BraviaTvAuthError("handshake failed: INVALID_ARGUMENT")

    monkeypatch.setattr(client, "_authenticate", refused)
    with pytest.raises(BraviaTvAuthError):
        client.rekey("cd" * 32, "newkey", "ef" * 32)
    assert (client._hmac_key, client._session_key, client._session_id) == before
    assert client._session_random == random_before
