"""Client for the BRAVIA TV agent (the companion app running on the TV).

Pure protocol module (aiohttp only, no Home Assistant): the agent serves a JSON
document over plain HTTP on the LAN (default port 8585), advertises itself with
mDNS ``_bravia-agent._tcp`` and streams changes as Server-Sent Events. See
https://github.com/braviafanboy/bravia-tv-agent for the contract.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable, Iterable
import hashlib
import json
import secrets
import ssl
from typing import Any

import aiohttp

AGENT_PORT = 8585
AGENT_PACKAGE = "com.braviafanboy.braviaagent"
CONTRACT = 1
# Blocks of the state document the integration understands.
BLOCKS = (
    "environment",
    "display",
    "media",
    "inputs",
    "audio",
    "bluetooth",
    "wifi",
    "settings",
)
_INFO_TIMEOUT = 4
_REQUEST_TIMEOUT = 8
# The agent sends a heartbeat every 30 s; treat a longer silence as a dead link.
EVENTS_IDLE_TIMEOUT = 75


class AgentError(Exception):
    """The agent could not be reached or answered with an error."""


class AgentUnauthorized(AgentError):
    """The agent rejected our token (401): re-pairing is needed."""


class AgentPairingClosed(AgentError):
    """POST /v1/pair outside the pairing window (403)."""


class AgentFingerprintMismatch(AgentError):
    """The agent presented a certificate other than the pinned one."""


def new_token() -> str:
    """A fresh bearer token for cloud delivery (64 hex chars, like the agent's)."""
    return secrets.token_hex(32)


def pair_deep_link(token: str, name: str) -> str:
    """The ``braviaagent://pair`` URI that makes the agent adopt ``token``."""
    return (
        f"braviaagent://pair?token={token}&name={aiohttp.helpers.quote(name, safe='')}"
    )


SETUP_DEEP_LINK = "braviaagent://setup"
UPDATE_DEEP_LINK = "braviaagent://update"


async def async_fetch_manifest(
    session: aiohttp.ClientSession, url: str
) -> dict[str, Any]:
    """Read a release manifest: ``{"version", "url", "sha256", "notes"}``."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise AgentError(f"manifest: HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as err:
        raise AgentError(f"manifest: {err}") from err
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("version"), str)
        or not data.get("url")
    ):
        raise AgentError("manifest: missing version or url")
    return data


def missing_permissions(info: dict[str, Any]) -> list[str]:
    """Permissions the agent reports as not granted (empty when all is well)."""
    perms = info.get("permissions") or {}
    return [
        k for k in ("notification_access", "bluetooth_connect") if perms.get(k) is False
    ]


def parse_sse(lines: Iterable[str]) -> Iterable[tuple[str, str]]:
    """Yield ``(event, data)`` pairs from Server-Sent Events text lines.

    Follows the SSE framing: ``event:`` and ``data:`` fields accumulate until a
    blank line dispatches the event; multi-line data is joined with newlines;
    comment lines (``:``) and unknown fields are ignored.
    """
    event = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value or "message"
        elif field == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


def make_ssl_context(cert_der: bytes | None) -> ssl.SSLContext:
    """TLS client context for the agent.

    With ``cert_der`` (the agent's self-signed certificate) the context trusts
    exactly that certificate and nothing else, so a different one fails the
    handshake before any request is sent. Without it, verification is off:
    used only to discover the agent and learn its certificate during pairing.
    Names are never checked (the agent is addressed by IP).

    Loads a certificate, so call it off the event loop.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    if cert_der is None:
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx.verify_mode = ssl.CERT_REQUIRED
    # The leaf is its own trust anchor; it carries no CA flag, so allow a
    # chain that ends at a non-CA certificate in the store.
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert_der))
    return ctx


def fingerprint_of(cert_der: bytes) -> str:
    """SHA-256 of a DER certificate, lowercase hex (as the agent publishes it)."""
    return hashlib.sha256(cert_der).hexdigest()


def encode_cert(cert_der: bytes) -> str:
    return base64.b64encode(cert_der).decode("ascii")


def decode_cert(text: str | None) -> bytes | None:
    try:
        return base64.b64decode(text) if text else None
    except ValueError:
        return None


