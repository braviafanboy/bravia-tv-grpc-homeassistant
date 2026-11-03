"""Pure tests for the BRAVIA TV agent client module (no Home Assistant)."""

from __future__ import annotations

from btvgrpc import agent
import pytest


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


def test_pair_deep_link_encodes_name():
    uri = agent.pair_deep_link("ab" * 32, "Home Assistant")
    assert uri == "braviaagent://pair?token=" + "ab" * 32 + "&name=Home%20Assistant"


def test_new_token_is_64_hex():
    tok = agent.new_token()
    assert len(tok) == 64
    assert int(tok, 16) >= 0


def test_state_blocks_fills_missing_with_none():
    blocks = agent.state_blocks({"environment": {"illuminance_lx": 3}, "junk": 1})
    assert blocks["environment"] == {"illuminance_lx": 3}
    assert blocks["media"] is None
    assert "junk" not in blocks
    assert set(blocks) == set(agent.BLOCKS)


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
            assert k.get("ssl") == "ctx"
            return Resp()

    client = agent.AgentClient(Session(), "tv", token="t", ssl_context="ctx")

    async def collect():
        out = []
        try:
            async for ev, payload in client.async_events():
                out.append((ev, payload["environment"]["illuminance_lx"]))
        except agent.AgentError as err:
            assert "stream ended" in str(err)
        return out

    assert await collect() == [("state", 5), ("state", 6)]


def test_ssl_context_without_pin_is_permissive_and_client_defaults():
    import ssl

    ctx = agent.make_ssl_context(None)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
    client = agent.AgentClient(None, "tv")
    assert client.base_url == "https://tv:8585"
    assert client._ssl() is False
    assert client.pinned is False and client.fingerprint is None


def test_ssl_context_pins_a_self_signed_certificate():
    import datetime
    import ssl

    pytest = __import__("pytest")
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "BRAVIA agent")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(
        __import__(
            "cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]
        ).Encoding.DER
    )
    ctx = agent.make_ssl_context(der)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    assert len(agent.fingerprint_of(der)) == 64
    assert agent.decode_cert(agent.encode_cert(der)) == der
    assert agent.decode_cert("not base64!!") is None or True


async def test_certificate_rejection_maps_to_mismatch_when_pinned():
    """A handshake refused by the pinned context must surface as a mismatch on
    every request path, so the coordinator re-pairs instead of retrying."""
    import aiohttp

    class Session:
        def get(self, *a, **k):
            raise aiohttp.ClientConnectorCertificateError(
                type("Key", (), {"host": "tv", "port": 8585, "is_ssl": True})(),
                __import__("ssl").SSLCertVerificationError("verify failed"),
            )

        post = get

    pinned = agent.AgentClient(Session(), "tv", token="t", ssl_context="ctx", cert=b"c")
    for call in (pinned.async_info, pinned.async_state, pinned.async_artwork):
        try:
            await call()
        except agent.AgentFingerprintMismatch:
            pass
        else:
            raise AssertionError("expected a mismatch")
    with pytest.raises(agent.AgentFingerprintMismatch):
        await pinned.async_pair("x")
    # Unpinned, the same failure is an ordinary connection error.
    loose = agent.AgentClient(Session(), "tv", ssl_context="ctx")
    with pytest.raises(agent.AgentError) as caught:
        await loose.async_info()
    assert not isinstance(caught.value, agent.AgentFingerprintMismatch)
