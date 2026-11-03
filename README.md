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
- **Cloud-only controls, via the Sony cloud.** Some TV functions — the sleep
  timer, Picture Off, playback transport and seek, channel by number, deep
  links into apps, Living Decor themes, on-screen search and waking the TV
  from standby — exist only on the capability set the TV registers with
  Sony's cloud, the channel the BRAVIA Connect app itself uses for them. They
  are offered through the same Sony sign-in, with the TV's result read back
  after each command, and need internet access. See
  [Cloud-backed controls](#cloud-backed-controls).

## Entities

Generated from the device capability schema; the exact set depends on the model
and current context. On a BRAVIA 8 II this is roughly 40 entities, a few of
them disabled by default.

- **`media_player`** (the primary device entity) — power, volume (set / step),
  mute, and **source selection** across HDMI/external inputs and installed apps
  (Netflix, Twitch, Disney+, …). Includes a browsable grid of installed apps
  with icons (tap to launch) and now-playing artwork for the foreground app.
  Source reflects the active input, or the foreground app when one is running.
  Play / pause / stop / next / previous and seek are offered when the TV
  advertises playback control to the Sony cloud (see
  [Cloud-backed controls](#cloud-backed-controls)).
- **`remote`** — `remote.send_command` for the TV's keys: Sony on-screen
  shortcut menus (Quick Settings, Audio, Guide, App Menu, Menu, TV), D-pad
  navigation (up/down/left/right/center/back/home), channel up/down and the
  power key. `hold_secs` is honoured for the D-pad keys, back, home, menu and
  power: under a second is a short press, a second or more is a long press
  (the TV completes it itself, e.g. long-press centre on the launcher opens
  an app's card menu).
- **`select`** — Picture Mode (60+ options incl. Custom for Pro 1/2, calibrated,
  Dolby Vision variants), Content Mode, Theatre Mode, two audio-output group
  selects: Speakers (TV / audio system) and Bluetooth, Sleep Timer
  (cloud-backed) and Speaker Type (cloud-backed, disabled by default as it
  overlaps Speakers). Switching an audio output reads the TV's activation
  result back and raises an error if the switch did not take.
- **`number`** — Volume, Brightness, TV Speaker / Bluetooth / HDMI-output /
  Wired Headphone (disabled by default on models without a jack)
  volumes, Voice Zoom, and Rear Speaker / Subwoofer levels for **Direct Connect**
  wireless BRAVIA Theatre speakers paired straight to the TV. On a new install
  the rear / subwoofer levels start **disabled** if the TV reports no such
  speaker was ever paired; if you pair them later, enable the two entities under
  **Settings → Devices & Services → Entities**.
- **`switch`** — Power, Mute, Bluetooth 3D Surround, Calibrated Picture Mode,
  Picture (cloud-backed: off = the TV's *Picture Off*, screen dark with audio
  playing) and Accessibility Service (cloud-backed, disabled by default; see
  below). Power-on falls back to the Sony cloud when the local write fails,
  which wakes a TV in standby.
- **`sensor`** — Video Signal (e.g. “4K 24fps Dolby Vision”), Audio Signal (e.g.
  “5.1 Surround Dolby Atmos”, or “PCM” for app audio), App (foreground app /
  active input), App List (installed apps), Bluetooth Device (connected audio
  device), Restricted Controls (which controls are blocked and why), and the
  connection status of any **Direct Connect** rear speakers / subwoofer.
- **`binary_sensor`** — Wi-Fi, Error Status (speaker protection faults),
  Multiview, Remote Start, and HDMI Linked Device (a Sony/eARC device such as a
  soundbar).
- **`text`** — the TV's friendly name.
- **`update`** — firmware update availability, with the pending version's
  release notes fetched from Sony's page when one is available.

## Cloud-backed controls

The TV advertises two capability sets: the local `GetCapabilities` schema
(118 paths on a BRAVIA 8 II) that everything above is built from, and a much
smaller set it registers with Sony's IoT cloud (23 on the same TV; see
[`schema/SCHEMA.md`](schema/SCHEMA.md)). Most cloud entries mirror local paths,
but a handful exist **only** in the cloud set and have no local gRPC route at
all:

| Cloud capability | Entity | Notes |
|---|---|---|
| `sleep_timer` | `select` Sleep Timer | off / 15 / 30 / 45 / 60 / 90 / 120 min; the TV clears it when powered off |
| `picture` | `switch` Picture | off = *Picture Off* (panel dark, audio continues) |
| `playback` | `media_player` play / pause / stop / next / previous | drives the foreground app's media session |
| `seek` | `media_player` seek | absolute position in seconds; needs an app with an active media session |
| `channel` | `media_player.play_media` with `media_content_type: channel` | tunes the built-in tuner to a channel number |
| `application.deep_link` | `media_player.play_media` with `media_content_type: url` | opens the URL in its app (YouTube, Netflix, Disney+, Prime Video, Spotify, Twitch, Apple TV recognised by host; otherwise pass `extra: {package_name: …}`) |
| `select_media` | `media_player.play_media` with `media_content_type: theme` | shows a Living Decor theme, e.g. `theme://skyview` (the ids are not listed by the TV; `theme://skyview`, `theme://naturalandscape`, `theme://monochrome` on a BRAVIA 8 II) |
| `search` | `bravia_tv_grpc.search` service on the media player | shows Sony's on-screen search results for a query (`play: true` asks the TV to play the best match); takes over the screen and audio focus |
| `speaker_type` | `select` Speaker Type | TV / audio system (disabled by default) |
| `access_shortcut` | `switch` Accessibility Service | turns the accessibility service assigned to the TV's shortcut (TalkBack by default) on or off. Works only after the shortcut has been set up once on the TV by holding **Mute** for 3 seconds on the remote; until then the TV rejects it with a toast. Optimistic (the TV does not report it) and disabled by default |
| `power` | power-on fallback | used only when the local write fails, e.g. the TV is in standby |

These are sent through the same Sony account the integration already signs in
with: a command is posted to Sony's IoT API, Sony delivers it to the TV, and
the TV's own result (including the new state) is read back before the entity
updates — a rejected command surfaces as an error rather than a silent no-op.
The TV only acts on them while powered on, so the entities show as
unavailable when it is off. They need internet access and Sony's cloud to be
up; everything else in the integration stays local-push. Because the TV
pushes no changes for these, the cloud snapshot is also polled every five
minutes so a sleep timer set with the TV remote shows up within that time.
`tools/cloud_command_probe.py` exercises any cloud capability from the
command line.

Verified on a BRAVIA 8 II: the sleep timer, Picture Off, playback, seek,
deep links, Living Decor themes, search, speaker type and the standby
power-on all execute and report back through the cloud within a second or
two (seek and the transport controls were checked against a playing YouTube
video). Channel tuning reaches the tuner but was not tested with channels
installed, and release notes depend on Sony's page for a pending update. On
other models, `tools/cloud_command_probe.py caps` shows what the TV
advertises and `send <name> set <value>` exercises one capability.

## BRAVIA TV agent (optional companion app)

Some things the TV never exposes over gRPC, the cloud or REST: the ambient
light sensor, the ambient temperature, what is actually playing, the
remote's battery, and whether the panel is lit. The optional
[BRAVIA TV agent](https://github.com/braviafanboy/bravia-tv-agent) is a small
sideloaded Android TV app that reads those on the TV and serves them to this
integration over the LAN. Nothing changes without it; with it, the TV device
gains:

| Entity | Source on the TV | Notes |
|---|---|---|
| `sensor` Illuminance (lx) | Android light sensor | The same reading the TV's own Auto luminance level uses |
| `sensor` Ambient temperature (°C) | Android ambient temperature sensor | |
| `sensor` Remote battery (%) | The remote's Bluetooth battery service | Diagnostic |
| `binary_sensor` Remote connected | Bluetooth connection state | Diagnostic |
| `binary_sensor` Panel | Display state | Off during *Picture Off* while the TV stays on |
| `media_player` now playing | The foreground app's media session | Title, artist, album, position, duration, artwork and playing / paused state |
| `select` Living Decor theme | The Living Decor app's theme list | Disabled by default; needs the cloud `select_media` capability |
| `sensor` Wi-Fi signal / link speed | WifiManager | Diagnostic, disabled by default |

### Installing the agent

The agent is installed once with adb, Android's own developer tool; the TV
has no built-in way to fetch and install an app, and Android requires the
install to be confirmed on the TV in any case. Everything after that is
automatic.

1. **Enable network debugging on the TV** (once). Settings → System → About →
   *Android TV OS build*: select it seven times to unlock *Developer options*.
   Then Settings → System → Developer options → turn on *Network debugging*
   (also called *ADB debugging* / *Wireless debugging* depending on firmware).
   Note the TV's IP address from Settings → Network & Internet.
2. **Install from a computer** with the Android platform tools
   ([download](https://developer.android.com/tools/releases/platform-tools)):

   ```
   adb connect <tv-ip>
   adb install bravia-agent.apk
   ```

   The TV shows an *Allow debugging?* prompt on the first connection; accept
   it with the remote. `bravia-agent.apk` is the file attached to the agent's
   [releases](https://github.com/braviafanboy/bravia-tv-agent/releases).
3. **That is it for the sensors.** Within five minutes the integration finds
   the agent on the TV's address, starts it (nothing appears on screen), pairs
   with it over the Sony cloud, or through a 60-second pairing window when the
   cloud is unreachable, and the illuminance, temperature, panel, Wi-Fi and
   settings entities appear. Pairing is remembered on both sides; a reinstalled
   agent is re-paired automatically.
4. **Three optional permissions** unlock the rest. Now-playing needs
   *Notification access*, the remote's battery needs *Bluetooth*, and updating
   the agent from Home Assistant needs *Install unknown apps*; Android only
   grants these through its own prompts on the TV. Press the **Agent
   setup** button on the TV's device page in Home Assistant, or open the
   BRAVIA agent app from the TV's launcher and press OK, then accept the
   prompts with the remote; the agent closes again once nothing is missing.
   If everything is already granted, the button says so in Home Assistant and
   does not touch the TV. A repair notice reminds you while now-playing or
   the remote's battery is missing a grant. While
   adb is still connected, the same two grants are:

   ```
   adb shell cmd notification allow_listener com.braviafanboy.braviaagent/.MediaListenerService
   adb shell pm grant com.braviafanboy.braviaagent android.permission.BLUETOOTH_CONNECT
   ```

You can turn network debugging off again afterwards; the agent does not use
it. **Updates come through Home Assistant**: an update entity for the agent
sits next to the TV firmware one, reads the agent's release manifest, and
installing it downloads the APK on the TV and asks for one confirmation on
screen. The manifest URL can be changed in the integration's options, for
example to a file Home Assistant serves itself.

How it works:

- The integration probes the TV's address on port 8585 at startup and every
  five minutes until an agent answers.
- Pairing hands the agent a bearer token, by Sony cloud deep link when the
  cloud is reachable, else by launching the agent so it opens a pairing
  window. The token is stored in the config entry; the agent accepts nothing
  else afterwards.
- The link is HTTPS. The agent generates a self-signed certificate on first
  run; the integration learns it during pairing and from then on trusts
  exactly that certificate, so nothing else on the network can pose as the
  agent and the token never travels in clear. A reinstalled agent has a new
  certificate and a new pairing follows automatically.
- State arrives over the agent's event stream and is unavailable whenever
  the TV is off, since the TV leaves the LAN in standby.

Diagnostics include the agent's version and granted permissions. If you
reinstall the agent or clear its pairing (hold LEFT on its status screen), the
integration re-pairs on its own the next time the TV is on.

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

## Compatible TVs

This integration speaks the same local control plane as Sony's **BRAVIA Connect**
app, so it works with the TVs that app supports — Sony's recent BRAVIA lineup
(roughly 2023 onward). Older Sony TVs use the previous *Video & TV SideView* app
and are **not** compatible.

Supported model families (exact model codes and availability vary by region;
Sony's [compatible-models list](https://www.sony-asia.com/electronics/support/articles/00305900)
is authoritative):

- **BRAVIA 9** / **9 II** (XR90)
- **BRAVIA 8** / **8 II** (XR80 / XR8)
- **BRAVIA 7** / **7 II** (XR70)
- **BRAVIA 5** (XR50)
- **BRAVIA 3** / **3 II** (S30)
- **BRAVIA 2 II** (S20)
- **A95L** series

> ⚠️ **Only the BRAVIA 8 II (K-65XR8M2) has been tested.** Because entities are
> generated from whatever the TV advertises via `GetCapabilities`, other models
> in the list should pair and work too — but that is unverified. If you try
> another model, please [open an issue](https://github.com/braviafanboy/bravia-tv-grpc-homeassistant/issues)
> reporting how it went.

## Installation

### Prerequisites

- **The TV must already be signed in to a Sony account in the BRAVIA Connect
  app** (Sony's official mobile app). The integration derives its local keys
  from that account, so if you've never paired the TV in BRAVIA Connect, do that
  first — and know which Sony account it uses.
- Home Assistant and the TV should be on the **same subnet** (for mDNS discovery
  and the local gRPC connection). Reserve a **static IP** for the TV on your
  router so it doesn't move.
- **Home Assistant 2025.11 or newer.**
- **Internet access** is needed only for the
  [cloud-backed controls](#cloud-backed-controls); everything else works on
  the local network.
- `grpcio` and `protobuf` are installed automatically (declared in the manifest);
  no manual dependency setup is required.

### 1. Add the repository (HACS)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=braviafanboy&repository=bravia-tv-grpc-homeassistant&category=integration)

Click the badge above (it opens HACS on your own Home Assistant), or:

1. In HACS, search for **Bravia TV (gRPC)** and download it.
2. **Restart Home Assistant**.

### 2. Add your TV

After the restart, this badge starts the setup flow directly:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=bravia_tv_grpc)

Or by hand:

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

### Session handling

The TV keeps the gRPC session in memory and forgets it when the Sony session
keys lapse (24 hours) or when its control service restarts. The integration
renews the keys an hour before they expire and re-runs the handshake in place
when the TV rejects a session, so neither case interrupts control or marks
entities unavailable.

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
- **A cloud-backed control reports "Could not send … through the Sony
  cloud"** — the TV needs internet access and Sony's cloud has to be
  reachable; the local entities keep working regardless. "The TV rejected the
  cloud command" means the TV received it but could not act on it in its
  current state (for example seek with no media playing, or the Accessibility
  Service before its one-time setup).

## Blueprints

HACS only installs the integration, so blueprints are imported separately.
This badge imports the blueprint into your Home Assistant:

[![Open your Home Assistant instance and show the blueprint import dialog with this blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fbraviafanboy%2Fbravia-tv-grpc-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbraviafanboy%2Fbravia_sound_by_audio_format.yaml)

Or copy
[`bravia_sound_by_audio_format.yaml`](blueprints/automation/braviafanboy/bravia_sound_by_audio_format.yaml)
into `config/blueprints/automation/braviafanboy/` yourself. Either way it appears
under **Settings → Automations & Scenes → Blueprints**.

- **Sound Settings by Audio Format** — runs sound settings (for example a
  soundbar's Sound Field or Night Mode) when the **Audio Signal** sensor matches
  the formats you list, such as `Dolby Atmos` or `PCM`. Create one automation
  per format profile.

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
    nonce-gated, AES-GCM app-list / resource readers, plus `cloud_commands.py`,
    the Sony cloud command client. The wire-decode and cloud modules are pure
    (no Home Assistant, no grpcio) and are what the unit tests exercise.
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
  running HA instance. Set `BRAVIA_HOST` to the TV's address; its optional
  push test also needs `BRAVIA_REST_PSK`, the pre-shared key of the TV's REST
  IP control (Settings → Network → Home network setup → IP control), which
  it uses to change brightness through that second channel and watch the
  change arrive over gRPC. The harness talks HTTPS on port 443, which the TV
  serves (with its own self-signed certificate, so not verified) once its
  IP-control HTTPS setting is on; that setting also closes plain HTTP on port
  80. Set `BRAVIA_REST_SCHEME=http` for a TV without it, where the key then
  crosses the LAN in clear. The test is skipped when the key is unset.
- `tools/cloud_command_probe.py` — sends one Sony **cloud command** (sleep
  timer, picture off, playback, …) with the credentials saved by
  `pair_and_enumerate.py` and prints every HTTP exchange plus the TV's result,
  for verifying the cloud channel without a packet capture.

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
