"""Coordinator tests for the HA TV Agent link (require HA harness)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.bravia_tv_grpc.const import (  # noqa: E402
    CONF_AGENT_CERT,
    CONF_AGENT_FINGERPRINT,
    CONF_AGENT_TOKEN,
    CONF_GRPC_DEVICE_ID,
    CONF_GRPC_KEYS,
    DOMAIN,
)
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    AGENT_ONLINE,
    BraviaTvCoordinator,
    agent_path,
)
from custom_components.bravia_tv_grpc.grpc import agent as agentmod  # noqa: E402

COORD = "custom_components.bravia_tv_grpc.coordinator"
_KEYS = '{"device_id": "dev", "hmac_key": "ab"}'
_INFO = {"agent": {"version": "0.1.0", "contract": 1, "port": 8585}}
_DOC = {
    "environment": {"illuminance_lx": 12.0, "temperature_c": 22.5},
    "display": {"on": True},
    "media": {
        "active": True,
        "app": "a",
        "state": "playing",
        "title": "T",
        "position_s": 3.0,
    },
    "bluetooth": {
        "remote": {"name": "R", "connected": True, "battery_pct": 30},
        "devices": [],
    },
    "wifi": {"rssi_dbm": -50, "link_mbps": 400},
}

pytestmark = pytest.mark.agent_probe


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _no_manifest_fetch():
    """The release manifest lives on GitHub; tests never fetch it unless they
    patch the fetch themselves."""
    with patch(
        f"{COORD}.agentmod.async_fetch_manifest",
        new=AsyncMock(side_effect=agentmod.AgentError("offline")),
    ):
        yield


@pytest.fixture(autouse=True)
def _stub_ssl():
    """The fakes never open sockets; keep certificate handling symbolic for
    the whole test, including the background task that pins after pairing."""
    with patch(
        f"{COORD}.agentmod.make_ssl_context", side_effect=lambda cert: ("ctx", cert)
    ):
        yield


def _client() -> MagicMock:
    client = MagicMock()
    client.capabilities = {}
    client.get_states.return_value = {"power": True, "volume": 10}
    client.read_application_list.return_value = []
    client.exec_command.return_value = True
    return client


def _entry(hass: HomeAssistant, options=None, **extra):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev",
        options=options or {},
        data={
            "host": "tv.local",
            CONF_GRPC_KEYS: _KEYS,
            CONF_GRPC_DEVICE_ID: "dev",
            **extra,
        },
    )
    entry.add_to_hass(hass)
    return entry


class FakeAgent:
    """Stand-in for agentmod.AgentClient driven by the test."""

    PRESENTED = b"cert-A"  # the certificate the fake "presents"

    def __init__(
        self, session, host, port=8585, token=None, ssl_context=None, cert=None
    ):
        self.host = host
        self.port = port
        self.token = token
        self.ssl_context = ssl_context
        self.cert = cert  # the pinned certificate, if any
        self.seen_cert = None
        self.info = dict(_INFO)
        self.state_doc = dict(_DOC)
        self.events: asyncio.Queue = asyncio.Queue()
        self.pair_tokens: list[str] = []
        self.reject_token: str | None = None
        self.stream_started = asyncio.Event()
        self.picture_configs: list[tuple] = []
        self.psk_calls: list[str] = []
        self.psk_accept = True
        self.forget_calls = 0
        self.picture_writes: list[tuple[str, str]] = []
        self.power_writes: list[str] = []
        self.refreshes = 0
        self.write_error: Exception | None = None

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def url(self, path):
        return self.base_url + path

    @property
    def pinned(self):
        return self.cert is not None

    @property
    def fingerprint(self):
        return agentmod.fingerprint_of(self.cert) if self.cert else None

    def pin(self, cert, ssl_context):
        self.cert = cert
        self.ssl_context = ssl_context

    def _handshake(self):
        if self.cert is not None and self.cert != self.PRESENTED:
            raise agentmod.AgentFingerprintMismatch("pin")
        self.seen_cert = self.PRESENTED

    async def async_info(self):
        self._handshake()
        return self.info

    async def async_state(self):
        self._handshake()
        if not self.token or self.token == self.reject_token:
            raise agentmod.AgentUnauthorized("state")
        return self.state_doc

    async def async_themes(self):
        return [{"id": "theme://a", "title": "A"}]

    async def async_pair(self, name):
        self._handshake()
        if not self.pair_tokens:
            raise agentmod.AgentPairingClosed("closed")
        self.token = self.pair_tokens.pop(0)
        return self.token

    async def async_events(self):
        self._handshake()
        self.stream_started.set()
        while True:
            ev = await self.events.get()
            if ev is None:
                raise agentmod.AgentError("ended")
            yield ev

    async def async_artwork(self):
        return b"png"

    async def async_stage_update(self, url, sha256, version):
        self.staged = (url, sha256, version)

    async def async_picture_config(self, poll_s, event_refresh, watch):
        self.picture_configs.append((poll_s, event_refresh, list(watch)))
        return {}

    async def async_set_psk(self, psk):
        self.psk_calls.append(psk)
        return self.psk_accept

    async def async_forget_rest_auth(self):
        self.forget_calls += 1

    async def async_set_picture(self, target, value):
        if self.write_error:
            raise self.write_error
        self.picture_writes.append((target, value))

    async def async_set_power_saving(self, mode):
        self.power_writes.append(mode)

    async def async_picture_refresh(self):
        self.refreshes += 1


async def _until(predicate, timeout=5.0):
    """Yield to the loop until ``predicate()`` holds (or fail after timeout)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def _start(hass, entry, fake_holder, *, configure=None, cloud_caps=None):
    """Build and start a coordinator whose agent client is a FakeAgent.

    The agent task starts eagerly inside ``async_start``, so anything the
    fake must know (pair tokens, …) is applied by ``configure`` at creation.
    ``make_ssl_context`` is stubbed: the fakes never open sockets.
    """

    def factory(session, host, port=8585, token=None, ssl_context=None, cert=None):
        fake = FakeAgent(session, host, port, token, ssl_context, cert)
        if configure:
            configure(fake)
        fake_holder.append(fake)
        return fake

    with (
        patch(f"{COORD}.agentmod.AgentClient", side_effect=factory),
        patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()),
    ):
        coordinator = BraviaTvCoordinator(hass, entry, _client())
        coordinator._state["power"] = True
        if cloud_caps:
            coordinator.cloud_capabilities = cloud_caps
        await coordinator.async_start()
        return coordinator


