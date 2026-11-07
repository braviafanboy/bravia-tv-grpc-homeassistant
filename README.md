# Bravia TV (gRPC) for Home Assistant

*Community project; not affiliated with Sony or Home Assistant. BRAVIA is a trademark of Sony.*

A Home Assistant integration for recent Sony BRAVIA TVs. It controls the TV
over the same gRPC protocol as Sony's BRAVIA Connect app, which pushes every
change to Home Assistant as it happens.

## Features

- **Instant, two-way control.** Power, volume, inputs, apps, picture and
  content modes, audio outputs and the remote's keys. Changes made with the
  TV's own remote appear in Home Assistant within a second, with no polling.
- **Set up in a few clicks.** The TV is discovered on the network, and its
  entities are generated from what the specific model advertises.
- **Your apps as sources.** Installed apps appear as sources with their
  icons, in a browsable grid that launches them.
- **What is playing, in detail.** Sensors report the video signal (for
  example "4K 24fps Dolby Vision") and the audio format, ready for
  automations such as switching a soundbar's sound mode.
- **Controls only Sony's cloud offers.** The sleep timer, Picture Off,
  playback and seek, deep links into apps, on-screen search, and waking the
  TV from standby. These go through Sony's cloud, as
  they do in BRAVIA Connect, and need internet access.
- **More with the optional HA TV Agent.** A small companion app on the TV adds
  the ambient light and temperature sensors, now-playing titles and artwork,
  the remote's battery, a Find remote button that makes the remote beep,
  on-screen messages, Home Assistant dashboards on the TV screen, every
  picture setting from the TV's menu, and an ambient brightness mode that sets
  the picture Brightness from the room's light.
- **Resilient.** It recovers from TV reboots, network drops and expired
  sessions on its own. Controls that cannot be used in the current context
  show as unavailable, and a diagnostic sensor says why.

The [entity reference](docs/entities.md) lists everything the integration
creates.

## Compatible TVs

