# Bravia TV (gRPC) — Home Assistant integration

A Home Assistant custom integration for controlling Sony BRAVIA TVs over the
**Sony ControlDeviceService gRPC API** — the same local control plane used by
the BRAVIA Connect mobile app. Unlike the HTTP REST API, this protocol is
**push-capable** (`StartNotifyStates`), so Home Assistant reflects changes made
on the TV instantly, without polling.

Bootstrapped from the excellent
[bravia-quad-homeassistant](https://github.com/steamEngineer/bravia-quad-homeassistant)
soundbar integration, whose gRPC + Sony Seeds auth machinery is reused. BRAVIA
TVs, however, expose a different field schema, a dynamically-assigned gRPC port,
and different value encodings — all handled here.

## Capabilities

- **Local push, no polling.** State changes stream from the TV over gRPC
  `StartNotifyStates`, so an adjustment made with the TV's own remote or the
  BRAVIA Connect app appears in Home Assistant within a second. The only
  recurring request is a lightweight liveness probe; a full `GetStates` read
  seeds state at startup and reconciles anything missed across a reconnect.
- **Auto-discovery.** The TV is found on the network via mDNS
  (`_sonysmarthome._tcp`) and its dynamically-assigned gRPC port is resolved
  automatically — no IP or port configuration needed in the common case.
- **Capability-driven entities.** Entities are generated from the TV's own
  `GetCapabilities` schema, so only features the specific model actually
  advertises are created. Different BRAVIA models expose different controls, and
  the integration adapts to each rather than assuming a fixed set.
- **Context-aware availability.** Controls that aren't usable in the current
  context — e.g. the TV-speaker volume while a soundbar is the active output, or
  a picture setting locked by the foreground app — are shown as *unavailable*
  rather than silently doing nothing. A **Restricted Controls** diagnostic
  sensor lists exactly which controls are currently blocked and why.
- **Installed apps as sources, with icons.** The installed-app list (a
  nonce-gated, AES-256-GCM-encrypted read on the TV) is decoded to expose your
  real installed apps as `media_player` sources. App icons are served both as a
  browsable grid and as now-playing artwork for the foreground app, and the list
  refreshes automatically when apps are installed or removed.
- **Reboot & reconnect resilient.** A TV reboot changes the dynamic gRPC port
  and invalidates the Sony session keys; the integration re-resolves the port,
  refreshes the keys, and self-heals the push stream on its own. Transient
  network or TV-side hiccups are retried with backoff — only a genuinely expired
  Sony sign-in prompts re-authentication.
- **Signal reporting.** Live video (resolution / HDR format / frame rate) and
  audio (codec / channel layout, or PCM for app-decoded audio, including when
  routed to a soundbar over eARC).
- **Energy-prerequisite handling.** EU energy-saving settings gated behind the
  Sony *lot5* agreement are confirmed automatically before a write, so picture,
  content and theatre modes actually apply.
- **Diagnostics & recovery.** Redacted config-entry / device diagnostics, plus
  standard Home Assistant repair and re-authentication prompts when the TV needs
  attention (see [Troubleshooting](#troubleshooting)).

## Entities

Generated from the device capability schema; the exact set depends on the model
and current context. On a BRAVIA 8 II this is roughly 35 entities.

- **`media_player`** (the primary device entity) — power, volume (set / step),
  mute, and **source selection** across HDMI/external inputs and installed apps
  (Netflix, Twitch, Disney+, …). Includes a browsable grid of installed apps
  with icons (tap to launch) and now-playing artwork for the foreground app.
  Source reflects the active input, or the foreground app when one is running.
- **`remote`** — `remote.send_command` for the TV's keys: Sony on-screen
  shortcut menus (Quick Settings, Audio, Guide, App Menu, Menu, TV), D-pad
  navigation (up/down/left/right/center/back/home) and channel up/down.
- **`select`** — Picture Mode (60+ options incl. Custom for Pro 1/2, calibrated,
  Dolby Vision variants), Content Mode, Theatre Mode, and two audio-output group
  selects: Speakers (TV / audio system) and Bluetooth.
- **`number`** — Volume, Brightness, TV Speaker / Bluetooth / HDMI-output
  volumes, Voice Zoom, and Rear Speaker / Subwoofer levels for **Direct Connect**
  wireless BRAVIA Theatre speakers paired straight to the TV.
- **`switch`** — Power, Mute, Bluetooth 3D Surround, Calibrated Picture Mode.
- **`sensor`** — Video Signal (e.g. “4K 24fps Dolby Vision”), Audio Signal (e.g.
  “5.1 Surround Dolby Atmos”, or “PCM” for app audio), App (foreground app /
  active input), App List (installed apps), Bluetooth Device (connected audio
  device), Restricted Controls (which controls are blocked and why), and the
  connection status of any **Direct Connect** rear speakers / subwoofer.
- **`binary_sensor`** — Wi-Fi, Error Status (speaker protection faults),
  Multiview, Remote Start, and HDMI Linked Device (a Sony/eARC device such as a
  soundbar).
- **`text`** — the TV's friendly name.
- **`update`** — firmware update availability.

## Soundbar control is out of scope

Control of a connected Sony soundbar — e.g. the **BRAVIA Theatre Quad**
(HT-A9M2) — is intentionally **not** implemented here. A soundbar's own settings
(its volume, sound fields and audio processing) are already covered by the
dedicated
[bravia-quad-homeassistant](https://github.com/steamEngineer/bravia-quad-homeassistant)
integration, which talks to the soundbar directly; duplicating them here would
create two conflicting sets of controls for the same device. Even when the TV
and soundbar are combined in BRAVIA Connect and the TV's gRPC schema surfaces
soundbar-adjacent fields, those are left to the Quad integration.

This integration still covers the TV side of audio: **selecting** the soundbar
as the TV's audio output (the Speakers / Bluetooth selects) and the TV's own
**Direct Connect** wireless rear speakers / subwoofer.

## Installation

### Prerequisites

- **The TV must already be signed in to a Sony account in the BRAVIA Connect
  app** (Sony's official mobile app). The integration derives its local keys
  from that account, so if you've never paired the TV in BRAVIA Connect, do that
  first — and know which Sony account it uses.
- Home Assistant and the TV should be on the **same subnet** (for mDNS discovery
  and the local gRPC connection). Reserve a **static IP** for the TV on your
  router so it doesn't move.
- `grpcio` is installed automatically (declared in the manifest); no manual
  dependency setup is required.

### 1. Add the repository (HACS)

1. HACS → ⋮ (top-right) → **Custom repositories** → paste this repository's URL,
   choose category **Integration**, and **Add**.
2. Search for and install **Bravia TV (gRPC)**, then **restart Home Assistant**.

### 2. Add your TV

3. Go to **Settings → Devices & Services**.
   - If the TV was **auto-discovered**, a *Bravia TV (gRPC)* card appears — click
     **Configure**.
   - Otherwise click **+ Add Integration**, search **Bravia TV (gRPC)**, and
     enter the TV's **IP address**. The gRPC port is found automatically.

### 3. Sign in with your Sony account

The integration needs local session keys, obtained through a one-time Sony
sign-in. The config dialog shows a **"Sign in to Sony"** step with a link and a
**Redirect URL** box.

4. **Open the sign-in link in a desktop web browser.**
   > ⚠️ Do **not** open it on the phone that has BRAVIA Connect installed — the
   > final redirect there launches the app instead of giving you the value you
   > need to paste back. Use a computer's browser.
5. Sign in with the **same Sony account** your TV is paired to in BRAVIA Connect,
   and approve access.
6. When sign-in finishes, the browser is redirected to an address starting with
   **`ssh-app://signin?code=…`**. Only the BRAVIA Connect phone app can open that
   scheme, so on a computer the page **looks like it failed** (an error page, or
   a prompt to open an application). **This is expected** — you just need to copy
   that full `ssh-app://…` address and paste it into the **Redirect URL** box.

   **Chrome / Edge**
   - It may leave the `ssh-app://…` address in the address bar, or show a
     *"This site can't be reached"* page — if so, copy it from there.
   - If not (the address bar reverts), grab it from DevTools:
     1. **Before** you sign in, press **F12** (or **⌘/Ctrl + Shift + I**), open
        the **Network** tab, and tick **Preserve log**.
     2. Complete the sign-in.
     3. Click the last request with a **`302`** status (the final Sony redirect),
        open **Headers**, and under **Response Headers** copy the **`Location`**
        value — it is your `ssh-app://signin?code=…` address.

   **Firefox**
   - Firefox shows a **"Launch Application"** pop-up for `ssh-app` — click
     **Cancel**. The address may be left in the address bar; copy it if so.
   - If not, grab it from DevTools:
     1. **Before** you sign in, press **F12**, open the **Network** tab (logging
        is on by default).
     2. Complete the sign-in and **Cancel** the launch pop-up.
     3. Click the last **`302`** request, and copy the **`Location`** response
        header (its **Headers** panel) — your `ssh-app://signin?code=…` address.

   > Tip: pasting just the `code=…` value works too — the integration accepts
   > either the full redirect URL or the bare code.

7. Paste the value into **Redirect URL** and submit. The entry is created and the
   TV's entities appear.

> Nothing is sent to Sony beyond the standard OAuth sign-in. The sign-in mints
> **local** gRPC session keys for the TV; those keys are stored in the config
> entry and all subsequent control happens on your LAN.

### Troubleshooting

- **"Could not find the gRPC control service"** — confirm the TV is powered on
  and on the same subnet as Home Assistant. The gRPC port is dynamic and is
  normally discovered automatically; if your network blocks mDNS *and* the port
  scan, open the integration's **⋮ → options** (or **Configure**) and set the
  gRPC port manually (`0` restores auto-discovery). A repair notification is
  raised when the port can't be reached.
- **"Pairing failed" / no devices** — make sure you signed in with the exact
  Sony account the TV is registered to in BRAVIA Connect.
- **"The Sony login redirect could not be validated"** — the pasted value was
  incomplete or from a different login attempt; restart the step and paste the
  whole `ssh-app://…` address.
- **Entities go unavailable after a TV reboot** — no action needed; the
  integration re-discovers the new port and refreshes its keys on its own. Allow
  a minute or two while the TV boots (see *Reboot & reconnect resilient* above).

## Contributing

Issues and pull requests are welcome. A few guidelines:

- **Run the checks before opening a PR.** `ruff check .`, `ruff format .` and
  `pytest tests/` should all pass (see [Testing](#testing)). CI runs Ruff,
  Hassfest, HACS validation and the test suite on every push and PR.
- **State the TV model** you developed/tested against — capability schemas
  differ between BRAVIA models, so this matters for reviewing a change.
- **Prefer capability-driven code.** Entities are generated from the TV's
  `GetCapabilities` schema; extend the mapping tables rather than hardcoding
  model-specific lists, so other models keep working.
- **Verify every write against a real TV.** A field appearing settable in the
  capability schema does *not* guarantee it is writable in the current context —
  the TV frequently returns an empty, success-looking response while silently
  ignoring the change. Confirm a control with a real change + read-back before
  shipping it, and gate it on its `.availability` / `.visibility` /
  `.unavailable_reason` companions.
- **Leave soundbar control to the Quad integration** — see
  [Soundbar control is out of scope](#soundbar-control-is-out-of-scope).

## Development

### Project layout

- `custom_components/bravia_tv_grpc/`
  - `grpc/` — the protocol layer: the Sony Seeds auth handshake, protobuf wire
    codecs (`wire.py`, `notify_decode.py`, `get_states_response.py`, …) and the
    `GetStates` / `StartNotifyStates` / `GetCapabilities` readers plus the
    nonce-gated, AES-GCM app-list / resource readers. The wire-decode modules
    are pure (no Home Assistant, no grpcio) and are what the unit tests exercise.
  - `coordinator.py`, `entity.py`, `config_flow.py`, and the platform modules
    (`media_player.py`, `select.py`, `number.py`, `switch.py`, `sensor.py`,
    `binary_sensor.py`, `text.py`, `update.py`, `remote.py`).
- `tests/` — unit tests (see [Testing](#testing)).
- `tools/` — developer tooling that runs **outside** Home Assistant.
- `schema/` — captured capability / state dumps for reference.

### Setup

```sh
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

Home Assistant (pulled in by `pytest-homeassistant-custom-component`) does not
build on Python 3.14 yet, so use **Python 3.13** for the full test suite. The
pure wire-decode tests run on any Python.

### Tools

- `tools/pair_and_enumerate.py` — drives Sony sign-in and gRPC **schema
  enumeration** outside Home Assistant; the starting point for adding a new
  model.
- `tools/ha_stub_harness.py` — exercises the real coordinator and entities
  against a **live TV** using lightweight Home Assistant stand-ins, without a
  running HA instance. Edit the `REPO` path at the top to your checkout.

### Regenerating the protobuf stubs

`grpc/bravia_control_pb2*.py` are generated from `grpc/bravia_control.proto`:

```sh
python -m grpc_tools.protoc -Igrpc --python_out=grpc --grpc_python_out=grpc \
  grpc/bravia_control.proto
```

Then re-apply two manual patches (also documented in `requirements-dev.txt`):

1. Make the `pb2_grpc` import relative — `from . import bravia_control_pb2`.
2. Register the descriptor in a **private** pool, not the global `Default()`
   one (`_pool = _descriptor_pool.DescriptorPool()` /
   `DESCRIPTOR = _pool.AddSerializedFile(...)`). This prevents a symbol-name
   collision in the shared protobuf descriptor pool with a co-installed
   `bravia_quad` integration, which uses the same proto package — without it,
   whichever integration loads second fails to import.

## Testing

Tests live in `tests/` and come in two tiers:

- **Pure wire-decode tests** (`test_notify_decode`, `test_get_states_response`,
  `test_application_list`, `test_resources`, `test_util`, `test_credentials`)
  exercise the protocol codecs with no Home Assistant and no network. They run
  on **any** Python via a synthetic package set up in `conftest.py`.
- **Home Assistant tests** (`test_config_flow`, `test_coordinator`, `test_init`,
  `test_entity`, `test_sensor`) use `pytest-homeassistant-custom-component` and
  cover the config/reauth flow, coordinator seeding + push handling, setup
  recovery, and entity behaviour. They are `importorskip`-guarded, so on a
  Python without the HA plugin they skip and the pure tests still run.

```sh
pytest tests/ -q          # run the suite
ruff check .              # lint (matches CI)
ruff format --check .     # formatting (matches CI)
```

CI (`.github/workflows/validate.yml`) runs four jobs on every push and PR:
**Ruff** (lint + format check), **Hassfest** (Home Assistant manifest
validation), **HACS** (repository validation) and **Tests**.

## License

Released under the [MIT License](LICENSE), matching the upstream
`bravia-quad-homeassistant` project it derives from (credited above).
