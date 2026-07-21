# BRAVIA TV gRPC / Sony IoT schema

Device: **BRAVIA 8 II** (BRAVIA 8 II), firmware 114.602.080.1EUA, software 4.1.4, destination EU-T2S2.

Enumerated from the Sony Seeds IoT cloud API (`GET /devices` capabilities + `GET /devices/{id}/states`). These are the same control/report fields the ControlDeviceService gRPC plane exposes; the cloud API gives them typed with command verbs and value enums.

Total capabilities: **23**. Fields marked reportable appear in the live state snapshot.

## Capabilities

| Field | Type | Commands | Reportable | Values / range | Current |
|-------|------|----------|:----------:|----------------|---------|
| `d_pad` | enum | set |  | up, down, left, right, back, center |  |
| `sleep_timer` | enum | set | yes | 7 values (see raw) | off |
| `application` | enum | set |  | 34 values (see raw) |  |
| `application.deep_link` | any | post |  |  |  |
| `brightness` | int | set, plus | yes | 0..50 | 15 |
| `power` | bool | set | yes |  | True |
| `skip_channels` | int | plus |  | -1..1 |  |
| `channel` | any | post |  |  |  |
| `mute` | bool | set |  |  |  |
| `volume` | int | set, plus | yes | 0..100 | 16 |
| `focused_ui_element` | any | — | yes |  | null |
| `select_media` | any | post |  |  |  |
| `seek` | int | set, plus |  | -2147483648..2147483647 |  |
| `ui_elements` | any | — | yes |  | null |
| `playback` | enum | set | yes | 8 values (see raw) | none |
| `picture_mode` | enum | set | yes | 20 values (see raw) | professional |
| `access_shortcut` | bool | set |  |  |  |
| `speaker_type` | enum | set | yes | tv, audio_system | audio_system |
| `gui_shortcut` | enum | set |  | home, guide |  |
| `picture` | bool | set | yes |  | True |
| `search` | any | post |  |  |  |
| `input` | any | post | yes |  | HDMI 3 (eARC/ARC) |
| `ui_action` | any | post |  |  |  |

## Enum value lists (full)

- **d_pad**: `up`, `down`, `left`, `right`, `back`, `center`
- **sleep_timer**: `off`, `15min`, `30min`, `45min`, `60min`, `90min`, `120min`
- **application**: `com.android.tv.settings`, `com.android.vending`, `com.google.android.youtube.tv`, `com.google.android.youtube.tvmusic`, `com.sony.dtv.osat.music.toast`, `com.sony.dtv.smartmediaapp`, `com.sony.dtv.timers`, `com.sony.dtv.tvlin`, `com.spocky.projengmenu`, `tv.twitch.android.app`, `com.amazon.amazonvideo.livingroom`, `com.google.android.play.games`, `com.netflix.ninja`, `com.sony.dtv.all4launcher`, `com.sony.dtv.calibrationmonitor`, `com.sony.dtv.ecodashboard`, `com.sony.dtv.iplayerlauncher`, `com.sony.dtv.itvlauncher`, `com.sony.dtv.livingfit`, `com.sony.dtv.my5launcher`, `com.sony.dtv.notificationcenter`, `com.sony.dtv.promos`, `com.sony.dtv.recapp`, `com.sony.dtv.seeds.iot`, `com.sony.dtv.smarthelp`, `com.sony.dtv.sonyselect`, `com.sony.dtv.soundslauncher`, `com.sonypicturescore`, `com.vewd.core.browser`, `com.youview.poa`, `com.apple.atve.sony.appletv`, `com.disney.disneyplus`, `com.kick.mobile`, `org.localsend.localsend_app`
- **playback**: `playing`, `paused`, `stopped`, `fast_forwarding`, `rewinding`, `previous`, `next`, `none`
- **picture_mode**: `calibrated`, `calm`, `cinema`, `custom_for_pro1`, `custom_for_pro2`, `dolby_netflix_calibrated`, `dolby_vision_bright`, `dolby_vision_dark`, `dolby_vision_game`, `dolby_vision_vivid`, `fps_game`, `imax`, `photo`, `photo_app`, `professional`, `rts_game`, `standard`, `vivid`, `netflix_calibrated`, `standard_game`
- **speaker_type**: `tv`, `audio_system`
- **gui_shortcut**: `home`, `guide`

## Live state snapshot fields

`GET /devices/{id}/states` returns these (plus `timestamp`, `connectivity`) — the push/report surface:

- `speaker_type` (enum) = `audio_system`
- `ui_elements` (any) = `null`
- `focused_ui_element` (any) = `null`
- `playback` (enum) = `none`
- `sleep_timer` (enum) = `off`
- `volume` (int) = `16`
- `power` (bool) = `True`
- `input` (any) = `HDMI 3 (eARC/ARC)`
- `picture` (bool) = `True`
- `brightness` (int) = `15`
- `picture_mode` (enum) = `professional`

## Local gRPC control plane — status (verified on BRAVIA 8 II / K-65XR8M2)

Auth handshake with Sony Seeds cloud credentials works:

- `ConfirmSignin` → **success** (`auth_data = SHA256(device_id)`)
- `ConfirmKeys` → accepted (`key_data = HMAC-SHA256(hmac_key, session_id)`)
- `GetSessionRandom` → returns a **64-byte `session_random` on the TV**; builders
  were made length-agnostic to handle it.

### GetCapabilities — WORKS (no auth, empty request)

Returns the TV's **full internal field-path namespace** as JSON: **118 paths**,
typed, with min/max, enum value lists, and `.availability` /
`.unavailable_reason` / `.prerequisites` sub-fields. Saved to
`grpc_capabilities.json` (raw) and `grpc_field_paths.json` (typed index). This
is the authoritative local schema — richer than the 23-field cloud view — and
is what a BRAVIA Connect packet capture would have recovered, obtained directly
and read-only. **No packet capture was needed.**

Key control paths include `display_setting.picture_mode` (enum incl.
`professional`, `calibrated`, custom-for-pro variants), `display_setting.brightness`
(int 0–50), `sound_setting.volume.tv_speaker`, `system_setting.input`, `power`,
`mute`, `remote_key.*`, `display_and_sound_setting.content_mode`, etc.

### StartNotifyStates (push) — WORKS

Server-streaming RPC, request is just `session_id` (no auth token / session
block). **Verified live**: nudging `display_setting.brightness` 15→16→15 via the
REST API produced two notify frames on the gRPC stream, decoded correctly by the
ported `notify_decode` to `display_setting.brightness`. This is the local push
channel the whole exercise was after — instant, no polling.

### GetStatesWithAuth (unary snapshot) — not yet working

Returns `INVALID_ARGUMENT` (improved from `UNKNOWN` once real field paths were
used, so paths are recognized). The remaining issue is the TV's exact
authenticated-request wire format — most likely the 64-byte `session_random`
session-block encoding, which is ambiguous without a real app capture. **Not blocking:** initial state comes from the
cloud snapshot (`GET /devices/{id}/states`), and live updates come from
StartNotifyStates. Nailing the unary snapshot is a follow-up if a
capture-free derivation isn't found.