It works with the TVs the BRAVIA Connect app supports, Sony's lineup from
roughly 2023 onward: BRAVIA 9, 8, 7, 5, 3 and 2 II, and the A95L. Sony's
[compatible-models list](https://www.sony-asia.com/electronics/support/articles/00305900)
is authoritative. Older TVs that use *Video & TV SideView* are not compatible.

> **Only the BRAVIA 8 II (K-65XR8M2) has been tested.** Other models should
> work because entities follow what the TV advertises, but that is
> unverified. Please [open an issue](https://github.com/braviafanboy/bravia-tv-grpc-homeassistant/issues)
> to report how another model behaves.

## Installation

You need:

- **Home Assistant 2025.11 or newer**, on the same network as the TV.
  Reserve a fixed IP address for the TV on your router.
- **The TV signed in to a Sony account in the BRAVIA Connect app.** The
  integration gets its keys through that account, so pair the TV in BRAVIA
  Connect first and note which account it uses.

### 1. Install with HACS

[![Open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=braviafanboy&repository=bravia-tv-grpc-homeassistant&category=integration)

Or search HACS for **Bravia TV (gRPC)**, download it, and restart Home
Assistant.

### 2. Add the TV

[![Start setting up the integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=bravia_tv_grpc)

A discovered TV appears under **Settings → Devices & Services**; click
**Configure**. Otherwise add **Bravia TV (gRPC)** and enter the TV's IP
address.

### 3. Sign in to Sony

The setup asks you to sign in to Sony once and paste back a redirect address.

1. Open the sign-in link **in a computer's browser**, not on the phone with
   BRAVIA Connect: the phone would open the app instead.
2. Sign in with the account the TV uses in BRAVIA Connect.
3. The browser ends on an address starting with `ssh-app://signin?code=…`.
   Only the phone app can open it, so the page looks like it failed. That is
   expected. Copy the address and paste it into **Redirect URL**. The bare
   `code=…` value works too.

If the browser does not leave the address in the address bar:

- **Chrome or Edge:** before signing in, open DevTools (F12), go to
  **Network** and tick **Preserve log**. After signing in, select the last
  request with status `302` and copy its `Location` response header.
- **Firefox:** cancel the *Launch Application* pop-up, then do the same in
  DevTools (F12) → **Network**.

The sign-in creates keys for local control, stored in the config entry. After
that, everything except the cloud-only controls runs directly between Home
Assistant and the TV.

## HA TV Agent (optional)

The [HA TV Agent](https://github.com/braviafanboy/bravia-tv-agent) is a small
app for the TV, part of this project. Install it once with adb, following its
README. The integration then finds it, pairs with it and adds its entities
automatically.

Agent updates come through Home Assistant but are never installed on their
own. Each integration release is paired with one agent version, and an update
entity offers that version; installing it needs the Install button and a
confirmation on the TV. Agent updates therefore arrive with integration
updates, never ahead of them. If the agent is too old for the integration, a
repair says which version is needed. Checking for agent updates can be turned
off in the options.

The **Agent setup** button on the TV's device page brings up the permission
prompts for now-playing and the remote's battery.

### Picture settings

With the agent installed, the TV's picture settings appear as read-only
sensors. To change them from Home Assistant:

1. On the TV, make sure **Remote device settings → Control remotely** is on.
2. In the TV's network settings under **IP control**, set *Authentication* to
   *Normal and Pre-Shared Key* and enter a *Pre-Shared Key*.
3. Enter the same key in the integration's options under **IP control
   pre-shared key**.

The settings then become numbers, switches and selects. Every setting is
enabled, so disable the ones you don't use. The options also set how often
they are re-read.

### Picture profiles

Calibrating a TV by hand means nudging dozens of settings, and there is no way
to A/B two calibrations or get back to a known-good one. Picture profiles let
you **save the TV's current picture settings** (basic, 2-point and 20-point
white balance — everything writable) under a name, then **restore** or
**compare** them in a couple of clicks. Everything lives on the TV's device
page, no automations required:

- **New profile name** (text) — type a name here.
- **Save picture profile** (button) — snapshots the current settings under that
  name; the box then clears.
- **Picture profile** (dropdown) — your saved profiles; picking one only
  *selects* it (it does not touch the TV).
- **Restore picture profile** (button) — writes the selected profile back to
  the TV. It sets the picture mode first, then only the settings that differ, so
  a restore is quick.
- **Delete picture profile** (button) — removes the selected profile.

A typical calibration session: save a *before* profile, make changes, save an
*after*, then flip between them with the dropdown + Restore to compare them on
screen.

> Profiles are stored per picture mode and written while the TV is on. Turn the
> TV's auto-luminance (ambient-light) adjustment off while calibrating and
> restoring — a restore reports `auto_luminance_on` to remind you.
>
> A profile is also tied to the **dynamic range** it was captured in, because
> contrast, gamma and brightness are separate for SDR and HDR. A profile only
> restores while matching content is playing (an SDR profile won't apply during
> HDR, and vice-versa) unless you pass `force`. SDR counts as the default: with
> no video signal (a menu or the home screen) the TV uses its SDR picture, so
> **SDR profiles can be saved and restored from a menu** with nothing playing.
> HDR and Dolby Vision profiles need that content on screen.

#### Where the controls are

Everything is on the TV's device page (Settings → Devices & services → your
TV), grouped under **Configuration**: the **New profile name** box, **Save**,
the **Picture profile** dropdown, **Restore** and **Delete**. That's the whole
save → pick → restore/delete loop — no dashboard needed. Open the dropdown to
see the selected profile's details (picture mode, dynamic range, model, when
saved), which it also exposes as attributes.

They're `config`-category entities, so Home Assistant doesn't add them to
dashboards automatically. If you'd rather keep them on a dashboard — or show a
profile's Mode / Range / Model inline — add an `entities` card for those
entities, with `type: attribute` rows on the select for the inline details.

For automations and scripts the same operations are actions that take the
TV as their `device_id`: `bravia_tv_grpc.save_picture_profile`, `restore_picture_profile`
(with `force` to skip the dynamic-range check), `delete_picture_profile`,
`list_picture_profiles` and `compare_picture_profiles` (which return data).

A hand-made file can hold just a subset of settings (e.g. only the 2-point
white balance): restoring it writes only those values. Omit `picture_mode` and
the restore won't change the mode; omit `hdr_class` and the dynamic-range check
is skipped.

#### Where profiles are stored (backup / share)

Each profile is a plain YAML file under
`config/bravia_picture_profiles/<your tv>/` (the folder is created when the TV
is added). The folder *is* the store, so there's no separate export/import:

- **Back up** or **version-control** a profile by copying its `.yaml` file.
- **Share** one by sending the file; the recipient drops it into their own
  TV's folder and it appears in the dropdown (within a minute, or after a
  reload).
- **Move to another TV** by copying the file into that TV's folder.

Use the **File editor**, **Samba share**, **Studio Code Server** or SSH add-on
to reach the folder. The file name is the profile name. Files are validated on
read, so a malformed or hand-mangled one is skipped rather than loaded.

### Ambient brightness

With the agent installed, the TV's picture **Brightness** can follow the room's
light level, with a separate curve per picture mode. The agent runs the loop on
the TV, so it keeps working even while Home Assistant is down; Home Assistant
edits and mirrors it. It is **off by default** and SDR only (the TV keeps
separate picture values for HDR and Dolby Vision).

- **Ambient brightness** (switch) — turns it on and off.
- **Ambient brightness learning** (switch) — chooses how a *manual* brightness
  change is treated. With learning **off**, changing Brightness by hand switches
  ambient brightness off until you turn it back on. With learning **on**, it
  takes the new value as this picture mode's brightness for the current light
  and carries on from there.
- **Ambient brightness at 0 / 10 / 25 / 60 / 150 / 500 lux** (numbers) — the
  current picture mode's curve; they show and edit whichever mode is active.
- **Reset ambient brightness curve** (button) — drops the current mode's own
  curve so it follows the shared default again.
- **Ambient brightness status** (sensor) — off, active, paused or unavailable,
  with the reason and the curves as attributes.

It pauses itself while the TV's own **Auto luminance level** is on (the two
would fight), without the IP control key, while the screen is off, and during
HDR or Dolby Vision. Restoring a [picture profile](#picture-profiles) resets the
learnt curve for that picture mode, since the restore sets a new brightness
baseline. Nothing about ambient brightness is stored in a profile.

### Dashboards on the TV

With the agent installed, the **Show dashboard on TV** action
(`bravia_tv_grpc.show_dashboard`, for a TV you pick) puts a Home
Assistant dashboard on the TV screen in one of two modes:

- **Full screen** — interactive. The remote's arrows move between controls and
  OK presses. Back steps back inside Home Assistant (closes a card's dialog,
  leaves a subview), then asks before closing the dashboard. It stays until
  closed unless you set a duration.
- **Overlay** — a view-only panel over whatever is playing, for example a
  doorbell camera in a corner or an energy view down the right side. The remote
  keeps controlling the app behind it, so it closes after a duration (30 s by
  default) or with **Close dashboard on TV**.

Both modes need the agent's "Display over other apps" permission (Android only
lets a background app bring a screen forward with it); the agent's setup asks
for it. Home Assistant's sidebar and editing controls are always hidden on the
TV. **Hide title bar** (`kiosk`) also hides the dashboard's title bar for a
cleaner look: on by default for overlays, off for full screen, where its tabs
and back arrow are how you move between views.

