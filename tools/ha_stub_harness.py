"""Drive the REAL Bravia TV coordinator + entities against the live TV using
minimal stand-ins for the Home Assistant base classes the integration imports.

The stubs are thin but faithful: DataUpdateCoordinator stores listeners and
data; CoordinatorEntity subscribes; the executor runs jobs on threads; the loop
marshals call_soon_threadsafe. This exercises the integration's own logic
(seeding, push->state, entity mapping, command dispatch) — not HA internals.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import types

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "custom_components" / "bravia_tv_grpc"


# ---- build stub `homeassistant` package tree --------------------------------
def _mod(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


ha = _mod("homeassistant")
core = _mod("homeassistant.core")
config_entries = _mod("homeassistant.config_entries")
const = _mod("homeassistant.const")
exceptions = _mod("homeassistant.exceptions")
helpers = _mod("homeassistant.helpers")
h_uc = _mod("homeassistant.helpers.update_coordinator")
h_dr = _mod("homeassistant.helpers.device_registry")
h_ep = _mod("homeassistant.helpers.entity_platform")
h_ac = _mod("homeassistant.helpers.aiohttp_client")
h_ev = _mod("homeassistant.helpers.event")
comp = _mod("homeassistant.components")
c_switch = _mod("homeassistant.components.switch")
c_number = _mod("homeassistant.components.number")
c_select = _mod("homeassistant.components.select")
c_mp = _mod("homeassistant.components.media_player")
c_sensor = _mod("homeassistant.components.sensor")
c_bsensor = _mod("homeassistant.components.binary_sensor")
c_text = _mod("homeassistant.components.text")


def async_track_time_interval(hass, action, interval, *a, **k):
    return lambda: None


h_ev.async_track_time_interval = async_track_time_interval


def callback(fn):
    return fn


core.callback = callback


class HomeAssistant:
    def __init__(self, loop):
        self.loop = loop
        self.data = {}

    async def async_add_executor_job(self, fn, *args):
        return await self.loop.run_in_executor(None, fn, *args)


core.HomeAssistant = HomeAssistant


class Platform:
    MEDIA_PLAYER = "media_player"
    NUMBER = "number"
    SELECT = "select"
    SENSOR = "sensor"
    TEXT = "text"
    SWITCH = "switch"


class EntityCategory:
    DIAGNOSTIC = "diagnostic"
    CONFIG = "config"


const.Platform = Platform
const.CONF_HOST = "host"
const.EntityCategory = EntityCategory


class ConfigEntry:
    def __init__(self, data, title="Bravia TV", unique_id="tvuid", entry_id="e1"):
        self.data = data
        self.title = title
        self.unique_id = unique_id
        self.entry_id = entry_id


config_entries.ConfigEntry = ConfigEntry


class ConfigEntryNotReady(Exception):
    pass


exceptions.ConfigEntryNotReady = ConfigEntryNotReady


class DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, name=None, config_entry=None, **kw):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.config_entry = config_entry
        self.data = None
        self._listeners = []

    def async_set_updated_data(self, data):
        self.data = data
        for cb in list(self._listeners):
            cb()

    def async_update_listeners(self):
        for cb in list(self._listeners):
            cb()

    def async_add_listener(self, cb, *a):
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    async def async_shutdown(self):
        pass


class CoordinatorEntity:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.hass = coordinator.hass

    def async_write_ha_state(self):
        pass

    def async_on_remove(self, fn):
        pass


h_uc.DataUpdateCoordinator = DataUpdateCoordinator
h_uc.CoordinatorEntity = CoordinatorEntity


class DeviceInfo(dict):
    def __init__(self, **kw):
        super().__init__(**kw)


h_dr.DeviceInfo = DeviceInfo
h_ep.AddEntitiesCallback = object


def async_get_clientsession(hass):
    raise RuntimeError("not used in this harness")


h_ac.async_get_clientsession = async_get_clientsession


class _Entity:
    _attr_has_entity_name = True

    @property
    def name(self):
        return getattr(self, "_attr_name", None)


class SwitchEntity(_Entity):
    pass


class NumberEntity(_Entity):
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0


class SelectEntity(_Entity):
    pass


class SensorEntity(_Entity):
    pass


class BinarySensorEntity(_Entity):
    pass


class TextEntity(_Entity):
    _attr_native_max = 100


class BinarySensorDeviceClass:
    UPDATE = "update"
    CONNECTIVITY = "connectivity"


c_switch.SwitchEntity = SwitchEntity
c_number.NumberEntity = NumberEntity
c_select.SelectEntity = SelectEntity
c_sensor.SensorEntity = SensorEntity
c_bsensor.BinarySensorEntity = BinarySensorEntity
c_bsensor.BinarySensorDeviceClass = BinarySensorDeviceClass
c_text.TextEntity = TextEntity


import enum as _enum


class MediaPlayerEntityFeature(_enum.IntFlag):
    TURN_ON = 128
    TURN_OFF = 256
    VOLUME_SET = 4
    VOLUME_MUTE = 8
    VOLUME_STEP = 1024
    SELECT_SOURCE = 2048


class MediaPlayerState(str, _enum.Enum):
    ON = "on"
    OFF = "off"


class MediaPlayerEntity(_Entity):
    pass


c_mp.MediaPlayerEntity = MediaPlayerEntity
c_mp.MediaPlayerEntityFeature = MediaPlayerEntityFeature
c_mp.MediaPlayerState = MediaPlayerState


# ---- load the integration as a proper package -------------------------------
def load_pkg():
    btv = types.ModuleType("bravia_tv")
    btv.__path__ = [str(PKG)]
    sys.modules["bravia_tv"] = btv
    grpc_pkg = types.ModuleType("bravia_tv.grpc")
    grpc_pkg.__path__ = [str(PKG / "grpc")]
    sys.modules["bravia_tv.grpc"] = grpc_pkg

    def load(qual, rel):
        spec = importlib.util.spec_from_file_location(qual, PKG / rel)
        m = importlib.util.module_from_spec(spec)
        sys.modules[qual] = m
        spec.loader.exec_module(m)
        return m

    for q, r in [
        ("bravia_tv.const", "const.py"),
        ("bravia_tv.grpc.credentials", "grpc/credentials.py"),
        ("bravia_tv.grpc.get_states_request", "grpc/get_states_request.py"),
        ("bravia_tv.grpc.get_states_auth", "grpc/get_states_auth.py"),
        ("bravia_tv.grpc.exec_command_request", "grpc/exec_command_request.py"),
        (
            "bravia_tv.grpc.get_capabilities_response",
            "grpc/get_capabilities_response.py",
        ),
        ("bravia_tv.grpc.notify_decode", "grpc/notify_decode.py"),
        ("bravia_tv.bravia_tv_client", "bravia_tv_client.py"),
        ("bravia_tv.coordinator", "coordinator.py"),
        ("bravia_tv.entity", "entity.py"),
        ("bravia_tv.switch", "switch.py"),
        ("bravia_tv.number", "number.py"),
        ("bravia_tv.select", "select.py"),
        ("bravia_tv.media_player", "media_player.py"),
        ("bravia_tv.sensor", "sensor.py"),
        ("bravia_tv.binary_sensor", "binary_sensor.py"),
        ("bravia_tv.text", "text.py"),
    ]:
        load(q, r)


async def main():
    load_pkg()
    from bravia_tv import (
        binary_sensor,
        media_player,
        number,
        select,
        sensor,
        switch,
        text,
    )
    from bravia_tv.bravia_tv_client import BraviaTvGrpcClient
    from bravia_tv.coordinator import BraviaTvCoordinator

    creds = json.load(open(REPO / "schema" / ".credentials.json"))
    loop = asyncio.get_running_loop()
    hass = HomeAssistant(loop)
    entry = ConfigEntry(
        data={
            "host": "192.0.2.10",
            "grpc_port": 36547,
            "grpc_device_id": creds["device_id"],
            "grpc_keys": json.dumps(creds),
        },
        unique_id=creds["device_id"],
    )
    client = BraviaTvGrpcClient(
        "192.0.2.10",
        36547,
        creds["device_id"],
        creds["hmac_key"],
        key_id=creds.get("key_id"),
        session_key=creds.get("session_key"),
    )
    coord = BraviaTvCoordinator(hass, entry, client)
    await coord.async_start()
    print("coordinator started; seeded state keys:", sorted(coord.data or {}))

    # Collect entities from each platform's setup
    collected = []

    def add(entities):
        collected.extend(entities)

    for plat in (switch, number, select, media_player, sensor, binary_sensor, text):
        await plat.async_setup_entry(hass, _EntryWrap(hass, entry, coord), add)
    print(f"entities created: {len(collected)}")
    for e in collected:
        val = None
        if hasattr(e, "native_value"):
            val = e.native_value
        elif hasattr(e, "current_option"):
            val = e.current_option
        elif hasattr(e, "state") and type(e).__name__.endswith("MediaPlayer"):
            val = f"state={e.state} vol={e.volume_level} muted={e.is_volume_muted} source={e.source}"
        elif hasattr(e, "is_on"):
            val = e.is_on
        print(f"  {type(e).__name__:18} {str(e.name):14} = {val}")

    # Drive the media_player: report features, then a benign volume round-trip
    # (set volume to its own current value -> no audible change).
    mp = next((e for e in collected if type(e).__name__.endswith("MediaPlayer")), None)
    if mp is not None:
        print(f"\nmedia_player features: {mp._attr_supported_features!r}")
        lvl = mp.volume_level
        print(f"volume_level={lvl}, muted={mp.is_volume_muted}, state={mp.state}")
        if lvl is not None:
            await mp.async_set_volume_level(lvl)  # set to current -> no change
            print(f"re-set volume to current ({round(lvl * mp._vol_max)}) -> accepted")
        # SELECT_SOURCE: source_list + re-select the CURRENT source (no-op app launch)
        print(
            f"source_list ({len(mp.source_list)}): {', '.join(mp.source_list[:8])} ..."
        )
        cur_src = mp.source
        print(f"current source: {cur_src}")
        if cur_src in (mp.source_list or []):
            await mp.async_select_source(cur_src)  # relaunch current app = no-op
            print(f"re-selected current source '{cur_src}' -> accepted (no-op)")

    # Drive a real command through a select entity: read picture mode, set it to
    # its own current value (no visible change), confirm command accepted.
    pm = next((e for e in collected if getattr(e, "name", "") == "Picture Mode"), None)
    if pm is not None:
        cur = pm.current_option
        print(f"\npicture mode currently: {cur} (options: {len(pm._attr_options)})")
        print(
            "custom_for_pro modes present:",
            [o for o in pm._attr_options if "ustomForPro" in o],
        )
        if cur:
            await pm.async_select_option(cur)
            print(f"re-selected {cur} via entity -> command accepted")

    # Prove PUSH end-to-end: change brightness via a DIFFERENT channel (REST),
    # then confirm the gRPC notify stream updated the Brightness entity through
    # the coordinator (not via our own optimistic exec).
    import urllib.request

    def rest(service, method, params):
        body = json.dumps(
            {"method": method, "params": params, "id": 1, "version": "1.0"}
        ).encode()
        req = urllib.request.Request(
            f"http://192.0.2.10/sony/{service}",
            body,
            {"Content-Type": "application/json", "X-Auth-PSK": "sony"},
        )
        return json.load(urllib.request.urlopen(req, timeout=8))

    bright = next(
        (e for e in collected if getattr(e, "name", "") == "Brightness"), None
    )
    if bright is not None:
        start = bright.native_value
        target = 16 if start != 16 else 17
        print(
            f"\nPUSH test: REST-set brightness {int(start)} -> {target} "
            "(external channel)"
        )
        rest(
            "video",
            "setPictureQualitySettings",
            [{"settings": [{"target": "brightness", "value": str(target)}]}],
        )
        for _ in range(20):
            await asyncio.sleep(0.25)
            if bright.native_value == float(target):
                break
        print(
            f"  entity via push = {bright.native_value} "
            f"({'UPDATED' if bright.native_value == float(target) else 'NOT updated'})"
        )
        rest(
            "video",
            "setPictureQualitySettings",
            [{"settings": [{"target": "brightness", "value": str(int(start))}]}],
        )
        await asyncio.sleep(1.0)
        print(f"  restored brightness to {bright.native_value}")

    await coord.async_shutdown()
    print("shutdown clean")


class _EntryWrap:
    """hass.data[DOMAIN][entry_id] = coordinator, as platforms expect."""

    def __init__(self, hass, entry, coord):
        from bravia_tv.const import DOMAIN

        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
        self.entry_id = entry.entry_id
        self.data = entry.data
        self.title = entry.title
        self.unique_id = entry.unique_id


if __name__ == "__main__":
    asyncio.run(main())