async def test_stored_token_streams_state(hass: HomeAssistant):
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes)
    fake = fakes[0]
    assert coordinator.agent is fake
    await asyncio.wait_for(fake.stream_started.wait(), 5)
    assert coordinator.agent_value("environment", "illuminance_lx") == 12.0
    fake.events.put_nowait(("state", {**_DOC, "environment": {"illuminance_lx": 99.0}}))
    await _until(
        lambda: coordinator.agent_value("environment", "illuminance_lx") == 99.0
    )
    assert coordinator.data[AGENT_ONLINE] is True
    assert coordinator.agent_value("bluetooth", "remote", "battery_pct") == 30
    assert coordinator.agent_themes[0]["id"] == "theme://a"
    assert entry.data[CONF_AGENT_TOKEN] == "tok"
    # A pre-TLS token still works; the certificate gets pinned on first use.
    assert entry.data[CONF_AGENT_FINGERPRINT] == agentmod.fingerprint_of(
        FakeAgent.PRESENTED
    )
    await coordinator.async_shutdown()


async def test_pairs_via_local_launch_when_no_token(hass: HomeAssistant):
    entry = _entry(hass)
    fakes: list[FakeAgent] = []
    with patch(f"{COORD}.asyncio.sleep", new=AsyncMock()):
        coordinator = await _start(
            hass, entry, fakes, configure=lambda f: f.pair_tokens.append("fresh")
        )
        await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) == "fresh")
        await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    # The agent was launched through the local app-launch path for its window.
    coordinator.client.exec_command.assert_any_call(
        "system_setting.application", agentmod.AGENT_PACKAGE
    )
    assert entry.data[CONF_AGENT_TOKEN] == "fresh"
    assert entry.data[CONF_AGENT_FINGERPRINT] == agentmod.fingerprint_of(
        FakeAgent.PRESENTED
    )
    assert entry.data[CONF_AGENT_CERT] == agentmod.encode_cert(FakeAgent.PRESENTED)
    assert coordinator.data[AGENT_ONLINE] is True
    await coordinator.async_shutdown()


