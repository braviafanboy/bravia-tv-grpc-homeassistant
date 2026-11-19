"""Client for the HA TV Agent (the companion app running on the TV).

Pure protocol module (aiohttp only, no Home Assistant): the agent serves a JSON
document over HTTPS on the LAN (default port 8585, a self-signed certificate
that clients pin by its SHA-256 fingerprint), advertises itself with mDNS
``_bravia-agent._tcp`` and streams changes as Server-Sent Events. See
https://github.com/braviafanboy/bravia-tv-agent for the contract.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Iterable
import hashlib
import json
import secrets
from typing import Any

import aiohttp

AGENT_PORT = 8585
AGENT_PACKAGE = "com.braviafanboy.braviaagent"
# The contract version this client implements (``/v1/info`` -> agent.contract).
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
    "rest",
    "picture",
    "power_saving",
    "remote_finder",
    "ambient",
    "dashboard",
)
_INFO_TIMEOUT = 4
_REQUEST_TIMEOUT = 8
# Room for one event line: the state document travels as a single ``data:``
# line, and aiohttp fails a line longer than twice its read buffer.
_EVENTS_READ_BUFFER = 1 << 20
# The agent sends a heartbeat every 30 s; treat a longer silence as a dead link.
EVENTS_IDLE_TIMEOUT = 75


class AgentError(Exception):
    """The agent could not be reached or answered with an error."""


class AgentUnauthorized(AgentError):
    """The agent rejected our token (401): re-pairing is needed."""


class AgentPairingClosed(AgentError):
    """POST /v1/pair answered 403: the pairing request is gone (it lapsed), or
    an agent from before 0.10 has no pairing window open."""


class AgentPairingDenied(AgentPairingClosed):
    """The viewer denied the pairing request on the TV."""


class AgentPairingBusy(AgentError):
    """POST /v1/pair answered 409: another pairing request is waiting on the
    agent, or ours just lapsed or was denied. ``retry_after`` is how many
    seconds until it is worth asking again."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"another pairing request is waiting ({retry_after} s)")
        self.retry_after = retry_after


class AgentPairRequest:
    """A pairing request the agent accepted (202).

    ``secret`` is ours and ``nonce`` the agent's; together with the agent's
    certificate they give the code both sides show (:func:`pairing_code`).
    The agent has only our commitment to ``secret`` so far."""

    def __init__(
        self, request_id: str, secret: bytes, nonce: bytes, expires_in: float
    ) -> None:
        self.request_id = request_id
        self.secret = secret
        self.nonce = nonce
        self.expires_in = expires_in


class AgentFingerprintMismatch(AgentError):
    """The agent's port presented a certificate other than the pinned one.

    ``got`` is the SHA-256 (hex) of the certificate that was presented."""

    def __init__(self, what: str, got: str | None = None) -> None:
        super().__init__(what)
        self.got = got


class AgentRequestError(AgentError):
    """The agent refused a request; ``code`` is its error string (e.g.
    ``rest_unauthorized``, ``invalid_value``, ``rest_unavailable``)."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(f"{code} (HTTP {status})")
        self.code = code
        self.status = status


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


def _as_int(value: Any, default: int) -> int:
    """A count from the agent's JSON, or ``default`` when it is not one."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def missing_permissions(info: dict[str, Any]) -> list[str]:
    """Permissions the agent reports as not granted (empty when all is well)."""
    perms = info.get("permissions") or {}
    return [
        k for k in ("notification_access", "bluetooth_connect") if perms.get(k) is False
    ]


def setup_complete(info: dict[str, Any]) -> bool:
    """True only when the agent reports every grant its setup flow asks for
    (unknown counts as not complete)."""
    perms = info.get("permissions") or {}
    return all(
        perms.get(k) is True
        for k in ("notification_access", "bluetooth_connect", "install_packages")
    )


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


_PAIRING_LABEL = b"bravia-agent pairing 1"
_PAIRING_SECRET_BYTES = 32


