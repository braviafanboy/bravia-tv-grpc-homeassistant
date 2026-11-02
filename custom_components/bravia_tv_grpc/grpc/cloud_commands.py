"""Sony Seeds IoT cloud command channel (BRAVIA Connect's cloud plane).

The TV registers two capability sets with Sony: the local ``GetCapabilities``
schema served over gRPC, and a smaller cloud set (the ``capabilities`` list of
its ``GET /devices`` entry; see ``schema/cloud_device.json``). Several controls
exist *only* on the cloud set — sleep timer, picture off, playback transport,
seek, deep links, D-pad — so this channel is the only way to reach them from
outside the TV. It uses the same account, token and API key as the other IoT
calls in ``credentials.py``.

Flow:

1. ``POST {IOT_BASE_URL}/messages`` with
   ``{"device_id": <id>, "type": "command", "body": {"commands": [...]}}``;
   the reply carries ``message_id``.
2. Sony delivers the message to the TV, which executes it and reports
   ``{result: "SUCCESS"|"FAILURE", error, reports: {changes, states}}``.
3. ``GET {IOT_BASE_URL}/messages/{id}/result?wait_seconds=N`` long-polls for
   that report. Until the TV has answered the cloud replies HTTP 200 with
   ``{"device_id": ..., "result": "waiting"}``; afterwards ``result`` is
   ``"success"`` / ``"failure"`` and ``reports.states`` carries the changed
   capability plus a baseline bundle (power, volume, mute, playback).
   Verified live on a BRAVIA 8 II (2026-11-02).

Command shape::

    {"name": "<capability>", "command": "set",    "args": {"value": <int|enum|bool>}}
    {"name": "<capability>", "command": "plus",   "args": {"value": <int step>}}
    {"name": "<capability>", "command": "post",   "args": {"args": {...}}}
    {"name": "<capability>", "command": "set",    "args": {"args": {...}}}   # any-typed
    {"name": "<capability>", "command": "toggle"}

This module is HA-agnostic (aiohttp only) so it can be exercised standalone by
``tools/cloud_command_probe.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import ClientResponseError, ClientSession

from .credentials import _IOT_HEADERS_BASE, IOT_BASE_URL

# Cloud capability names that have no local gRPC path on current BRAVIA TVs
# (K-65XR8M2 verified), with the cloud command words the TV advertised.
CLOUD_ONLY_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "sleep_timer": ("set",),
    "picture": ("set",),
    "playback": ("set",),
    "seek": ("set", "plus"),
    "application.deep_link": ("post",),
    "select_media": ("post",),
    "channel": ("post",),
    "skip_channels": ("plus",),
    "search": ("post",),
    "d_pad": ("set",),
    "gui_shortcut": ("set",),
    "access_shortcut": ("set",),
    "speaker_type": ("set",),
    "ui_action": ("post",),
}

SLEEP_TIMER_VALUES = ("off", "15min", "30min", "45min", "60min", "90min", "120min")
PLAYBACK_VALUES = (
    "playing",
    "paused",
    "stopped",
    "fast_forwarding",
    "rewinding",
    "previous",
    "next",
    "none",
)

# Long-poll length per result request and the overall wait for a result. The
# TV usually answers within a second; a TV in deep standby can take longer to
# wake its FCM client.
RESULT_WAIT_SECONDS = 10
RESULT_TIMEOUT = 25.0


class CloudCommandError(OSError):
    """The cloud rejected the message or the TV reported a failure.

    ``status`` carries the HTTP status for a cloud-side rejection; ``result``
    carries the TV's result document when the TV executed but failed, and
    ``error`` its ``ErrorType`` name (e.g. ``POWER_OFF``).
    """

    def __init__(
        self,
        *args: object,
        status: int | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        super().__init__(*args)
        self.status = status
        self.result = result
        self.error = error


class CloudCommandTimeout(CloudCommandError):
    """The TV did not report a result in time (offline / FCM not delivered)."""


# ----------------------------------------------------------------------------
# Command builders (pure)
# ----------------------------------------------------------------------------
def set_command(name: str, value: Any) -> dict[str, Any]:
    """``set`` a capability. Scalars go in ``value``; dicts (any-typed) in
    ``args``."""
    if isinstance(value, dict):
        return {"name": name, "command": "set", "args": {"args": value}}
    return {"name": name, "command": "set", "args": {"value": value}}


def plus_command(name: str, step: int) -> dict[str, Any]:
    """``plus`` an int capability by ``step`` (negative steps down)."""
    return {"name": name, "command": "plus", "args": {"value": int(step)}}


def post_command(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """``post`` an any-typed action (deep link, channel, search, ui_action)."""
    return {"name": name, "command": "post", "args": {"args": dict(args)}}


def toggle_command(name: str) -> dict[str, Any]:
    """``toggle`` a bool capability."""
    return {"name": name, "command": "toggle"}


def build_message(device_id: str, commands: list[dict[str, Any]]) -> dict[str, Any]:
    """The document BRAVIA Connect posts to ``/messages``."""
    if not commands:
        raise ValueError("at least one command is required")
    return {
        "device_id": device_id,
        "type": "command",
        "body": {"commands": list(commands)},
    }


def unwrap(document: Any) -> Any:
    """Strip the cloud's ``{"data": ...}`` success envelope when present."""
    if isinstance(document, dict) and "data" in document and len(document) <= 2:
        return document["data"]
    return document


