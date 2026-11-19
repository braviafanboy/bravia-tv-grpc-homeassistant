"""Pure tests for the HA TV Agent client module (no Home Assistant)."""

from __future__ import annotations

import importlib.util

from btvgrpc import agent
import pytest

# The async tests need pytest-asyncio, which comes with the Home Assistant
# test package; without it (the pure tier) they are skipped.
needs_asyncio = pytest.mark.skipif(
    importlib.util.find_spec("pytest_asyncio") is None,
    reason="pytest-asyncio is not installed",
)


def test_parse_sse_basic_events():
    text = (
        "event: state\n"
        'data: {"a": 1}\n'
        "\n"
        ": comment\n"
        "event: heartbeat\n"
        "data: {}\n"
        "\n"
        "data: line1\n"
        "data: line2\n"
        "\n"
    )
    events = list(agent.parse_sse(text.splitlines(keepends=True)))
    assert events == [
        ("state", '{"a": 1}'),
        ("heartbeat", "{}"),
        ("message", "line1\nline2"),
    ]


def test_parse_sse_trailing_event_without_blank_line():
    events = list(agent.parse_sse(["event: state\n", "data: {}\n"]))
    assert events == [("state", "{}")]


def test_parse_sse_crlf_and_no_space_after_colon():
    events = list(agent.parse_sse(["event:state\r\n", 'data:{"x":2}\r\n', "\r\n"]))
    assert events == [("state", '{"x":2}')]


def test_state_blocks_fills_missing_with_none():
    blocks = agent.state_blocks({"environment": {"illuminance_lx": 3}, "junk": 1})
    assert blocks["environment"] == {"illuminance_lx": 3}
    assert blocks["media"] is None
    assert "junk" not in blocks
    assert set(blocks) == set(agent.BLOCKS)


@needs_asyncio
async def test_async_events_assembles_line_delivered_frames():
    """aiohttp yields the body line by line; events must be assembled across lines."""

    lines = [
        b"event: state\n",
        b'data: {"environment": {"illuminance_lx": 5}}\n',
        b"\n",
        b"event: heartbeat\n",
        b"data: {}\n",
        b"\n",
        b"event: state\n",
        b'data: {"environment": {"illuminance_lx": 6}}\n',
        b"\n",
    ]

    class Content:
        def __init__(self):
            self._it = iter(lines)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

    class Resp:
        status = 200

        def __init__(self):
            self.content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def get(self, *a, **k):
            # Every request must carry the client's TLS choice explicitly;
            # aiohttp's default would verify against the system CAs.
            assert k.get("ssl") is client._ssl()
            assert k["ssl"] is not None
            return Resp()

    client = agent.AgentClient(Session(), "tv", token="t", fingerprint="ab" * 32)

    async def collect():
        out = []
        try:
            async for ev, payload in client.async_events():
                out.append((ev, payload["environment"]["illuminance_lx"]))
        except agent.AgentError as err:
            assert "stream ended" in str(err)
        return out

    assert await collect() == [("state", 5), ("state", 6)]


@needs_asyncio
async def test_oversized_event_line_is_an_agent_error():
    """aiohttp raises a bare ValueError for a line beyond its buffer; the
    caller must see an AgentError (which it retries), not a stray exception."""

    class Content:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ValueError("Chunk too big")

    class Resp:
        status = 200
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def get(self, *a, **k):
            return Resp()

    client = agent.AgentClient(Session(), "tv", token="t")
    with pytest.raises(agent.AgentError) as caught:
        async for _ in client.async_events():
            pass
    assert "Chunk too big" in str(caught.value)


@needs_asyncio
async def test_an_update_without_checksum_or_version_is_never_sent():
    """The agent installs an update only after checking the download against
    its SHA-256; a release manifest that lacks one must not get that far."""

    class Session:
        def post(self, *a, **k):
            raise AssertionError("nothing may be sent")

    client = agent.AgentClient(Session(), "tv", token="t", fingerprint="ab" * 32)
    for sha256, version in (("", "1.0"), ("ab" * 32, ""), (None, None)):
        with pytest.raises(agent.AgentError, match="checksum or version"):
            await client.async_stage_update("https://x/a.apk", sha256, version)