def pairing_code(secret: bytes, nonce: bytes, fingerprint: str) -> str:
    """The six digits a pairing shows on both sides.

    ``secret`` is the client's, ``nonce`` the agent's, ``fingerprint`` the
    SHA-256 (hex) of the certificate the client saw the agent present. The
    agent computes the same from its own certificate, so the two agree only
    when nothing stands between them, and only for this one request. Neither
    side can steer the result: the client commits to its secret before it
    learns the agent's, and reveals it after.

    The first four bytes of SHA-256(label, 0, secret, nonce, certificate
    SHA-256) as a number, modulo a million.
    """
    digest = hashlib.sha256(
        _PAIRING_LABEL + b"\x00" + secret + nonce + bytes.fromhex(fingerprint)
    ).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


def fingerprint_of(cert_der: bytes) -> str:
    """SHA-256 of a DER certificate, lowercase hex (as the agent publishes it)."""
    return hashlib.sha256(cert_der).hexdigest()


def decode_cert(text: str | None) -> bytes | None:
    """A stored base64 DER certificate (entries pinned before fingerprints
    alone were kept), or None."""
    try:
        return base64.b64decode(text) if text else None
    except ValueError:
        return None


def normalize_fingerprint(value: Any) -> str | None:
    """A stored fingerprint as 64 lowercase hex characters, or None."""
    if not isinstance(value, str):
        return None
    text = value.replace(":", "").strip().lower()
    if len(text) != 64:
        return None
    try:
        bytes.fromhex(text)
    except ValueError:
        return None
    return text


