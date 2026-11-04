# Entities

The full list of what the integration creates. Entities are generated from the
TV's own capability schema, so the exact set depends on the model and on what
is connected. On a BRAVIA 8 II it is about 40 entities from the TV itself,
plus the HA TV Agent's entities and picture settings when the agent is
installed. A few are disabled by default.

## Local (gRPC)

These come from the TV's local gRPC control service and update by push.

- **`media_player`** (the primary device entity): power, volume (set and
  step), mute, and **source selection** across HDMI and external inputs and
  installed apps. It includes a browsable grid of installed apps with icons
  (tap to launch). Source reflects the active input, or the foreground app
  when one is running. The app's icon is the artwork unless the HA TV Agent
  provides now-playing artwork.
- **`remote`**: `remote.send_command` for the TV's keys: Sony's on-screen
  shortcut menus (Quick Settings, Audio, Guide, App Menu, Menu, TV), D-pad
  navigation (up, down, left, right, center, back, home), channel up and down,
  and the power key. `hold_secs` is honoured for the D-pad keys, back, home,
  menu and power: under a second is a short press, a second or more is a long
  press.
- **`select`**: Picture Mode (60+ options, including Custom for Pro 1/2,
  calibrated and Dolby Vision variants), Content Mode, Theatre Mode, and two
  audio-output selects, Speakers (TV or audio system) and Bluetooth. Switching
  an audio output reads the TV's result back and raises an error if the
  switch did not take.
- **`number`**: Volume, Brightness, the TV speaker, Bluetooth, HDMI-output
  and wired headphone volumes (headphone disabled by default on models
  without a jack), Voice Zoom, and the rear speaker and subwoofer levels for
  **Direct Connect** wireless BRAVIA Theatre speakers paired straight to the
  TV. On a new install the rear and subwoofer levels start disabled if the TV
  reports that no such speaker was ever paired. Enable them under
  **Settings → Devices & Services → Entities** after pairing.
- **`switch`**: Power, Mute, Bluetooth 3D Surround and Calibrated Picture
  Mode.
- **`sensor`**: Video Signal (for example "4K 24fps Dolby Vision"), Audio
  Signal (for example "5.1 Surround Dolby Atmos", or "PCM" for app audio), App
  (foreground app or active input), App List, Bluetooth Device, Restricted
  Controls (which controls are blocked right now and why), and the connection
  status of Direct Connect rear speakers and subwoofer.
- **`binary_sensor`**: Wi-Fi, Error Status (speaker protection faults),
  Multiview, Remote Start, and HDMI Linked Device (a Sony or eARC device such
  as a soundbar).
- **`text`**: the TV's friendly name.
- **`update`**: firmware update availability, with the pending version's
  release notes from Sony's page when one is available.

A control that cannot be used in the current context, such as the TV speaker
volume while a soundbar is the output, is shown as unavailable rather than
silently doing nothing.

## Sony cloud

The TV registers a second, smaller capability set with Sony's cloud (23
entries on a BRAVIA 8 II, against 118 local paths; see
[`schema/SCHEMA.md`](../schema/SCHEMA.md)). Most mirror local paths, but a
handful exist only there, with no local route at all. The integration sends
these through the same Sony account it signs in with, the way the BRAVIA
Connect app does. Each command is posted to Sony's cloud, delivered to the TV,
and the TV's own result is read back before the entity updates, so a rejected
command is an error rather than a silent no-op.

| Cloud capability | Entity | Notes |
|---|---|---|
| `sleep_timer` | `select` Sleep Timer | Off, 15, 30, 45, 60, 90 or 120 minutes; the TV clears it when powered off |
| `picture` | `switch` Picture | Off is the TV's *Picture Off*: panel dark, audio continues |
| `playback` | `media_player` play, pause, stop, next, previous | Drives the foreground app's media session |
| `seek` | `media_player` seek | Absolute position in seconds; needs an app with an active media session |
| `channel` | `media_player.play_media`, `media_content_type: channel` | Tunes the built-in tuner to a channel number |
| `application.deep_link` | `media_player.play_media`, `media_content_type: url` | Opens the URL in its app. YouTube, Netflix, Disney+, Prime Video, Spotify, Twitch and Apple TV are recognised by host; otherwise pass `extra: {package_name: …}` |
| `search` | `bravia_tv_grpc.search` service | Shows Sony's on-screen search results for a query; `play: true` plays the best match |
| `speaker_type` | `select` Speaker Type | TV or audio system; disabled by default as it overlaps Speakers |
| `access_shortcut` | `switch` Accessibility Service | Turns the service on the TV's accessibility shortcut (TalkBack by default) on or off. Works only after the shortcut has been set up once by holding **Mute** for 3 seconds. Optimistic and disabled by default |
| `power` | Power-on fallback | Used only when the local write fails, which wakes a TV in standby |