async def test_pairs_via_cloud_deep_link_first(hass: HomeAssistant):
    entry = _entry(hass)
    fakes: list[FakeAgent] = []
    sent: list[dict] = []

    async def fake_cloud(name, commands):
        sent.append(commands[0])
        fakes[0].reject_token = None  # the agent adopted whatever we delivered
        return {}

    with (
        patch(f"{COORD}.asyncio.sleep", new=AsyncMock()),
        patch(
            f"{COORD}.BraviaTvCoordinator.async_cloud_command", side_effect=fake_cloud
        ),
    ):
        coordinator = await _start(
            hass, entry, fakes, cloud_caps={"application.deep_link": ("post",)}
        )
        fake = fakes[0]
        await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) is not None)
    assert sent and sent[0]["name"] == "application.deep_link"
    uri = sent[0]["args"]["uri"] if "args" in sent[0] else sent[0]["uri"]
    assert uri.startswith("braviaagent://pair?token=")
    assert entry.data[CONF_AGENT_TOKEN] == fake.token
    coordinator.client.exec_command.assert_not_called()
    await coordinator.async_shutdown()


async def test_no_agent_is_harmless(hass: HomeAssistant):
    entry = _entry(hass)

    class Absent(FakeAgent):
        async def async_info(self):
            raise agentmod.AgentError("refused")

    with (
        patch(
            f"{COORD}.agentmod.AgentClient", side_effect=lambda *a, **k: Absent(*a, **k)
        ),
        patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()),
    ):
        coordinator = BraviaTvCoordinator(hass, entry, _client())
        await coordinator.async_start()
    assert coordinator.agent is None
    assert AGENT_ONLINE not in coordinator.data
    # It keeps looking: a later install is picked up without a reload.
    assert coordinator._agent_task is not None
    await coordinator.async_shutdown()


async def test_offline_tv_marks_agent_offline(hass: HomeAssistant):
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes)
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    coordinator._set_agent_online(False)
    assert coordinator.data[AGENT_ONLINE] is False
    assert coordinator.data[agent_path("media")] is None
    await coordinator.async_shutdown()


async def test_new_certificate_triggers_repair(hass: HomeAssistant):
    """A pinned fingerprint that no longer matches (reinstalled agent) clears
    the pin and the token and pairs again, learning the new certificate."""
    entry = _entry(
        hass,
        **{
            CONF_AGENT_TOKEN: "old",
            CONF_AGENT_CERT: agentmod.encode_cert(b"cert-OLD"),
            CONF_AGENT_FINGERPRINT: agentmod.fingerprint_of(b"cert-OLD"),
        },
    )
    fakes: list[FakeAgent] = []
    with patch(f"{COORD}.asyncio.sleep", new=AsyncMock()):
        coordinator = await _start(
            hass, entry, fakes, configure=lambda f: f.pair_tokens.append("renewed")
        )
        await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) == "renewed")
    assert entry.data[CONF_AGENT_FINGERPRINT] == agentmod.fingerprint_of(
        FakeAgent.PRESENTED
    )
    assert fakes[0].cert == FakeAgent.PRESENTED
    assert coordinator.data[AGENT_ONLINE] is True
    await coordinator.async_shutdown()


async def test_unpaired_agent_forces_pairing_despite_stored_token(hass: HomeAssistant):
    """An agent that reports itself unpaired accepts any token, so a stored
    token must not be trusted: pair again."""
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "stale"})
    fakes: list[FakeAgent] = []

    def configure(f):
        f.info = {**_INFO, "pairing": {"paired": False}}
        f.pair_tokens.append("fresh")

    with patch(f"{COORD}.asyncio.sleep", new=AsyncMock()):
        coordinator = await _start(hass, entry, fakes, configure=configure)
        await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) == "fresh")
    assert coordinator.data[AGENT_ONLINE] is True
    await coordinator.async_shutdown()


async def test_missing_permission_raises_repair_and_clears_when_granted(
    hass: HomeAssistant,
):
    from homeassistant.helpers import issue_registry as ir

    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []

    def configure(f):
        f.info = {
            **_INFO,
            "permissions": {"notification_access": False, "bluetooth_connect": True},
        }

    coordinator = await _start(hass, entry, fakes, configure=configure)
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    registry = ir.async_get(hass)
    issue_id = f"agent_permissions_{entry.entry_id}"
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    fakes[0].info = {
        **_INFO,
        "permissions": {"notification_access": True, "bluetooth_connect": True},
    }
    await coordinator.async_agent_refresh_info()
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    await coordinator.async_shutdown()


