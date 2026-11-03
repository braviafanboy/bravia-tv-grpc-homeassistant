"""Client for the BRAVIA TV agent (the companion app running on the TV).

Pure protocol module (aiohttp only, no Home Assistant): the agent serves a JSON
document over plain HTTP on the LAN (default port 8585), advertises itself with
mDNS ``_bravia-agent._tcp`` and streams changes as Server-Sent Events. See
https://github.com/braviafanboy/bravia-tv-agent for the contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
import json
import secrets
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


def new_token() -> str:
    """A fresh bearer token for cloud delivery (64 hex chars, like the agent's)."""
    return secrets.token_hex(32)


def pair_deep_link(token: str, name: str) -> str:
    """The ``braviaagent://pair`` URI that makes the agent adopt ``token``."""
    return (
        f"braviaagent://pair?token={token}&name={aiohttp.helpers.quote(name, safe='')}"
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


class AgentClient:
    """Thin async HTTP client for one agent instance."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int = AGENT_PORT,
        token: str | None = None,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self.token = token

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _get_json(self, path: str, timeout: float) -> dict[str, Any]:
        try:
            async with self._session.get(
                self.url(path),
                headers=self._headers(),
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
            raise AgentError(f"{path}: {err}") from err
        except Exception as err:  # noqa: BLE001 - resolver/loop quirks surface raw
            raise AgentError(f"{path}: {err}") from err
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
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 401:
                    raise AgentUnauthorized("/v1/media/artwork")
                if resp.status != 200:
                    return None
                return await resp.read()
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise AgentError(f"artwork: {err}") from err

    async def async_pair(self, name: str) -> str:
        """``POST /v1/pair`` while the agent's pairing window is open."""
        try:
            async with self._session.post(
                self.url("/v1/pair"),
                params={"name": name},
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 403:
                    raise AgentPairingClosed("pairing window closed")
                if resp.status != 200:
                    raise AgentError(f"pair: HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except AgentError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as err:
            raise AgentError(f"pair: {err}") from err
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise AgentError("pair: no token in reply")
        self.token = token
        return token

    async def async_events(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Stream ``(event, payload)`` from ``GET /v1/events`` until the link drops.

        Raises AgentError when the connection fails or stays silent longer than
        EVENTS_IDLE_TIMEOUT; the caller decides how to reconnect.
        """
        try:
            async with self._session.get(
                self.url("/v1/events"),
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=_REQUEST_TIMEOUT,
                    sock_read=EVENTS_IDLE_TIMEOUT,
                ),
            ) as resp:
                if resp.status == 401:
                    raise AgentUnauthorized("/v1/events")
                if resp.status != 200:
                    raise AgentError(f"events: HTTP {resp.status}")
                pending: list[str] = []
                async for chunk in resp.content:
                    pending.append(chunk.decode("utf-8", "replace"))
                    text = "".join(pending)
                    if not text.endswith("\n"):
                        continue
                    pending = []
                    for event, data in parse_sse(text.splitlines(keepends=True)):
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
            raise AgentError(f"events: {err}") from err
        raise AgentError("events: stream ended")


def state_blocks(document: dict[str, Any]) -> dict[str, Any]:
    """The known blocks of a state document, as ``{block: value}``."""
    return {block: document.get(block) for block in BLOCKS}


ArtworkFetcher = Callable[[], Any]
