"""``tools/ha_stub_harness.py`` against a fake TV.

Nothing else runs the harness, and it once stopped starting without anyone
noticing: its stand-ins for Home Assistant had fallen behind what the
integration imports. These tests run it the way a developer does, as its own
process, so that cannot happen quietly again.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading

import pytest

pytest.importorskip("grpc")
# What the integration's modules import besides Home Assistant.
for _module in ("aiohttp", "awesomeversion", "cryptography", "voluptuous", "yaml"):
    pytest.importorskip(_module)

from fake_tv_server import KEY, FakeTvServer  # noqa: E402
from pb_helpers import ld, vint  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_HARNESS = _REPO / "tools" / "ha_stub_harness.py"
# A line of the entity listing: platform, key, value.
_ENTITY_LINE = re.compile(
    r"^  (binary_sensor|button|media_player|notify|number|remote|select|sensor"
    r"|switch|text|update) +\S+ +\S"
)


@pytest.fixture
def tv():
    server = FakeTvServer()
    server.states = {
        "power": True,
        "volume": 18,
        "mute": False,
        "display_setting.brightness": 30,
        "display_setting.picture_mode": "standard",
        "system_setting.friendly_name": "Fake BRAVIA",
    }
    yield server
    server.stop()


def _credentials(tmp_path: Path, key: str = KEY) -> Path:
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "device_id": "device-1",
                "hmac_key": key,
                "key_id": "key-1",
                "session_key": "cd" * 32,
                "refresh_token": "refresh",
            }
        )
    )
    return path


def _run(port: int, credentials: Path, *args: str, on_line=None) -> tuple[int, str]:
    """Run the harness as its own process; (exit code, everything it printed)."""
    process = subprocess.Popen(
        [
            sys.executable,
            str(_HARNESS),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--credentials",
            str(credentials),
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=_REPO,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    lines: list[str] = []

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line.rstrip("\n"))
            if on_line is not None:
                on_line(lines[-1])

    reader = threading.Thread(target=read)
    reader.start()
    try:
        code = process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        code = -1
    reader.join(10)
    return code, "\n".join(lines)


def test_lists_entities_writes_and_sees_a_push(tv, tmp_path):
    def push_when_watching(line: str) -> None:
        if line.startswith("watching"):
            tv.pushes.put(ld(2, ld(1, ld(1, ld(1, b"volume") + ld(2, vint(1, 30))))))

    code, out = _run(
        tv.port, _credentials(tmp_path), "--watch", "3", on_line=push_when_watching
    )
    assert code == 0, out
    assert "Traceback" not in out, out
    assert "started: 118 capability paths, 6 state values" in out
    # Entities are built from the schema by the integration's own platforms.
    count = int(re.search(r"^entities: (\d+)$", out, re.M).group(1))
    listed = [line for line in out.splitlines() if _ENTITY_LINE.match(line)]
    assert count == len(listed) > 20, out
    assert "!!" not in out  # no entity failed to give its value
    platforms = {line.split()[0] for line in listed}
    assert {"media_player", "number", "select", "sensor", "switch"} <= platforms
    assert any(
        line.split()[:2] == ["media_player", "power"] and "on, volume 18%" in line
        for line in listed
    )
    assert any(
        line.split() == ["number", "display_setting.brightness", "30.0"]
        for line in listed
    )
    # Command dispatch: each write reached the TV, signed.
    assert "volume set to its current level (18%): accepted" in out
    assert "current picture mode 'Standard' (of 63) selected again: accepted" in out
    # (The fake aborts a write whose signature is wrong. The picture mode
    # goes out as two: the client confirms the change first.)
    assert b"\x06volume" in tv.writes[0]
    assert b"display_setting.picture_mode\x10" in tv.writes[-1]
    assert all(
        b"volume" in write or b"display_setting.picture_mode" in write
        for write in tv.writes
    )
    # Push to state.
    assert re.search(r"^  \d\d:\d\d:\d\d volume: 18 -> 30$", out, re.M), out
    assert out.rstrip().endswith("shut down cleanly")
    assert tv.handshakes == 1 and tv.subscriptions == 1


def test_read_only_writes_nothing_and_leaves_the_agent_alone(tv, tmp_path):
    code, out = _run(tv.port, _credentials(tmp_path), "--read-only", "--verbose")
    assert code == 0, out
    assert tv.writes == []
    assert "read-only: nothing written to the TV" in out
    # The integration looked for the HA TV Agent on the TV and for Sony's
    # cloud, and got neither.
    assert "web requests refused: 127.0.0.1" in out
    assert "web requests refused: v1.api." in out
    # --verbose says which Home Assistant names were placeholders.
    assert "homeassistant.components.select.SelectEntity" in out


def test_rejected_keys_end_in_advice(tv, tmp_path):
    code, out = _run(tv.port, _credentials(tmp_path, key="ee" * 32), "--read-only")
    assert code == 1, out
    assert "Traceback" not in out, out
    assert "could not start" in out
    assert "run again with --cloud" in out
    assert tv.handshakes == 0 and tv.writes == []


def test_unreachable_tv_is_reported_without_the_keys_advice(tmp_path):
    server = FakeTvServer()
    port = server.port
    server.stop()
    code, out = _run(port, _credentials(tmp_path), "--read-only")
    assert code == 1, out
    assert "Traceback" not in out, out
    assert "could not start" in out
    assert "--cloud" not in out


def test_an_import_without_a_stand_in_does_not_stop_it():
    """What broke the harness before: the integration came to import things
    of Home Assistant's that the harness had no stand-in for."""
    script = f"""
import importlib.util
spec = importlib.util.spec_from_file_location("stub_harness", {str(_HARNESS)!r})
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
harness.install_stand_ins()

from homeassistant.components.novel import NovelEntity, NovelEntityFeature
import homeassistant.helpers.another_one as another
from homeassistant.helpers.not_written_yet import NEW_CONSTANT, NewThing, new_helper


class Mine(NovelEntity):
    _attr_supported_features = NovelEntityFeature.ONE | NovelEntityFeature.TWO


assert Mine().supported_features == NovelEntityFeature.TWO | NovelEntityFeature.ONE
assert NewThing(a=1).a == 1 and NEW_CONSTANT == "new_constant"
assert not new_helper(1, 2) and not another.registry.async_get(None)
assert "homeassistant.helpers.not_written_yet.new_helper" in harness.MADE_UP
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr


# -- the parts that only matter with --cloud, which no test can run for real --
@pytest.fixture(scope="module")
def harness():
    """The harness as a module. Importing it installs nothing: the stand-ins
    go in only when it runs."""
    spec = importlib.util.spec_from_file_location("stub_harness_under_test", _HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "bravia_tv" not in sys.modules
    return module


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        return "reply"

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        return "reply"

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return "reply"


def test_web_session_reaches_only_sonys_cloud_and_only_when_asked(harness):
    import aiohttp

    sony = {"auth.sony.example", "iot.sony.example"}
    offline = harness.GuardedSession(sony)
    with pytest.raises(aiohttp.ClientConnectionError, match="--cloud"):
        offline.post("https://auth.sony.example/token")
    assert offline.cloud_refused
    # The agent on the TV, and anything else, is refused whatever the mode.
    session = _Session()
    online = harness.GuardedSession(sony, session)
    for call in (
        lambda: online.get("https://192.0.2.10:8585/v1/info"),
        lambda: online.post("https://192.0.2.10:8585/v1/pair"),
        lambda: online.request("GET", "https://github.com/x/releases/agent.json"),
        lambda: offline.get("https://192.0.2.10:8585/v1/info"),
    ):
        with pytest.raises(aiohttp.ClientConnectionError, match="does not connect"):
            call()
    assert session.calls == [] and not online.cloud_refused
    assert online.get("https://iot.sony.example/devices") == "reply"
    assert online.post("https://auth.sony.example/token") == "reply"
    assert session.calls == [
        ("GET", "https://iot.sony.example/devices"),
        ("POST", "https://auth.sony.example/token"),
    ]
    # No other way through to the session underneath.
    assert not hasattr(online, "ws_connect")


def test_refreshed_credentials_are_written_back_whole(harness, tmp_path, capsys):
    path = _credentials(tmp_path)
    before = json.loads(path.read_text())
    entry = harness.ConfigEntry(
        data={"keys": json.dumps(before)}, title="TV", unique_id="d"
    )
    updated = harness.credentials_saver(path, before, "keys")

    # The same credentials in another spelling are not a change.
    entry.data["keys"] = json.dumps(before, separators=(",", ":"))
    updated(entry)
    assert capsys.readouterr().out == ""

    after = {**before, "hmac_key": "12" * 32, "session_key": "34" * 32, "key_id": "k2"}
    entry.data["keys"] = json.dumps(after)
    updated(entry)
    assert json.loads(path.read_text()) == after
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    assert "saved" in capsys.readouterr().out

    # Something that is not a set of keys never replaces the file.
    entry.data["keys"] = json.dumps({"device_id": "device-1"})
    with pytest.raises(ValueError):
        updated(entry)
    assert json.loads(path.read_text()) == after


def test_placeholders_behave_like_what_they_stand_for(harness):
    flag = harness._make_up("homeassistant.components.x", "ThingEntityFeature")
    features = flag(0)
    assert not features
    features |= flag.PLAY | flag.PAUSE
    assert features & flag.PLAY and not features & flag.STOP
    assert repr(features) == "pause | play"
    state = harness._make_up("homeassistant.components.x", "ThingState")
    assert state.ON == "on" and state.ON.value == "on"
    assert harness._make_up("homeassistant.const", "CONF_THING") == "conf_thing"
    error = harness._make_up("homeassistant.exceptions", "ThingNotReady")
    assert issubclass(error, harness.HomeAssistantError)
    entity = harness._make_up("homeassistant.components.x", "ThingEntity")
    assert issubclass(entity, harness.Entity)
    thing = entity()
    thing._attr_native_value = 4
    assert thing.native_value == 4 and thing.available
    with pytest.raises(AttributeError):
        thing.current_option  # noqa: B018
    registry = harness._make_up("homeassistant.helpers", "thing_registry")
    assert not registry and list(registry.async_get(None).entities) == []

    @registry.decorator
    def kept() -> int:
        return 1

    assert kept() == 1
