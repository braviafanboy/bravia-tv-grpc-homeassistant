"""The gRPC client serialises writes and nonce-gated reads (no HA needed)."""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("grpc")

from custom_components.bravia_tv_grpc.bravia_tv_client import (  # noqa: E402
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