class AgentClient:
    """Thin async HTTPS client for one agent instance.

    The agent's certificate is self-signed, so nothing about its name or chain
    is checked. Instead the client pins the certificate itself: once
    ``fingerprint`` (its SHA-256) is set, every connection is refused unless
    the peer presents exactly that certificate. Before a pin exists the
    connection is unverified; :meth:`async_fetch_fingerprint` reads the
    certificate so the caller can pin it before sending anything that matters.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int = AGENT_PORT,
        token: str | None = None,
        fingerprint: str | None = None,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self.token = token
        self._fingerprint: str | None = None
        # One object for the client's lifetime: aiohttp keys its connection
        # pool on it, so requests share connections.
        self._pin: aiohttp.Fingerprint | None = None
        self.pin(fingerprint)

    @property
    def pinned(self) -> bool:
        return self._pin is not None

    @property
    def fingerprint(self) -> str | None:
        """SHA-256 (lowercase hex) of the pinned certificate, if any."""
        return self._fingerprint

    def pin(self, fingerprint: str | None) -> None:
        """Accept only the certificate with this SHA-256 from now on; ``None``
        drops the pin. Raises ``ValueError`` for anything but 64 hex digits."""
        if fingerprint is None:
            self._fingerprint = None
            self._pin = None
            return
        text = normalize_fingerprint(fingerprint)
        if text is None:
            raise ValueError("fingerprint must be 64 hex characters")
        self._pin = aiohttp.Fingerprint(bytes.fromhex(text))
        self._fingerprint = text

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        # identity: a gzipped event stream would be buffered by the agent's
        # deflater and arrive in bursts instead of per event.
        headers = {"Accept-Encoding": "identity"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _ssl(self) -> Any:
        """aiohttp ``ssl`` argument: the pin, or no verification before one
        exists (a self-signed certificate has nothing else to check)."""
        return self._pin if self._pin is not None else False

    def _wrap(self, err: Exception, what: str) -> AgentError:
        if isinstance(err, aiohttp.ServerFingerprintMismatch):
            got = getattr(err, "got", None)
            return AgentFingerprintMismatch(
                what, got.hex() if isinstance(got, bytes) else None
            )
        return AgentError(f"{what}: {err}")

    async def async_fetch_fingerprint(self) -> str:
        """SHA-256 (hex) of the certificate the agent's port presents now.

        Used once, before pairing, to learn which certificate to pin. aiohttp
        checks a pin right after the TLS handshake and reports the digest it
        saw when the pin does not match, so asking for a pin nothing can
        match reads the certificate without sending a request."""
        probe = aiohttp.Fingerprint(bytes(32))
        try:
            async with self._session.get(
                self.url("/v1/info"),
                headers=self._headers(auth=False),
                ssl=probe,
                timeout=aiohttp.ClientTimeout(total=_INFO_TIMEOUT),
            ):
                pass
        except aiohttp.ServerFingerprintMismatch as err:
            got = getattr(err, "got", None)
            if isinstance(got, bytes) and len(got) == 32:
                return got.hex()
            raise AgentError("certificate: not reported") from err
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise AgentError(f"certificate: {err}") from err
        raise AgentError("certificate: could not be read")

    async def _get_json(
        self, path: str, timeout: float, *, auth: bool = True
    ) -> dict[str, Any]:
        try:
            async with self._session.get(
                self.url(path),
                headers=self._headers(auth=auth),
                ssl=self._ssl(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
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
        """``GET /v1/info``. It answers without the token, and the call also
        runs before a certificate is pinned, when no token may be sent. Once
        the certificate is pinned the token goes along: the agent then adds
        what it keeps from strangers (the TV's build, who is paired)."""
        return await self._get_json("/v1/info", _INFO_TIMEOUT, auth=self.pinned)

    async def async_state(self) -> dict[str, Any]:
        return await self._get_json("/v1/state", _REQUEST_TIMEOUT)

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

    async def _post_pair(self, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        try:
            async with self._session.post(
                self.url("/v1/pair"),
                params=params,
                headers=self._headers(auth=False),
                ssl=self._ssl(),
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
            ) as resp:
                status = resp.status
                if status not in (200, 202, 403, 409):
                    raise AgentError(f"pair: HTTP {status}")
                data = await resp.json(content_type=None)
        except AgentError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as err:
            raise self._wrap(err, "pair") from err
        return status, data if isinstance(data, dict) else {}

    def _paired(self, data: dict[str, Any]) -> str:
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise AgentError("pair: no token in reply")
        self.token = token
        return token

    async def async_pair_request(self, name: str) -> AgentPairRequest | str:
        """``POST /v1/pair?name=…&commit=…``: ask to pair.

        The agent answers with a request (:class:`AgentPairRequest`) and its
        own secret; nothing shows on the TV yet. ``commit`` is the SHA-256 of
        a secret of ours, revealed next (:meth:`async_pair_reveal`). Raises
        :class:`AgentPairingBusy` while another request is waiting there.

        An agent from before 0.10 knows no requests: it hands out the token
        while its pairing window is open (returned here as a ``str``) and
        answers 403 otherwise (:class:`AgentPairingClosed`).
        """
        secret = secrets.token_bytes(_PAIRING_SECRET_BYTES)
        status, data = await self._post_pair(
            {"name": name, "commit": hashlib.sha256(secret).hexdigest()}
        )
        if status == 200:
            return self._paired(data)
        if status == 403:
            raise AgentPairingClosed("no pairing window is open")
        if status == 409:
            raise AgentPairingBusy(_as_int(data.get("retry_after"), 30))
        request_id = data.get("request")
        if not isinstance(request_id, str) or not request_id:
            raise AgentError("pair: no request id in reply")
        try:
            nonce = bytes.fromhex(str(data.get("nonce") or ""))
        except ValueError:
            nonce = b""
        if len(nonce) != _PAIRING_SECRET_BYTES:
            raise AgentError("pair: no agent secret in reply")
        return AgentPairRequest(
            request_id, secret, nonce, _as_int(data.get("expires_in"), 30)
        )

    async def async_pair_reveal(self, asked: AgentPairRequest) -> str:
        """``POST /v1/pair?request=<id>&reveal=…``: reveal our secret, which
        gives the request its code, and return that code as we compute it.

        It is tied to the pinned certificate, so pairing needs one: a code
        computed from a certificate nobody checked would prove nothing."""
        if not self._fingerprint:
            raise AgentError("pair: no certificate is pinned")
        status, _ = await self._post_pair(
            {"request": asked.request_id, "reveal": asked.secret.hex()}
        )
        if status != 202:
            raise AgentPairingClosed("the pairing request is gone")
        return pairing_code(asked.secret, asked.nonce, self._fingerprint)

    async def async_pair_collect(self, request_id: str) -> str | None:
        """``POST /v1/pair?request=<id>``: collect the token for a request.

        ``None`` while the viewer has not allowed it yet. Raises
        :class:`AgentPairingDenied` when they denied it, and
        :class:`AgentPairingClosed` once the request is gone."""
        status, data = await self._post_pair({"request": request_id})
        if status == 200:
            return self._paired(data)
        if status == 202:
            return None
        if data.get("error") == "pairing_denied":
            raise AgentPairingDenied("the pairing request was denied on the TV")
        raise AgentPairingClosed("the pairing request lapsed")

    async def async_stage_update(self, url: str, sha256: str, version: str) -> None:
        """``POST /v1/update``: tell the agent which APK to install next. The
        agent takes it only with the SHA-256 the download must have and the
        version it brings (and from 0.10 only from an https address)."""
        if not sha256 or not version:
            raise AgentError("update: the release manifest has no checksum or version")
        params = {"url": url, "sha256": sha256, "version": version}
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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """An authenticated request; returns the JSON reply or raises."""
        try:
            async with self._session.request(
                method,
                self.url(path),
                json=body,
                params=params,
                headers=self._headers(),
                ssl=self._ssl(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 401:
                    raise AgentUnauthorized(path)
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = None
                if resp.status != 200:
                    code = data.get("error") if isinstance(data, dict) else None
                    raise AgentRequestError(str(code or "http_error"), resp.status)
        except AgentError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise self._wrap(err, path) from err
        return data if isinstance(data, dict) else {}

    async def async_arm_setup(self) -> bool:
        """``POST /v1/setup``: the next launch of the agent by the TV opens its
        permission screens. False for an agent that has no such call (before
        0.10), where the launch alone has to do."""
        try:
            await self._request("POST", "/v1/setup")
        except AgentRequestError as err:
            if err.status in (404, 405):
                return False
            raise
        return True

    async def async_find_remote(self) -> str:
        """``POST /v1/remote/find``: beep the remote. Returns the agent's
        outcome (ringing, found, unsupported, no_remote, no_permission, failed)."""
        data = await self._request(
            "POST", "/v1/remote/find", timeout=_REQUEST_TIMEOUT * 3
        )
        return str(data.get("state") or "failed")

    async def async_notify(
        self,
        message: str,
        title: str | None = None,
        duration_s: int | None = None,
        icon: str | None = None,
    ) -> dict[str, Any]:
        """Show a message on the TV screen; returns {"state", "overlay"}."""
        body: dict[str, Any] = {"message": message}
        if title:
            body["title"] = title
        if duration_s:
            body["duration_s"] = duration_s
        if icon:
            body["icon"] = icon
        return await self._request("POST", "/v1/notify", body=body)

    async def async_dashboard_open(
        self,
        url: str,
        token: str,
        expires_in: int,
        *,
        mode: str = "full",
        position: str | None = None,
        size: int | None = None,
        duration_s: int | None = None,
        kiosk: bool | None = None,
    ) -> dict[str, Any]:
        """Show a Home Assistant dashboard on the TV; returns {"state"}
        (shown | no_permission | failed). 404 on agents before 0.9.0."""
        body: dict[str, Any] = {
            "url": url,
            "mode": mode,
            "token": token,
            "expires_in": expires_in,
        }
        if kiosk is not None:  # omitted: the agent's per-mode default
            body["kiosk"] = kiosk
        if position:
            body["position"] = position
        if size:
            body["size"] = size
        if duration_s:
            body["duration_s"] = duration_s
        return await self._request("POST", "/v1/dashboard", body=body)

    async def async_dashboard_token(self, token: str, expires_in: int) -> None:
        """Hand the showing dashboard a fresh access token."""
        await self._request(
            "POST",
            "/v1/dashboard/token",
            body={"token": token, "expires_in": expires_in},
        )

    async def async_dashboard_close(self) -> None:
        """Close the dashboard on the TV, if one is showing."""
        await self._request("DELETE", "/v1/dashboard")

    async def async_media_command(
        self, action: str, position_ms: int | None = None
    ) -> dict[str, Any]:
        """Send a transport command (play/pause/stop/next/previous/seek) to the
        TV's active media session; returns {"ok": bool} (ok False when there is
        no controllable session). Raises AgentError on an older agent (404)."""
        body: dict[str, Any] = {"action": action}
        if position_ms is not None:
            body["position_ms"] = position_ms
        return await self._request("POST", "/v1/media/command", body=body)

    async def async_picture_batch(
        self, settings: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Apply many picture targets for a profile restore; returns
        {"applied", "failed", "results": [{"target", "ok", "value"}]}."""
        return await self._request(
            "POST",
            "/v1/picture/batch",
            body={"settings": settings},
            timeout=_REQUEST_TIMEOUT * 10,
        )

    async def async_ambient(self, sub: str, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /v1/ambient/<sub>`` (config, curve, reset, curves, context):
        ambient brightness. Returns the new ambient status."""
        return await self._request("POST", f"/v1/ambient/{sub}", body=body)

    async def async_picture_refresh(self) -> None:
        """``POST /v1/picture/refresh``: re-read the REST picture settings now."""
        await self._request("POST", "/v1/picture/refresh")

    async def async_picture_config(
        self, poll_s: int, event_refresh: bool, watch: list[str]
    ) -> dict[str, Any]:
        """``POST /v1/picture/config``: refresh settings, stored on the TV."""
        return await self._request(
            "POST",
            "/v1/picture/config",
            body={"poll_s": poll_s, "event_refresh": event_refresh, "watch": watch},
        )

    async def async_set_picture(self, target: str, value: str) -> None:
        """``POST /v1/picture``: write one picture-quality target."""
        # Writes wait for the TV and the agent's re-read; allow for both.
        await self._request(
            "POST",
            "/v1/picture",
            body={"target": target, "value": value},
            timeout=_REQUEST_TIMEOUT * 2,
        )

    async def async_set_power_saving(self, mode: str) -> None:
        """``POST /v1/power_saving?mode=``."""
        await self._request(
            "POST",
            "/v1/power_saving",
            params={"mode": mode},
            timeout=_REQUEST_TIMEOUT * 2,
        )

    async def async_set_psk(self, psk: str) -> bool:
        """``POST /v1/rest/psk``: hand the agent the IP-control key; True when
        the TV accepted it."""
        data = await self._request("POST", "/v1/rest/psk", body={"psk": psk})
        return bool(data.get("write_authorized"))

    async def async_forget_rest_auth(self) -> None:
        """``DELETE /v1/rest/auth``: drop the agent's IP-control credential."""
        await self._request("DELETE", "/v1/rest/auth")

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
                read_bufsize=_EVENTS_READ_BUFFER,
            ) as resp:
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
        except ValueError as err:
            # aiohttp's "Chunk too big": one line outgrew the read buffer.
            raise AgentError(f"events: {err}") from err
        raise AgentError("events: stream ended")


def state_blocks(document: dict[str, Any]) -> dict[str, Any]:
    """The known blocks of a state document, as ``{block: value}``."""
    return {block: document.get(block) for block in BLOCKS}


def contract_of(info: dict[str, Any]) -> int | None:
    """The contract version an agent's ``/v1/info`` reports, if any."""
    value = (info.get("agent") or {}).get("contract")
    return value if isinstance(value, int) and not isinstance(value, bool) else None