async def test_agent_setup_prefers_cloud_deep_link(hass: HomeAssistant):
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    sent: list[dict] = []

    async def fake_cloud(name, commands):
        sent.append(commands[0])
        return {}

    with patch(
        f"{COORD}.BraviaTvCoordinator.async_cloud_command", side_effect=fake_cloud
    ):
        coordinator = await _start(
            hass, entry, fakes, cloud_caps={"application.deep_link": ("post",)}
        )
        await coordinator.async_agent_setup()
    assert sent and sent[0]["args"]["uri"] == agentmod.SETUP_DEEP_LINK
    coordinator.client.exec_command.assert_not_called()
    await coordinator.async_shutdown()


async def test_agent_setup_skips_tv_when_everything_is_granted(hass: HomeAssistant):
    from homeassistant.exceptions import ServiceValidationError

    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    sent: list[dict] = []

    def configure(f):
        f.info = {
            **_INFO,
            "permissions": {
                "notification_access": True,
                "bluetooth_connect": True,
                "install_packages": True,
            },
        }

    async def fake_cloud(name, commands):
        sent.append(commands[0])
        return {}

    with patch(
        f"{COORD}.BraviaTvCoordinator.async_cloud_command", side_effect=fake_cloud
    ):
        coordinator = await _start(
            hass,
            entry,
            fakes,
            configure=configure,
            cloud_caps={"application.deep_link": ("post",)},
        )
        await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
        with pytest.raises(ServiceValidationError) as err:
            await coordinator.async_agent_setup()
    assert err.value.translation_key == "agent_setup_complete"
    assert not sent
    coordinator.client.exec_command.assert_not_called()
    await coordinator.async_shutdown()


async def test_agent_setup_launches_when_a_grant_is_missing(hass: HomeAssistant):
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    sent: list[dict] = []

    def configure(f):
        f.info = {
            **_INFO,
            "permissions": {
                "notification_access": True,
                "bluetooth_connect": True,
                "install_packages": False,
            },
        }

    async def fake_cloud(name, commands):
        sent.append(commands[0])
        return {}

    with patch(
        f"{COORD}.BraviaTvCoordinator.async_cloud_command", side_effect=fake_cloud
    ):
        coordinator = await _start(
            hass,
            entry,
            fakes,
            configure=configure,
            cloud_caps={"application.deep_link": ("post",)},
        )
        await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
        await coordinator.async_agent_setup()
    assert sent and sent[0]["args"]["uri"] == agentmod.SETUP_DEEP_LINK
    await coordinator.async_shutdown()


async def test_agent_update_entity_flow(hass: HomeAssistant):
    """Manifest → latest version; install stages the APK and opens the updater."""
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    manifest = {
        "version": "9.9.9",
        "url": "https://x/bravia-agent.apk",
        "sha256": "ab" * 32,
        "notes": "n",
    }
    sent: list[dict] = []

    async def fake_cloud(name, commands):
        sent.append(commands[0])
        return {}

    with (
        patch(
            f"{COORD}.agentmod.async_fetch_manifest",
            new=AsyncMock(return_value=manifest),
        ),
        patch(
            f"{COORD}.BraviaTvCoordinator.async_cloud_command", side_effect=fake_cloud
        ),
    ):
        coordinator = await _start(
            hass, entry, fakes, cloud_caps={"application.deep_link": ("post",)}
        )
        await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
        assert coordinator.agent_version == "0.1.0"
        assert coordinator.agent_latest["version"] == "9.9.9"
        await coordinator.async_agent_install_update()
    assert fakes[0].staged == ("https://x/bravia-agent.apk", "ab" * 32, "9.9.9")
    assert sent and sent[0]["args"]["uri"] == agentmod.UPDATE_DEEP_LINK
    await coordinator.async_shutdown()


# -- picture settings over REST (R3) ----------------------------------------

