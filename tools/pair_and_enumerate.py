#!/usr/bin/env python3
"""Pair with a BRAVIA TV over Sony Seeds + gRPC and enumerate its full schema.

This is a development/bootstrap tool, not part of the HA runtime. It uses
``pybravia-connect`` to:

  1. login      -- start Sony Seeds OAuth (PKCE); prints a URL to open in a
                   browser and log in with the Sony account paired to the TV.
  2. exchange   -- exchange the returned ssh-app://signin?code=... redirect for
                   OAuth tokens, list IoT devices, fetch gRPC session keys, and
                   save a credentials bundle.
  3. enumerate  -- using saved credentials: dump the Sony IoT cloud state
                   snapshot, discover the TV gRPC port, connect, and dump
                   GetCapabilities + GetStates.

Outputs land in ../schema/ (git-ignored credentials, committed schema dumps).

Usage:
    python tools/pair_and_enumerate.py login
    python tools/pair_and_enumerate.py exchange "<ssh-app://signin?code=...>"
    python tools/pair_and_enumerate.py enumerate --host 192.0.2.10
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pybravia_connect import (
    BraviaConnectClient,
    build_credentials_bundle,
    discover_grpc_port,
    get_device_states,
    get_devices,
    get_session_keys,
    parse_authorization_code,
    parse_oauth_redirect_state,
    refresh_access_token,
    start_oauth_login,
)
from pybravia_connect.credentials import (  # private bootstrap helpers
    _TOKEN_HEADERS,
    AUTH_BASE_URL,
    CLIENT_ID,
    REDIRECT_URI,
    _sync_post_form,
)

_REPO = Path(__file__).resolve().parent.parent
_SCHEMA_DIR = _REPO / "schema"
_STATE_FILE = _SCHEMA_DIR / ".pairing_state.json"  # git-ignored
_CREDS_FILE = _SCHEMA_DIR / ".credentials.json"  # git-ignored


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def cmd_login(_args: argparse.Namespace) -> None:
    auth_url, code_verifier, state = start_oauth_login()
    _save(_STATE_FILE, {"code_verifier": code_verifier, "state": state})
    print("\n1. Open this URL in a browser and log in with the Sony account")
    print("   that is paired with the TV in the BRAVIA Connect app:\n")
    print(auth_url)
    print(
        "\n2. After login the browser will try to redirect to an ssh-app://"
        " URL and probably show an error -- that is expected. Copy the FULL"
        " ssh-app://signin?code=... URL from the address bar and run:\n"
    )
    print('   python tools/pair_and_enumerate.py exchange "<that url>"\n')


def cmd_exchange(args: argparse.Namespace) -> None:
    state = _load(_STATE_FILE)
    redirect_state = parse_oauth_redirect_state(args.redirect)
    if redirect_state and redirect_state != state["state"]:
        print("ERROR: OAuth state mismatch; re-run login.", file=sys.stderr)
        sys.exit(1)
    auth_code = parse_authorization_code(args.redirect)
    # Bootstrap tool: token exchange helpers are not all on the public root.
    token_response = _sync_post_form(  # noqa: SLF001
        f"{AUTH_BASE_URL}/token",
        {
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": state["code_verifier"],
        },
        _TOKEN_HEADERS,  # noqa: SLF001
    )
    access_token = token_response["access_token"]

    devices = get_devices(access_token).get("devices", [])
    if not devices:
        print("ERROR: no Sony IoT devices on this account.", file=sys.stderr)
        sys.exit(1)
    print(f"\nFound {len(devices)} device(s):")
    for i, d in enumerate(devices):
        print(f"  [{i}] {d.get('device_id')}  {d.get('model') or d.get('name') or ''}")

    idx = args.device_index if args.device_index is not None else 0
    device_id = devices[idx]["device_id"]
    session_keys = get_session_keys(device_id, access_token)
    session_keys.setdefault("device_id", device_id)
    bundle = build_credentials_bundle(session_keys, token_response)
    _save(_CREDS_FILE, bundle)
    print(f"\nSaved credentials for device {device_id} -> {_CREDS_FILE}")
    print("Keys present:", sorted(k for k in bundle if "token" not in k.lower()))
    print("\nNext: python tools/pair_and_enumerate.py enumerate --host <TV_IP>")


def _dump_cloud_schema(creds: dict) -> dict:
    """Fetch the Sony IoT cloud state snapshot (full JSON field tree)."""
    token = refresh_access_token(creds["refresh_token"])
    access_token = token["access_token"]
    device_id = creds["device_id"]
    states = get_device_states(device_id, access_token)
    _save(_SCHEMA_DIR / "cloud_states.json", states)
    paths: dict[str, str] = {}

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{prefix}[{i}]")
        else:
            paths[prefix] = type(node).__name__

    walk(states)
    _save(_SCHEMA_DIR / "cloud_field_paths.json", paths)
    print(f"  Cloud snapshot: {len(paths)} leaf paths -> schema/cloud_field_paths.json")
    return states


def cmd_enumerate(args: argparse.Namespace) -> None:
    creds = _load(_CREDS_FILE)

    print("\n[1/3] Fetching Sony IoT cloud state snapshot...")
    try:
        _dump_cloud_schema(creds)
    except Exception as e:  # noqa: BLE001
        print("  cloud snapshot failed:", type(e).__name__, e)

    print("\n[2/3] Discovering gRPC control port...")
    port = args.port or discover_grpc_port(
        args.host, scan_range=range(1024, 65536) if args.scan else None
    )
    if not port:
        print("  Could not locate gRPC port (try --scan or --port).", file=sys.stderr)
        return
    print(f"  gRPC control port: {port}")

    print("\n[3/3] Connect + GetCapabilities + GetStates...")
    client = BraviaConnectClient(
        args.host,
        port,
        device_id=creds["device_id"],
        hmac_key=creds["hmac_key"],
        key_id=creds.get("key_id"),
        session_key=creds.get("session_key"),
    )
    try:
        client.connect()
        index = client.get_capabilities()
        # Library already parsed; re-dump a compact typed index for schema/.
        _save(
            _SCHEMA_DIR / "grpc_field_paths.json",
            {
                name: {
                    "type": m.type,
                    "min": m.min,
                    "max": m.max,
                    "values": list(m.values) if m.values else None,
                }
                for name, m in index.items()
            },
        )
        print(
            f"  GetCapabilities OK: {len(index)} field paths "
            "-> schema/grpc_field_paths.json"
        )
        states = client.get_states()
        _save(_SCHEMA_DIR / "grpc_states.json", {"parsed": states})
        print(f"  GetStates OK: {len(states)} fields -> schema/grpc_states.json")
    except Exception as e:  # noqa: BLE001
        print("  enumerate failed:", type(e).__name__, e)
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login").set_defaults(func=cmd_login)

    ex = sub.add_parser("exchange")
    ex.add_argument("redirect", help="ssh-app://signin?code=... redirect URL")
    ex.add_argument("--device-index", type=int, default=None)
    ex.set_defaults(func=cmd_exchange)

    en = sub.add_parser("enumerate")
    en.add_argument("--host", required=True, help="TV IP address")
    en.add_argument("--port", type=int, default=None)
    en.add_argument("--scan", action="store_true", help="full port scan if needed")
    en.set_defaults(func=cmd_enumerate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