```yaml
action: bravia_tv_grpc.show_dashboard
data:
  device_id: 0123456789abcdef0123456789abcdef   # the TV: pick it in the action editor
  dashboard: dashboard-tv/doorbell   # the path after the host in its address
  mode: overlay
  position: top_right                # right, left, full or a corner
  size: 30                           # percent of the screen
  duration: 20
```

How it signs in: on first use the integration creates a limited user,
**HA TV Agent display** (not an administrator, local network only), and hands
the TV 30-minute access tokens for it over the agent's local, encrypted link.
The TV never holds a lasting credential, and nothing about your Home Assistant
goes through Sony. Home Assistant can't limit a sign-in to one dashboard, so
the TV can open whatever that user can see; admin-only dashboards won't open.
The user is removed when the last TV is removed from the integration.

The TV loads the dashboard from Home Assistant's local address (Settings →
System → Network), so it must be able to reach Home Assistant. If the TV is on
a separate IoT network, allow it to connect to Home Assistant's port. Home
Assistant over HTTPS needs a certificate the TV trusts.

## Options

- **IP control pre-shared key:** enables changing picture settings (see
  above).
- **Picture settings poll interval** and **Refresh picture settings on TV
  events:** how the agent keeps picture settings current.
- **gRPC port:** set only if discovery fails on your network.
- **Check for HA TV Agent updates:** on by default; off removes the agent's
  update entity and stops the checks.
- **Agent release manifest URL:** leave empty for the agent release paired
  with this integration; set it only to serve the agent from elsewhere.

## Troubleshooting

- **"Could not find the gRPC control service":** check that the TV is on and
  on the same network. If your network blocks discovery, set the gRPC port in
  the options (`0` restores automatic discovery).
- **Pairing fails or finds no devices:** sign in with the exact Sony account
  the TV uses in BRAVIA Connect.
- **"The Sony login redirect could not be validated":** the pasted value was
  incomplete or from another attempt. Restart the step and paste the whole
  address.
- **Entities unavailable after a TV reboot:** wait a minute or two. The
  integration finds the TV again and refreshes its keys on its own.
- **A cloud control fails to send:** the TV needs internet access and Sony's
  cloud must be reachable. "The TV rejected the cloud command" means the TV
  could not act in its current state, such as seeking with nothing playing.
- **Picture settings unavailable, or a repair about IP control:** turn on
  **Control remotely** on the TV, and check that the key matches.

## Blueprints

[![Import the blueprint.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fbraviafanboy%2Fbravia-tv-grpc-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbraviafanboy%2Fbravia_sound_by_audio_format.yaml)

**Sound Settings by Audio Format** runs actions, such as a soundbar's sound
field or night mode, when the Audio Signal sensor matches formats you list,
like `Dolby Atmos` or `PCM`.

## Soundbars

Soundbar settings such as its volume and sound fields are left to the
[bravia-quad-homeassistant](https://github.com/steamEngineer/bravia-quad-homeassistant)
integration, which talks to the soundbar directly. This integration selects
the soundbar as the TV's audio output and controls the TV's own Direct
Connect rear speakers and subwoofer.

## Development

See [docs/development.md](docs/development.md) for the project layout, tools
and tests, and [docs/entities.md](docs/entities.md) for the full entity
reference.

## Thanks

This integration started from
[bravia-quad-homeassistant](https://github.com/steamEngineer/bravia-quad-homeassistant)
by steamEngineer, whose gRPC and Sony sign-in code it reuses.

## License

[MIT](LICENSE), matching bravia-quad-homeassistant.
