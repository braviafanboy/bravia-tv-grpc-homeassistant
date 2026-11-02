"""Probe the Sony Seeds cloud command channel against a real TV.

Standalone (no Home Assistant). Reuses the credentials saved by
``tools/pair_and_enumerate.py exchange`` (``schema/.credentials.json``).

    python tools/cloud_command_probe.py caps
    python tools/cloud_command_probe.py states
    python tools/cloud_command_probe.py send sleep_timer set 30min
    python tools/cloud_command_probe.py send picture set false
    python tools/cloud_command_probe.py send playback set paused
    python tools/cloud_command_probe.py send seek plus -- -10
    python tools/cloud_command_probe.py send application.deep_link post \
        --json '{"packageName": "com.google.android.youtube.tv", "uri": "..."}'

Every HTTP exchange is printed verbatim so the wire format can be confirmed
(and the module adjusted) without a packet capture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import types

_REPO = Path(__file__).resolve().parents[1]
_GRPC = _REPO / "custom_components" / "bravia_tv_grpc" / "grpc"
_CREDS_FILE = _REPO / "schema" / ".credentials.json"  # git-ignored

# Import the pure protocol modules under a synthetic package (as tests do), so
# the HA-dependent parent package is never imported.
if "btvgrpc" not in sys.modules:
    _pkg = types.ModuleType("btvgrpc")
    _pkg.__path__ = [str(_GRPC)]
    sys.modules["btvgrpc"] = _pkg

import aiohttp
from btvgrpc import cloud_commands as cloud
from btvgrpc import credentials as cred


def _parse_value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _build_command(args: argparse.Namespace) -> dict:
    extra = json.loads(args.json) if args.json else None
    if args.command == "set":
        value = extra if extra is not None else _parse_value(args.value or "")
        return cloud.set_command(args.name, value)
    if args.command == "plus":
        return cloud.plus_command(args.name, int(args.value or 0))
    if args.command == "post":
        return cloud.post_command(args.name, extra or {})
    if args.command == "toggle":
        return cloud.toggle_command(args.name)
    raise SystemExit(f"unknown command word: {args.command}")


async def _main(args: argparse.Namespace) -> int:
    creds = json.loads(Path(args.creds).read_text())
    device_id = args.device_id or creds["device_id"]
    async with aiohttp.ClientSession() as session:
        token = (
            await cred.async_refresh_access_token(session, creds["refresh_token"])
        )["access_token"]
        if args.mode == "caps":
            devices = (await cred.async_get_devices(session, token)).get("devices", [])
            device = next((d for d in devices if d.get("device_id") == device_id), None)
            if device is None:
                print(f"device {device_id} not on this account", file=sys.stderr)
                return 1
            for name, commands in sorted(
                cloud.capabilities_from_device(device).items()
            ):
                values = cloud.capability_values(device, name)
                print(f"{name:28s} {','.join(commands) or '-':10s} {' '.join(values)}")
            return 0
        if args.mode == "states":
            print(
                json.dumps(
                    await cred.async_get_device_states(session, device_id, token),
                    indent=2,
                )
            )
            return 0
        command = _build_command(args)
        print(">>> POST /messages")
        print(json.dumps(cloud.build_message(device_id, [command]), indent=2))
        try:
            message_id = await cloud.async_send_message(
                session, device_id, token, [command]
            )
        except cloud.CloudCommandError as err:
            print(f"!!! send failed: {err}")
            return 1
        print(f"<<< message_id = {message_id}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + args.timeout
        while True:
            print(f">>> GET /messages/{message_id}/result?wait_seconds={args.wait}")
            outcome, document = await cloud.async_get_result(
                session, message_id, token, wait_seconds=args.wait
            )
            print(f"<<< outcome={outcome} {json.dumps(document, indent=2)}")
            if outcome is not None:
                states = cloud.reported_states(document)
                if states:
                    print("reported states:", json.dumps(states))
                return 0 if outcome else 2
            if loop.time() >= deadline:
                print("!!! timed out waiting for the TV's result")
                return 3
            await asyncio.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--creds", default=str(_CREDS_FILE))
    parser.add_argument("--device-id", default=None)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("caps", help="list the TV's cloud capabilities")
    sub.add_parser("states", help="dump the cloud state snapshot")
    send = sub.add_parser("send", help="send one command and wait for the result")
    send.add_argument("name", help="cloud capability name, e.g. sleep_timer")
    send.add_argument("command", choices=("set", "plus", "post", "toggle"))
    send.add_argument("value", nargs="?", help="scalar value (JSON or string)")
    send.add_argument("--json", help="JSON object for any-typed set/post args")
    send.add_argument("--wait", type=int, default=cloud.RESULT_WAIT_SECONDS)
    send.add_argument("--timeout", type=float, default=cloud.RESULT_TIMEOUT)
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
