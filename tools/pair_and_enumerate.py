#!/usr/bin/env python3
"""Pair with a BRAVIA TV over Sony Seeds + gRPC and enumerate its full schema.

This is a development/bootstrap tool, not part of the HA runtime. It reuses the
integration's ported gRPC + Sony Seeds credential modules to:

  1. login      -- start Sony Seeds OAuth (PKCE); prints a URL to open in a
                   browser and log in with the Sony account paired to the TV.
  2. exchange   -- exchange the returned ssh-app://signin?code=... redirect for
                   OAuth tokens, list IoT devices, fetch gRPC session keys, and
                   save a credentials bundle.
  3. enumerate  -- using saved credentials: dump the Sony IoT cloud state
                   snapshot (rich JSON schema), discover the TV gRPC port, run
                   the ControlDeviceService auth handshake, and issue
                   GetStatesWithAuth to confirm live wire values / types.

Outputs land in ../schema/ (git-ignored credentials, committed schema dumps).

Usage:
    python tools/pair_and_enumerate.py login
    python tools/pair_and_enumerate.py exchange "<ssh-app://signin?code=...>"
    python tools/pair_and_enumerate.py enumerate --host 192.0.2.10
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request
import uuid

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "custom_components" / "bravia_tv"

# Load the integration's modules under a synthetic package ``btv`` (NOT via the
# real ``bravia_tv`` package, whose __init__ imports Home Assistant, and NOT by
# putting the ``grpc/`` subdir on sys.path, which would shadow grpcio). A
# synthetic parent with __path__ lets relative imports resolve while ``import
# grpc`` inside the modules still finds the installed grpcio.
import importlib.util  # noqa: E402
import types  # noqa: E402

_btv = types.ModuleType("btv")
_btv.__path__ = [str(_PKG)]
sys.modules["btv"] = _btv


def _load_module(qualname: str, relpath: str):
    spec = importlib.util.spec_from_file_location(qualname, _PKG / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


GRPC_SERVICE = _load_module("btv.const", "const.py").GRPC_SERVICE
cred = _load_module("btv.grpc.credentials", "grpc/credentials.py")
build_get_states_with_auth_request = _load_module(
    "btv.grpc.get_states_request", "grpc/get_states_request.py"
).build_get_states_with_auth_request
sign_get_states_request_body = _load_module(
    "btv.grpc.get_states_auth", "grpc/get_states_auth.py"
).sign_get_states_request_body
parse_get_states_response = _load_module(
    "btv.grpc.get_states_response", "grpc/get_states_response.py"
).parse_get_states_response
_gc = _load_module(
    "btv.grpc.get_capabilities_response", "grpc/get_capabilities_response.py"
)
get_capabilities_method = _gc.get_capabilities_method
parse_capability_index = _gc.parse_capability_index
decode_capabilities_json_text = _gc.decode_capabilities_json_text
paths_for_safe_get_states = _gc.paths_for_safe_get_states

_SCHEMA_DIR = _REPO / "schema"
_STATE_FILE = _SCHEMA_DIR / ".pairing_state.json"  # git-ignored
_CREDS_FILE = _SCHEMA_DIR / ".credentials.json"  # git-ignored


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# ----------------------------------------------------------------------------
# Step 1: login
# ----------------------------------------------------------------------------
def cmd_login(_args: argparse.Namespace) -> None:
    auth_url, code_verifier, state = cred.start_oauth_login()
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


# ----------------------------------------------------------------------------
# Step 2: exchange
# ----------------------------------------------------------------------------
def _sync_exchange_code(auth_code: str, code_verifier: str) -> dict:
    """Sync authorization_code -> token exchange (mirrors the async helper)."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": cred.REDIRECT_URI,
            "client_id": cred.CLIENT_ID,
            "code_verifier": code_verifier,
        }
    ).encode()
    req = urllib.request.Request(
        f"{cred.AUTH_BASE_URL}/token", data=body, headers=cred._TOKEN_HEADERS
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def cmd_exchange(args: argparse.Namespace) -> None:
    state = _load(_STATE_FILE)
    redirect_state = cred.parse_oauth_redirect_state(args.redirect)
    if redirect_state and redirect_state != state["state"]:
        print("ERROR: OAuth state mismatch; re-run login.", file=sys.stderr)
        sys.exit(1)
    auth_code = cred.parse_authorization_code(args.redirect)
    token_response = _sync_exchange_code(auth_code, state["code_verifier"])
    access_token = token_response["access_token"]

    devices = cred.get_devices(access_token).get("devices", [])
    if not devices:
        print("ERROR: no Sony IoT devices on this account.", file=sys.stderr)
        sys.exit(1)
    print(f"\nFound {len(devices)} device(s):")
    for i, d in enumerate(devices):
        print(f"  [{i}] {d.get('device_id')}  {d.get('model') or d.get('name') or ''}")

    idx = args.device_index if args.device_index is not None else 0
    device_id = devices[idx]["device_id"]
    session_keys = cred.get_session_keys(device_id, access_token)
    session_keys.setdefault("device_id", device_id)
    bundle = cred.build_credentials_bundle(session_keys, token_response)
    _save(_CREDS_FILE, bundle)
    print(f"\nSaved credentials for device {device_id} -> {_CREDS_FILE}")
    print("Keys present:", sorted(k for k in bundle if "token" not in k.lower()))
    print("\nNext: python tools/pair_and_enumerate.py enumerate --host <TV_IP>")


# ----------------------------------------------------------------------------
# Step 3: enumerate
# ----------------------------------------------------------------------------
def _grpc_handshake(host: str, port: int, creds: dict):
    """Run ConfirmSignin/ConfirmKeys/GetSessionRandom; return (stub, session)."""
    from btv.grpc.bravia_control_pb2 import (
        ConfirmKeysRequest,
        ConfirmSigninRequest,
        GetSessionRandomRequest,
    )
    from btv.grpc.bravia_control_pb2_grpc import ControlDeviceServiceStub
    import grpc as grpclib

    device_id = creds["device_id"]
    hmac_key = creds.get("hmac_key") or creds.get("hmac_key_hex")
    session_id = creds.get("key_id") or str(uuid.uuid4())

    channel = grpclib.insecure_channel(f"{host}:{port}")
    grpclib.channel_ready_future(channel).result(timeout=5)
    stub = ControlDeviceServiceStub(channel)

    def hmac_key_bytes(h: str) -> bytes:
        return bytes.fromhex(h) if len(h) == 64 else h.encode()[:32].ljust(32, b"\x00")

    # ConfirmSignin: auth_data = SHA256(device_id)
    signin = ConfirmSigninRequest()
    signin.auth_data = hashlib.sha256(device_id.encode()).digest()
    try:
        r = stub.ConfirmSignin(signin, timeout=8)
        print("  ConfirmSignin:", getattr(r, "success", "(no success field)"))
    except grpclib.RpcError as e:
        print("  ConfirmSignin RPC error:", e.code(), e.details())

    # ConfirmKeys: key_data = HMAC(hmac_key, session_id)
    keys = ConfirmKeysRequest()
    keys.session_id = session_id
    keys.key_data = hmac.new(
        hmac_key_bytes(hmac_key), session_id.encode(), hashlib.sha256
    ).digest()
    try:
        r = stub.ConfirmKeys(keys, timeout=8)
        print("  ConfirmKeys:", getattr(r, "success", "(no success field)"))
    except grpclib.RpcError as e:
        print("  ConfirmKeys RPC error:", e.code(), e.details())

    # GetSessionRandom -> session_random (8B) + auth_token (32B)
    sess = stub.GetSessionRandom(
        GetSessionRandomRequest(session_id=session_id), timeout=8
    )
    print(
        f"  GetSessionRandom: random={len(sess.session_random)}B "
        f"token={len(sess.auth_token)}B"
    )
    return channel, {
        "session_id": session_id,
        "session_random": sess.session_random,
        "auth_token": sess.auth_token,
        "hmac_key": hmac_key,
    }


def _dump_cloud_schema(creds: dict) -> dict:
    """Fetch the Sony IoT cloud state snapshot (full JSON field tree)."""
    token = cred.refresh_access_token(creds["refresh_token"])
    access_token = token["access_token"]
    device_id = creds["device_id"]
    states = cred.get_device_states(device_id, access_token)
    _save(_SCHEMA_DIR / "cloud_states.json", states)
    # Flatten to dotted field paths for a compact schema view.
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
    discover_grpc_port = _load_module(
        "btv.grpc_discovery", "grpc_discovery.py"
    ).discover_grpc_port

    port = args.port or discover_grpc_port(
        args.host, scan_range=range(1024, 65536) if args.scan else None
    )
    if not port:
        print("  Could not locate gRPC port (try --scan or --port).", file=sys.stderr)
        return
    print(f"  gRPC control port: {port}")

    print("\n[3/4] gRPC auth handshake...")
    try:
        channel, session = _grpc_handshake(args.host, port, creds)
    except Exception as e:  # noqa: BLE001
        print("  handshake failed:", type(e).__name__, e)
        return

    import grpc as grpclib

    # GetCapabilities is unauthenticated (empty request) and returns the TV's
    # full internal field-path namespace as JSON -- the authoritative local
    # gRPC schema. This is what a BRAVIA Connect packet capture would have
    # recovered, obtained directly and read-only.
    print("\n[4/4] GetCapabilities + GetStatesWithAuth...")
    cap_call = channel.unary_unary(
        get_capabilities_method(),
        request_serializer=lambda p: p if isinstance(p, bytes) else b"",
        response_deserializer=lambda p: p,
    )
    seed_paths: list[str] = []
    try:
        cap_raw = cap_call(b"", timeout=10)
        cap_json = decode_capabilities_json_text(cap_raw)
        index = parse_capability_index(cap_raw)
        if cap_json:
            _save(_SCHEMA_DIR / "grpc_capabilities.json", json.loads(cap_json))
        # Serialize the typed index for a compact, diffable schema view.
        _save(
            _SCHEMA_DIR / "grpc_field_paths.json",
            {
                name: {
                    "type": m.type,
                    "min": m.min,
                    "max": m.max,
                    "values": list(m.values) if m.values else None,
                }
                for name, m in (index or {}).items()
            },
        )
        print(
            f"  GetCapabilities OK: {len(index or {})} field paths "
            "-> schema/grpc_capabilities.json"
        )
        # Only paths the device marks safe for a bulk GetStates batch.
        seed_paths = paths_for_safe_get_states(cap_json) if cap_json else []
        print(f"  GetStates-safe paths: {len(seed_paths)}")
    except grpclib.RpcError as e:
        print("  GetCapabilities RPC error:", e.code(), e.details())

    if not seed_paths:
        print("  No safe paths for GetStates; skipping.")
        return

    call = channel.unary_unary(f"/{GRPC_SERVICE}/GetStatesWithAuth")

    def _try(auth_token: bytes, label: str) -> bool:
        req = build_get_states_with_auth_request(
            seed_paths,
            session_random=session["session_random"],
            session_id=session["session_id"],
            auth_token=auth_token,
        )
        try:
            raw = call(req, timeout=10)
        except grpclib.RpcError as e:
            print(f"  GetStatesWithAuth [{label}] -> {e.code()}")
            return False
        parsed = parse_get_states_response(raw)
        _save(
            _SCHEMA_DIR / "grpc_states.json",
            {"paths": seed_paths, "parsed": str(parsed)[:40000]},
        )
        print(
            f"  GetStatesWithAuth [{label}] OK: {len(raw)} bytes -> schema/grpc_states.json"
        )
        return True

    # Primary: reuse the 32-byte auth_token returned by GetSessionRandom.
    # Fallback: a fresh HMAC over the request preimage (use_signed_auth).
    if not _try(session["auth_token"], "session-token"):
        seed_req = build_get_states_with_auth_request(
            seed_paths,
            session_random=session["session_random"],
            session_id=session["session_id"],
            auth_token=b"\x00" * 32,
        )
        _try(sign_get_states_request_body(session["hmac_key"], seed_req), "signed-hmac")


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