The TV acts on these only while it is on, so they are unavailable when it is
off. They need internet access and Sony's cloud to be up. The TV pushes no
changes for them, so the cloud state is also polled every five minutes; a
sleep timer set with the remote shows up within that time.

Verified on a BRAVIA 8 II: the sleep timer, Picture Off, playback, seek, deep
links, search, speaker type and standby power-on all execute and report back
within a second or two. Channel tuning reaches the tuner but was not tested
with channels installed. On other models,
`tools/cloud_command_probe.py caps` shows what the TV advertises.

## HA TV Agent

With the [HA TV Agent](https://github.com/braviafanboy/bravia-tv-agent)
installed on the TV, the device gains:

| Entity | Source on the TV | Notes |
|---|---|---|
| `sensor` Illuminance (lx) | Android light sensor | The reading the TV's own auto luminance level uses |
| `sensor` Ambient temperature (°C) | Android ambient temperature sensor | |
| `sensor` Remote battery (%) | The remote's Bluetooth battery service | Diagnostic; needs the Bluetooth permission |
| `binary_sensor` Remote connected | Bluetooth connection state | Diagnostic |
| `binary_sensor` Panel | Display state | Off during *Picture Off* while the TV stays on |
| `media_player` now playing | The foreground app's media session | Title, artist, album, position, duration, artwork and play state; needs Notification access |
| `sensor` Wi-Fi signal and link speed | WifiManager | Diagnostic, disabled by default |
| `button` Agent setup | | Walks through any missing permission prompts on the TV |
| `update` HA TV Agent | The agent's release manifest | Installs a new agent version, with one confirmation on the TV |
| Picture settings and power saving | The TV's REST IP control, called by the agent on the TV | See below |

Agent entities are unavailable while the TV is off, since the TV leaves the
network in standby.

What now-playing shows depends on what each app publishes. Measured on a
BRAVIA 8 II:

| App | Title and artwork |
|---|---|
| YouTube, YouTube Music | Title, channel and thumbnail |
| Spotify | Track, artist, album and cover |
| Disney+ | Title, and season and episode for series; no artwork |
| Apple TV | Duration only |
| Prime Video | Nothing readable beyond a catalogue id |
| Netflix | Nothing beyond play state |

## Picture settings

With the agent installed, every picture setting the TV's menu offers appears
in Home Assistant: contrast, colour, colour temperature, gamma, the full white
balance and colour management sets, local dimming, peak luminance, HDR mode
and tone mapping, Motionflow, Reality Creation, noise reduction, the HDMI
signal settings, and the Ambient Optimization switches. Power saving is
included. On a BRAVIA 8 II that is 66 settings. Brightness and picture mode
keep their gRPC entities.

- **Classification.** A setting with a range becomes a `number`, an on/off
  setting a `switch`, and a list of values a `select` with readable labels.
  Without the IP control key they are read-only `sensor`s instead.
- **Colour gamma adjustment.** The TV's 20-point adjustment (points 1 to 20,
  each with an R-, G- and B-Offset) appears as three read-only diagnostic
  sensors, one per colour. The state lists the 20 values in point order, and
  the `points`, `min` and `max` attributes give them as numbers with the
  allowed range.
- **Everything is enabled.** Disable the entities you do not use; fewer
  enabled settings means less work for the TV at short poll intervals.
- **Refresh.** The TV announces no picture-setting change, so the agent
  re-reads them while the TV is on: every **Picture settings poll interval**
  (default 60 s), and immediately when the picture mode, input, content mode
  or HDR state changes if **Refresh picture settings on TV events** is on
  (the default). The TV keeps separate values for each of those.
- **Availability follows the TV.** A setting is unavailable while the TV
  offers no control for it: the HDMI signal settings and wide mode on an app,
  the Ambient Optimization sub-switches while the master switch is off, HDR
  tone mapping on SDR content.
- **Repairs** appear when the TV rejects the key, or when IP control stays
  unreachable for 3 minutes (Control remotely switched off on the TV).