_PICTURE = {
    "targets": {
        "brightness": {
            "value": "50",
            "available": True,
            "candidates": [{"min": 0, "max": 50, "step": 1}],
        },
        "pictureMode": {
            "value": "custom",
            "available": True,
            "candidates": [{"value": "custom"}],
        },
        "contrast": {
            "value": "90",
            "available": True,
            "candidates": [{"min": 0, "max": 100, "step": 1}],
        },
        "sensorAutoAdjustment": {
            "value": "off",
            "available": True,
            "candidates": [{"value": "on"}, {"value": "off"}],
        },
        "autoLuminanceLevel": {
            "value": "off",
            "available": False,
            "candidates": [{"value": "on"}, {"value": "off"}],
        },
        "colorTemperature": {
            "value": "expert2",
            "available": True,
            "candidates": [{"value": "cool"}, {"value": "expert2"}],
        },
        "wideMode": {"value": "", "available": False, "candidates": []},
    }
}
_REST_OK = {
    "available": True,
    "scheme": "http",
    "auth": "none",
    "write_authorized": False,
}


def _with_picture(rest=None):
    def configure(f):
        f.state_doc = {
            **_DOC,
            "rest": rest or _REST_OK,
            "picture": _PICTURE,
            "power_saving": "off",
        }

    return configure


async def test_rest_sync_sends_settings_and_key(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            "rest_psk": "k3y",
            "picture_poll_s": 30,
            "picture_event_refresh": False,
        },
        **{CONF_AGENT_TOKEN: "tok"},
    )
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes, configure=_with_picture())
    await _until(lambda: fakes[0].picture_configs and fakes[0].psk_calls)
    assert fakes[0].picture_configs[0] == (30, False, [])
    assert fakes[0].psk_calls == ["k3y"]
    assert coordinator.rest_writable
    await coordinator.async_shutdown()


async def test_rest_sync_forgets_key_when_option_cleared(hass: HomeAssistant):
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    coordinator = await _start(
        hass, entry, fakes, configure=_with_picture({**_REST_OK, "auth": "psk"})
    )
    await _until(lambda: fakes[0].forget_calls == 1)
    assert not fakes[0].psk_calls
    assert not coordinator.rest_writable
    await coordinator.async_shutdown()


async def test_picture_refresh_on_grpc_picture_set_changes(hass: HomeAssistant):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes, configure=_with_picture())
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    fake = fakes[0]

    def advance():
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))

    coordinator._state["display_setting.picture_mode"] = "custom"
    coordinator._apply_delta("display_setting.picture_mode", "cinema")
    coordinator._apply_delta("system_setting.input", "x")  # first value: no trigger
    advance()
    await _until(lambda: fake.refreshes == 1)
    video = "playback_control.signal_info.video"
    coordinator._apply_delta(video, [{"hdr_type": "sdr", "width": 1920}])  # first
    advance()
    await _until(lambda: fake.refreshes == 2)
    coordinator._apply_delta(video, [{"hdr_type": "sdr", "width": 3840}])
    coordinator._apply_delta(video, None)  # no video: SDR values on the TV
    coordinator._apply_delta(video, [{"hdr_type": "sdr", "width": 1280}])
    advance()
    await hass.async_block_till_done()
    assert fake.refreshes == 2  # resolution / no video / SDR: same picture set
    coordinator._apply_delta(video, None)
    coordinator._apply_delta(video, [{"hdr_type": "hdr10", "width": 3840}])
    advance()
    await _until(lambda: fake.refreshes == 3)
    coordinator._apply_delta(video, None)  # HDR stopped: TV back on SDR values
    advance()
    await _until(lambda: fake.refreshes == 4)
    coordinator._apply_delta(video, [{"hdr_type": "hdr10", "width": 3840}])
    advance()
    await _until(lambda: fake.refreshes == 5)
    await coordinator.async_shutdown()


async def test_picture_refresh_off_when_option_disabled(hass: HomeAssistant):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = _entry(
        hass, options={"picture_event_refresh": False}, **{CONF_AGENT_TOKEN: "tok"}
    )
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes, configure=_with_picture())
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    coordinator._state["display_setting.picture_mode"] = "custom"
    coordinator._apply_delta("display_setting.picture_mode", "cinema")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert fakes[0].refreshes == 0
    await coordinator.async_shutdown()


async def test_picture_write_errors_are_translated(hass: HomeAssistant):
    from homeassistant.exceptions import HomeAssistantError

    entry = _entry(hass, options={"rest_psk": "k"}, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes, configure=_with_picture())
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    await coordinator.async_set_picture("contrast", "91")
    assert fakes[0].picture_writes == [("contrast", "91")]
    for code, key in (
        ("rest_unauthorized", "rest_key_rejected"),
        ("rest_unavailable", "rest_unavailable"),
        ("invalid_value", "picture_write_refused"),
    ):
        fakes[0].write_error = agentmod.AgentRequestError(code, 403)
        with pytest.raises(HomeAssistantError) as err:
            await coordinator.async_set_picture("contrast", "91")
        assert err.value.translation_key == key
    await coordinator.async_shutdown()