def test_pairing_code_on_fixed_values():
    """The code both sides of a pairing show. The agent's tests hold the same
    three values (PairingTest.codes), so the two cannot drift apart."""
    run = lambda first: bytes((first + i) % 256 for i in range(32))  # noqa: E731
    assert agent.pairing_code(run(0), run(32), run(64).hex()) == "576869"
    assert agent.pairing_code(bytes(32), bytes(32), "00" * 32) == "685108"
    # A leading zero is kept: always six digits.
    assert agent.pairing_code(b"\x01" * 32, bytes(32), "00" * 32) == "023151"
    # It is tied to all three: another certificate, secret or agent secret
    # gives another code.
    base = agent.pairing_code(run(0), run(32), run(64).hex())
    assert agent.pairing_code(run(0), run(32), run(96).hex()) != base
    assert agent.pairing_code(run(1), run(32), run(64).hex()) != base
    assert agent.pairing_code(run(0), run(33), run(64).hex()) != base


def test_client_defaults_are_unpinned():
    client = agent.AgentClient(None, "tv")
    assert client.base_url == "https://tv:8585"
    # No pin yet: nothing to verify a self-signed certificate against.
    assert client._ssl() is False
    assert client.pinned is False and client.fingerprint is None


def test_pin_uses_the_certificate_fingerprint():
    import aiohttp

    der = b"any certificate bytes"
    fingerprint = agent.fingerprint_of(der)
    assert len(fingerprint) == 64
    client = agent.AgentClient(None, "tv", fingerprint=fingerprint.upper())
    assert client.pinned and client.fingerprint == fingerprint
    pin = client._ssl()
    assert isinstance(pin, aiohttp.Fingerprint)
    assert pin.fingerprint == bytes.fromhex(fingerprint)
    # One object for the client's lifetime, so pooled connections are shared.
    assert client._ssl() is pin
    client.pin(None)
    assert client._ssl() is False and client.fingerprint is None
    for bad in ("", "abcd", "zz" * 32, "ab" * 31):
        with pytest.raises(ValueError):
            client.pin(bad)


def test_fingerprint_helpers():
    import base64

    der = b"\x30\x82 certificate"
    assert agent.decode_cert(base64.b64encode(der).decode()) == der
    assert agent.decode_cert("not base64!!") is None
    assert agent.decode_cert(None) is None
    fp = agent.fingerprint_of(der)
    assert agent.normalize_fingerprint(fp.upper()) == fp
    assert (
        agent.normalize_fingerprint(":".join(fp[i : i + 2] for i in range(0, 64, 2)))
        == fp
    )
    for bad in (None, 5, "", "ab" * 31, "xy" * 32):
        assert agent.normalize_fingerprint(bad) is None


def test_contract_of():
    assert agent.contract_of({"agent": {"contract": 2}}) == 2
    assert agent.contract_of({"agent": {"contract": "2"}}) is None
    assert agent.contract_of({"agent": {"contract": True}}) is None
    assert agent.contract_of({}) is None


@needs_asyncio
async def test_other_certificate_maps_to_mismatch_on_every_request_path():
    """A peer presenting another certificate than the pinned one must surface
    as a mismatch on every request path, carrying what was presented."""
    import aiohttp

    got = bytes(range(32))

    class Session:
        def get(self, *a, **k):
            raise aiohttp.ServerFingerprintMismatch(b"\x00" * 32, got, "tv", 8585)

        post = get
        request = get

    pinned = agent.AgentClient(Session(), "tv", token="t", fingerprint="ab" * 32)
    for call in (
        pinned.async_info,
        pinned.async_state,
        pinned.async_artwork,
        pinned.async_picture_refresh,
        lambda: pinned.async_pair_request("x"),
        lambda: pinned.async_pair_collect("r"),
        lambda: pinned.async_stage_update("u", "ab" * 32, "1.0"),
        pinned.async_arm_setup,
    ):
        with pytest.raises(agent.AgentFingerprintMismatch) as caught:
            await call()
        assert caught.value.got == got.hex()
    with pytest.raises(agent.AgentFingerprintMismatch):
        async for _ in pinned.async_events():
            pass

    # Any other connection failure is an ordinary agent error.
    class Down:
        def get(self, *a, **k):
            raise aiohttp.ClientConnectionError("refused")

    loose = agent.AgentClient(Down(), "tv")
    with pytest.raises(agent.AgentError) as caught:
        await loose.async_info()
    assert not isinstance(caught.value, agent.AgentFingerprintMismatch)


def test_missing_permissions_lists_only_denied_known_grants():
    info = {
        "permissions": {
            "notification_access": False,
            "bluetooth_connect": True,
            "usage_access": False,
        }
    }
    assert agent.missing_permissions(info) == ["notification_access"]
    assert agent.missing_permissions({}) == []
    assert (
        agent.missing_permissions(
            {"permissions": {"notification_access": True, "bluetooth_connect": True}}
        )
        == []
    )
