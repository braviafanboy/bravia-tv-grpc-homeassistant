"""The agent client against a real TLS server on loopback: reading the
certificate, pinning it, and refusing any other."""

from __future__ import annotations

import datetime
import hashlib
import ssl

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")
x509 = pytest.importorskip("cryptography.x509")

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from custom_components.bravia_tv_grpc.grpc import agent  # noqa: E402


def _certificate(tmp_path, name: str, *, expired: bool = False):
    """A self-signed certificate like the agent's; returns (ssl context, DER)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "HA TV Agent")])
    now = datetime.datetime.now(datetime.UTC)
    start = now - datetime.timedelta(days=30)
    end = (
        now - datetime.timedelta(days=1)
        if expired
        else now + datetime.timedelta(days=30)
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / f"{name}.crt"
    key_file = tmp_path / f"{name}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx, cert.public_bytes(serialization.Encoding.DER)


class _Agent:
    """A minimal agent: /v1/info, /v1/state, /v1/pair; records what it saw.

    It pairs as the agent does from 0.10: a request with a commitment, the
    secret revealed (which gives it a code), allowed or denied by the viewer
    (``accepted``, ``denied``), then collected. ``legacy`` makes it the older
    kind, which hands the token to whoever asks while its window is open."""

    NONCE = bytes(range(32, 64))

    def __init__(self) -> None:
        self.authorizations: dict[str, str | None] = {}
        self.runner: web.AppRunner | None = None
        self.port = 0
        self.pending: str | None = None
        self.commit: str | None = None
        self.revealed: bytes | None = None
        self.accepted = False
        self.denied = False
        self.legacy = False
        self.window_open = False

    async def start(self, ctx: ssl.SSLContext) -> None:
        app = web.Application()
        app.router.add_get("/v1/info", self._info)
        app.router.add_get("/v1/state", self._state)
        app.router.add_post("/v1/pair", self._pair)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    def _seen(self, request: web.Request) -> None:
        self.authorizations[request.path] = request.headers.get("Authorization")

    async def _info(self, request: web.Request) -> web.Response:
        self._seen(request)
        return web.json_response({"agent": {"version": "0.9.1", "contract": 1}})

    async def _state(self, request: web.Request) -> web.Response:
        self._seen(request)
        return web.json_response({"environment": {"illuminance_lx": 3}})

    async def _pair(self, request: web.Request) -> web.Response:
        self._seen(request)
        if self.legacy:
            if self.window_open:
                return web.json_response({"token": "tok"})
            return web.json_response({"error": "pairing_closed"}, status=403)
        asked = request.query.get("request")
        if asked is None:
            commit = request.query.get("commit") or ""
            if len(commit) != 64:
                return web.json_response({"error": "commit_required"}, status=400)
            if self.pending is not None:
                return web.json_response(
                    {"error": "pairing_busy", "retry_after": 21}, status=409
                )
            self.pending = "0123456789abcdef0123456789abcdef"
            self.commit = commit
            return web.json_response(
                {
                    "request": self.pending,
                    "nonce": self.NONCE.hex(),
                    "accepted": False,
                    "expires_in": 30,
                },
                status=202,
            )
        if asked != self.pending:
            return web.json_response({"error": "pairing_closed"}, status=403)
        waiting = {"request": asked, "accepted": self.accepted, "expires_in": 12}
        reveal = request.query.get("reveal")
        if reveal is not None:
            secret = bytes.fromhex(reveal)
            if hashlib.sha256(secret).hexdigest() != self.commit:
                self.pending = None
                return web.json_response({"error": "pairing_closed"}, status=403)
            self.revealed = secret
            return web.json_response(waiting, status=202)
        if self.denied:
            return web.json_response({"error": "pairing_denied"}, status=403)
        if not self.accepted:
            return web.json_response(waiting, status=202)
        self.pending = None
        return web.json_response({"token": "tok"})


@pytest.fixture
async def server(socket_enabled, tmp_path):
    ctx, der = _certificate(tmp_path, "agent")
    fake = _Agent()
    await fake.start(ctx)
    yield fake, der
    await fake.stop()


async def test_reads_the_certificate_then_pins_it_for_pairing(server):
    """The whole first contact: the certificate is read off the handshake
    (also for the small replies whose connection aiohttp has already let go
    of), pinned, and pairing then runs over the pinned connection."""
    fake, der = server
    async with aiohttp.ClientSession() as session:
        client = agent.AgentClient(session, "127.0.0.1", fake.port)
        fingerprint = await client.async_fetch_fingerprint()
        assert fingerprint == agent.fingerprint_of(der)
        # Reading it sent no request at all.
        assert fake.authorizations == {}
        client.pin(fingerprint)
        # Ask, with a commitment to a secret; the agent answers with its own.
        asked = await client.async_pair_request("Home Assistant")
        assert isinstance(asked, agent.AgentPairRequest)
        assert asked.request_id == fake.pending and asked.expires_in == 30
        assert asked.nonce == fake.NONCE and len(asked.secret) == 32
        assert fake.commit == hashlib.sha256(asked.secret).hexdigest()
        assert fake.revealed is None  # the secret itself has not left yet
        assert await client.async_pair_collect(asked.request_id) is None
        # Reveal it: both sides now have the code, tied to the certificate
        # the client pinned, which is the one the agent serves.
        code = await client.async_pair_reveal(asked)
        assert fake.revealed == asked.secret
        assert code == agent.pairing_code(fake.revealed, fake.NONCE, fingerprint)
        assert len(code) == 6 and code.isdigit()
        assert await client.async_pair_collect(asked.request_id) is None
        assert client.token is None
        # While it waits, a second request is turned away.
        with pytest.raises(agent.AgentPairingBusy) as busy:
            await client.async_pair_request("Home Assistant")
        assert busy.value.retry_after == 21
        # The viewer allows it on the TV: now the token can be collected, once.
        fake.accepted = True
        assert await client.async_pair_collect(asked.request_id) == "tok"
        assert client.token == "tok"
        with pytest.raises(agent.AgentPairingClosed):
            await client.async_pair_collect(asked.request_id)
        # Pairing itself never carries a token.
        assert fake.authorizations["/v1/pair"] is None
        assert (await client.async_state())["environment"]["illuminance_lx"] == 3
        assert fake.authorizations["/v1/state"] == "Bearer tok"
        # /v1/info answers without the token; over the pinned connection it
        # is sent, for the parts the agent keeps from strangers.
        await client.async_info()
        assert fake.authorizations["/v1/info"] == "Bearer tok"


async def test_a_denied_request_says_so_and_an_unpinned_client_shows_no_code(server):
    fake, der = server
    async with aiohttp.ClientSession() as session:
        client = agent.AgentClient(session, "127.0.0.1", fake.port)
        asked = await client.async_pair_request("Home Assistant")
        assert isinstance(asked, agent.AgentPairRequest)
        # A code computed from a certificate nobody checked proves nothing,
        # so without a pin there is none, and the secret stays here.
        with pytest.raises(agent.AgentError):
            await client.async_pair_reveal(asked)
        assert fake.revealed is None
        client.pin(agent.fingerprint_of(der))
        await client.async_pair_reveal(asked)
        fake.denied = True
        with pytest.raises(agent.AgentPairingDenied):
            await client.async_pair_collect(asked.request_id)
        assert client.token is None


async def test_info_carries_no_token_before_the_certificate_is_pinned(server):
    fake, _der = server
    async with aiohttp.ClientSession() as session:
        client = agent.AgentClient(session, "127.0.0.1", fake.port, token="secret")
        await client.async_info()
        assert fake.authorizations["/v1/info"] is None


async def test_pairs_with_an_agent_from_before_requests(server):
    """An older agent has a window instead: 403 while it is shut, the token
    at once while it is open."""
    fake, der = server
    fake.legacy = True
    async with aiohttp.ClientSession() as session:
        client = agent.AgentClient(
            session, "127.0.0.1", fake.port, fingerprint=agent.fingerprint_of(der)
        )
        with pytest.raises(agent.AgentPairingClosed):
            await client.async_pair_request("Home Assistant")
        fake.window_open = True
        assert await client.async_pair_request("Home Assistant") == "tok"
        assert client.token == "tok"


async def test_another_certificate_is_refused_before_any_request(server):
    fake, der = server
    async with aiohttp.ClientSession() as session:
        client = agent.AgentClient(
            session, "127.0.0.1", fake.port, token="secret", fingerprint="ab" * 32
        )
        for call in (client.async_info, client.async_state):
            with pytest.raises(agent.AgentFingerprintMismatch) as caught:
                await call()
            # It names the certificate that was presented instead.
            assert caught.value.got == agent.fingerprint_of(der)
        with pytest.raises(agent.AgentFingerprintMismatch):
            await client.async_pair_request("Home Assistant")
        with pytest.raises(agent.AgentFingerprintMismatch):
            await client.async_pair_collect("0123456789abcdef0123456789abcdef")
    # The handshake was refused: the token never left.
    assert fake.authorizations == {}


async def test_pin_is_the_certificate_not_its_dates(socket_enabled, tmp_path):
    """The pin is the exact certificate. One that has run out (or a clock that
    is off) is still the agent's own, not a changed identity."""
    ctx, der = _certificate(tmp_path, "expired", expired=True)
    fake = _Agent()
    await fake.start(ctx)
    try:
        async with aiohttp.ClientSession() as session:
            client = agent.AgentClient(
                session, "127.0.0.1", fake.port, fingerprint=agent.fingerprint_of(der)
            )
            assert (await client.async_info())["agent"]["contract"] == 1
    finally:
        await fake.stop()


async def test_no_agent_is_a_plain_agent_error(socket_enabled, unused_tcp_port):
    async with aiohttp.ClientSession() as session:
        client = agent.AgentClient(session, "127.0.0.1", unused_tcp_port)
        with pytest.raises(agent.AgentError) as caught:
            await client.async_fetch_fingerprint()
        assert not isinstance(caught.value, agent.AgentFingerprintMismatch)