def parse_result(document: Any) -> tuple[bool | None, dict[str, Any]]:
    """Return ``(outcome, result_document)`` for a result reply.

    ``outcome`` is ``True``/``False`` once the TV has reported, ``None`` while
    the message is still pending delivery or execution (empty / waiting
    reply). The returned document is the TV's own result document.
    """
    doc = unwrap(document)
    if not isinstance(doc, dict) or not doc:
        return None, {}
    result = doc.get("result")
    if isinstance(result, bool):
        return result, doc
    if isinstance(result, str):
        low = result.lower()
        if low in ("success", "succeeded", "ok"):
            return True, doc
        if low in ("failure", "failed", "error"):
            return False, doc
    return None, doc


def reported_states(document: dict[str, Any]) -> dict[str, Any]:
    """``{name: value}`` for every state the TV reported in a result."""
    reports = document.get("reports")
    if not isinstance(reports, dict):
        return {}
    states = reports.get("states")
    if not isinstance(states, list):
        return {}
    out: dict[str, Any] = {}
    for item in states:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out[item["name"]] = item.get("value")
    return out


def capabilities_from_device(device: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """``{name: commands}`` from a ``GET /devices`` entry's capability list."""
    caps: dict[str, tuple[str, ...]] = {}
    for cap in device.get("capabilities") or []:
        if not isinstance(cap, dict) or not isinstance(cap.get("name"), str):
            continue
        props = cap.get("props") or {}
        commands = props.get("commands") if isinstance(props, dict) else None
        caps[cap["name"]] = tuple(c for c in (commands or []) if isinstance(c, str))
    return caps


def capability_values(device: dict[str, Any], name: str) -> tuple[str, ...]:
    """Advertised enum values of a cloud capability, or ``()``."""
    for cap in device.get("capabilities") or []:
        if isinstance(cap, dict) and cap.get("name") == name:
            values = (cap.get("props") or {}).get("values") or []
            return tuple(v for v in values if isinstance(v, str))
    return ()


# ----------------------------------------------------------------------------
# Async transport
# ----------------------------------------------------------------------------
def _headers(access_token: str) -> dict[str, str]:
    return {
        **_IOT_HEADERS_BASE,
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }


async def async_send_message(
    session: ClientSession,
    device_id: str,
    access_token: str,
    commands: list[dict[str, Any]],
) -> str:
    """Post a command message for ``device_id``; return its ``message_id``."""
    url = f"{IOT_BASE_URL}/messages"
    body = json.dumps(build_message(device_id, commands), separators=(",", ":"))
    async with session.post(url, data=body, headers=_headers(access_token)) as resp:
        text = await resp.text()
        try:
            resp.raise_for_status()
        except ClientResponseError as exc:
            msg = f"Sony cloud rejected the command: HTTP {exc.status} {text[:200]}"
            raise CloudCommandError(msg, status=exc.status) from exc
    try:
        document = unwrap(json.loads(text)) if text else {}
    except ValueError as exc:
        raise CloudCommandError(f"unexpected reply: {text[:200]}") from exc
    message_id = document.get("message_id") if isinstance(document, dict) else None
    if not isinstance(message_id, str) or not message_id:
        raise CloudCommandError(f"no message_id in reply: {text[:200]}")
    return message_id


async def async_get_result(
    session: ClientSession,
    message_id: str,
    access_token: str,
    *,
    wait_seconds: int = RESULT_WAIT_SECONDS,
) -> tuple[bool | None, dict[str, Any]]:
    """One long-poll for a message's result: ``(outcome, document)``.

    ``outcome`` is ``None`` while the TV has not reported yet.
    """
    url = f"{IOT_BASE_URL}/messages/{message_id}/result?wait_seconds={wait_seconds}"
    async with session.get(url, headers=_headers(access_token)) as resp:
        text = await resp.text()
        if resp.status in (202, 204):
            return None, {}
        try:
            resp.raise_for_status()
        except ClientResponseError as exc:
            msg = f"Sony cloud result lookup failed: HTTP {exc.status} {text[:200]}"
            raise CloudCommandError(msg, status=exc.status) from exc
    if not text.strip():
        return None, {}
    try:
        return parse_result(json.loads(text))
    except ValueError as exc:
        raise CloudCommandError(f"unexpected result reply: {text[:200]}") from exc


async def async_run_commands(
    session: ClientSession,
    device_id: str,
    access_token: str,
    commands: list[dict[str, Any]],
    *,
    timeout: float = RESULT_TIMEOUT,
    wait_seconds: int = RESULT_WAIT_SECONDS,
) -> dict[str, Any]:
    """Send ``commands`` and wait for the TV's result document.

    Raises :class:`CloudCommandError` if the TV reports failure (``error``
    carries its ``ErrorType``) and :class:`CloudCommandTimeout` if no result
    arrives within ``timeout``.
    """
    message_id = await async_send_message(session, device_id, access_token, commands)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    document: dict[str, Any] = {}
    while True:
        outcome, document = await async_get_result(
            session, message_id, access_token, wait_seconds=wait_seconds
        )
        if outcome is True:
            return document
        if outcome is False:
            error = document.get("error")
            raise CloudCommandError(
                f"TV rejected cloud command {message_id}: {error or document}",
                result=document,
                error=error if isinstance(error, str) else None,
            )
        if loop.time() >= deadline:
            raise CloudCommandTimeout(
                f"no result for cloud command {message_id} within {timeout:.0f}s",
                result=document,
            )
        # A reply without a document came back early; don't spin.
        await asyncio.sleep(1.0)