async def test_rejected_key_raises_repair(hass: HomeAssistant):
    from homeassistant.helpers import issue_registry as ir

    entry = _entry(hass, options={"rest_psk": "wrong"}, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    coordinator = await _start(
        hass,
        entry,
        fakes,
        configure=_with_picture({**_REST_OK, "auth": "psk", "write_authorized": False}),
    )
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    issue_id = f"rest_key_rejected_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    fakes[0].events.put_nowait(
        (
            "state",
            {
                **fakes[0].state_doc,
                "rest": {**_REST_OK, "auth": "psk", "write_authorized": True},
            },
        )
    )
    await _until(lambda: ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None)
    await coordinator.async_shutdown()


async def test_picture_entities_and_writes(hass: HomeAssistant):
    from custom_components.bravia_tv_grpc.entity import async_add_picture_entities
    from custom_components.bravia_tv_grpc.number import BraviaTvPictureNumber
    from custom_components.bravia_tv_grpc.select import BraviaTvPictureSelect
    from custom_components.bravia_tv_grpc.sensor import BraviaTvPictureSensor
    from custom_components.bravia_tv_grpc.switch import BraviaTvPictureSwitch

    entry = _entry(hass, options={"rest_psk": "k"}, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes, configure=_with_picture())
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)

    added: list = []
    async_add_picture_entities(
        coordinator, entry, added.extend, lambda c, t, k: BraviaTvPictureSensor(c, t, k)
    )
    names = sorted(e.target for e in added)
    # brightness / pictureMode stay with gRPC
    assert names == [
        "autoLuminanceLevel",
        "colorTemperature",
        "contrast",
        "sensorAutoAdjustment",
        "wideMode",
    ]

    contrast = BraviaTvPictureNumber(coordinator, "contrast")
    assert (
        contrast.native_min_value,
        contrast.native_max_value,
        contrast.native_step,
    ) == (0, 100, 1)
    assert contrast.native_value == 90 and contrast.available
    await contrast.async_set_native_value(91.0)
    master = BraviaTvPictureSwitch(coordinator, "sensorAutoAdjustment")
    assert master.is_on is False
    await master.async_turn_on()
    auto = BraviaTvPictureSwitch(coordinator, "autoLuminanceLevel")
    assert not auto.available  # the TV marks it unavailable while the master is off
    temp = BraviaTvPictureSelect(coordinator, "colorTemperature")
    assert temp.options == ["cool", "expert2"] and temp.current_option == "expert2"
    wide = BraviaTvPictureSelect(coordinator, "wideMode")
    assert "vertical_fit" in wide.options and not wide.available  # HDMI-only, on an app
    assert fakes[0].picture_writes == [
        ("contrast", "91"),
        ("sensorAutoAdjustment", "on"),
    ]
    await coordinator.async_shutdown()


async def test_mode_switch_removes_other_picture_entities(hass: HomeAssistant):
    from homeassistant.helpers import entity_registry as er

    from custom_components.bravia_tv_grpc import _remove_other_picture_entities

    entry = _entry(hass)
    reg = er.async_get(hass)
    sensor = reg.async_get_or_create(
        "sensor", DOMAIN, "dev_agent_picture_contrast", config_entry=entry
    )
    number = reg.async_get_or_create(
        "number", DOMAIN, "dev_agent_picture_contrast", config_entry=entry
    )
    ps_sensor = reg.async_get_or_create(
        "sensor", DOMAIN, "dev_agent_power_saving", config_entry=entry
    )
    other = reg.async_get_or_create(
        "sensor", DOMAIN, "dev_agent_illuminance", config_entry=entry
    )
    _remove_other_picture_entities(hass, entry, writable=True)
    assert reg.async_get(sensor.entity_id) is None
    assert reg.async_get(ps_sensor.entity_id) is None
    assert reg.async_get(number.entity_id) is not None
    assert reg.async_get(other.entity_id) is not None
    _remove_other_picture_entities(hass, entry, writable=False)
    assert reg.async_get(number.entity_id) is None