class AgentClient:
    """Thin async HTTPS client for one agent instance.

    ``ssl_context`` comes from :func:`make_ssl_context`: pinned to the agent's
    certificate once known, permissive before pairing. The certificate seen on
    each connection is recorded in ``seen_cert`` so the coordinator can pin it
    after pairing has proven it talks to the right agent.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int = AGENT_PORT,
        token: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        cert: bytes | None = None,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self.token = token
        self.ssl_context = ssl_context
        self.cert = cert
        self.seen_cert: bytes | None = None

    @property
    def pinned(self) -> bool:
        return self.cert is not None

    @property
    def fingerprint(self) -> str | None:
        return fingerprint_of(self.cert) if self.cert else None

    @property
    def seen_fingerprint(self) -> str | None:
        return fingerprint_of(self.seen_cert) if self.seen_cert else None

    def pin(self, cert_der: bytes | None, ssl_context: ssl.SSLContext) -> None:
        """Use ``ssl_context`` (built for ``cert_der``) from now on."""
        self.cert = cert_der
        self.ssl_context = ssl_context

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        # identity: a gzipped event stream would be buffered by the agent's
        # deflater and arrive in bursts instead of per event.
        headers = {"Accept-Encoding": "identity"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _ssl(self) -> Any:
        """aiohttp ``ssl`` argument."""
        return self.ssl_context if self.ssl_context is not None else False

    def _observe(self, resp: aiohttp.ClientResponse) -> None:
        """Record the certificate this response arrived over."""
        try:
            conn = resp.connection
            transport = conn.transport if conn is not None else None
            ssl_object = transport.get_extra_info("ssl_object") if transport else None
            der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        except Exception:  # noqa: BLE001
            der = None
        if der:
            self.seen_cert = der

    def _wrap(self, err: Exception, what: str) -> AgentError:
        if self.pinned and isinstance(
            err, aiohttp.ClientConnectorCertificateError | ssl.SSLCertVerificationError
        ):
            return AgentFingerprintMismatch(what)
        return AgentError(f"{what}: {err}")

    async def _get_json(self, path: str, timeout: float) -> dict[str, Any]:
        try:
            async with self._session.get(
                self.url(path),
                headers=self._headers(),
                ssl=self._ssl(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                self._observe(resp)
                if resp.status == 401:
                    raise AgentUnauthorized(path)
                if resp.status != 200:
                    raise AgentError(f"{path}: HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except AgentError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as err:
            raise self._wrap(err, path) from err
        except Exception as err:  # noqa: BLE001 - resolver/loop quirks surface raw
            raise self._wrap(err, path) from err
        if not isinstance(data, dict):
            raise AgentError(f"{path}: not an object")
        return data

    async def async_info(self) -> dict[str, Any]:
        """``GET /v1/info`` (never needs the token)."""
        return await self._get_json("/v1/info", _INFO_TIMEOUT)

    async def async_state(self) -> dict[str, Any]:
        return await self._get_json("/v1/state", _REQUEST_TIMEOUT)

    async def async_themes(self) -> list[dict[str, Any]]:
        data = await self._get_json("/v1/themes", _REQUEST_TIMEOUT)
        themes = data.get("themes")
        return (
            [t for t in themes if isinstance(t, dict)]
            if isinstance(themes, list)
            else []
        )

    async def async_artwork(self) -> bytes | None:
        """The current now-playing artwork (PNG), or None when there is none."""
        try:
            async with self._session.get(
                self.url("/v1/media/artwork"),
                headers=self._headers(),
                ssl=self._ssl(),
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 401:
                    raise AgentUnauthorized("/v1/media/artwork")
                if resp.status != 200:
                    return None
                return await resp.read()
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise self._wrap(err, "artwork") from err

    async def async_pair(self, name: str) -> str:
        """``POST /v1/pair`` while the agent's pairing window is open."""
        try:
            async with self._session.post(
                self.url("/v1/pair"),
                params={"name": name},
                ssl=self._ssl(),
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
            ) as resp:
                self._observe(resp)
                if resp.status == 403:
                    raise AgentPairingClosed("pairing window closed")
                if resp.status != 200:
                    raise AgentError(f"pair: HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except AgentError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as err:
            raise self._wrap(err, "pair") from err
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise AgentError("pair: no token in reply")
        self.token = token
        return token

    async def async_stage_update(
        self, url: str, sha256: str | None, version: str | None
    ) -> None:
        """``POST /v1/update``: tell the agent which APK to install next."""
        params = {"url": url}
        if sha256:
            params["sha256"] = sha256
        if version:
            params["version"] = version
        try:
            async with self._session.post(
                self.url("/v1/update"),
                params=params,
                headers=self._headers(),
                ssl=self._ssl(),
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 401:
                    raise AgentUnauthorized("/v1/update")
                if resp.status != 200:
                    raise AgentError(f"update: HTTP {resp.status}")
        except AgentError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise self._wrap(err, "update") from err

    async def async_events(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Stream ``(event, payload)`` from ``GET /v1/events`` until the link drops.

        Raises AgentError when the connection fails or stays silent longer than
        EVENTS_IDLE_TIMEOUT; the caller decides how to reconnect.
        """
        try:
            async with self._session.get(
                self.url("/v1/events"),
                headers=self._headers(),
                ssl=self._ssl(),
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=_REQUEST_TIMEOUT,
                    sock_read=EVENTS_IDLE_TIMEOUT,
                ),
            ) as resp:
                self._observe(resp)
                if resp.status == 401:
                    raise AgentUnauthorized("/v1/events")
                if resp.status != 200:
                    raise AgentError(f"events: HTTP {resp.status}")
                # Iterating the body yields one line at a time; an SSE event
                # spans several lines and ends with a blank one, so collect
                # lines until then and parse the whole event.
                pending: list[str] = []
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace")
                    pending.append(line)
                    if line.strip():
                        continue
                    frame, pending = pending, []
                    for event, data in parse_sse(frame):
                        if event == "heartbeat":
                            continue
                        try:
                            payload = json.loads(data)
                        except ValueError:
                            continue
                        if isinstance(payload, dict):
                            yield event, payload
        except AgentError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise self._wrap(err, "events") from err
        raise AgentError("events: stream ended")


def state_blocks(document: dict[str, Any]) -> dict[str, Any]:
    """The known blocks of a state document, as ``{block: value}``."""
    return {block: document.get(block) for block in BLOCKS}


ArtworkFetcher = Callable[[], Any]
