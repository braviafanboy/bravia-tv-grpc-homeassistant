"""Coordinator tests for the HA TV Agent link (require HA harness)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

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


# A polling helper must keep yielding to the loop even while a test replaces
# asyncio.sleep (the tests patch it process-wide to skip the pairing waits).
_real_sleep = asyncio.sleep


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

    def __init__(self, session, host, port=8585, token=None, fingerprint=None):
        self.host = host
        self.port = port
        self.token = token
        self.fingerprint = fingerprint  # SHA-256 of the pinned certificate, if any
        self.fingerprint_reads = 0
        self.requests: list[tuple[str, bool]] = []  # (call, pinned at the time)
        self.info = dict(_INFO)
        self.state_doc = dict(_DOC)
        self.events: asyncio.Queue = asyncio.Queue()
        self.pair_tokens: list[str] = []
        # An agent from before 0.10 unless ``by_code()`` is called: the
        # launch opens a window, and the first to ask gets the token. From
        # 0.10 on a request gets a code, the viewer allows or denies it on
        # the TV (``accepted``, ``denied``), and only then is it collected.
        self.legacy = True
        self.window_open = False
        self.pending: str | None = None
        self.revealed = False
        self.accepted = False
        self.denied = False
        self.busy_for: int | None = None  # someone else's request is waiting
        self.lapse = False  # the request lapses unanswered
        self.launches = 0
        self.pairing: list[str] = []  # what happened, in order
        self.setup_armed = 0
        self.has_setup_call = True
        self.ambient_calls: list[tuple[str, dict]] = []
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
    def presented(self):
        """SHA-256 of the certificate this fake "presents"."""
        return agentmod.fingerprint_of(self.PRESENTED)

    @property
    def pinned(self):
        return self.fingerprint is not None

    def pin(self, fingerprint):
        self.fingerprint = fingerprint

    def _handshake(self, call="request"):
        if self.fingerprint is not None and self.fingerprint != self.presented:
            raise agentmod.AgentFingerprintMismatch("pin", self.presented)
        self.requests.append((call, self.pinned))

    async def async_fetch_fingerprint(self):
        self.fingerprint_reads += 1
        return self.presented

    async def async_info(self):
        self._handshake("info")
        return self.info

    async def async_state(self):
        self._handshake("state")
        if not self.token or self.token == self.reject_token:
            raise agentmod.AgentUnauthorized("state")
        return self.state_doc

    async def async_ambient(self, sub, body):
        self.ambient_calls.append((sub, body))
        return {"enabled": False, "state": "off", "curve": [9, 9, 11, 15, 25, 25]}

    def by_code(self):
        """Make this an agent from 0.10 on, which pairs by a compared code."""
        self.legacy = False
        self.info = {
            **self.info,
            "pairing": {"paired": bool(self.token), "method": "compare"},
        }

    def launched(self):
        """The TV launched the agent app (the integration's gRPC app launch).
        To an older agent that opens its pairing window; to one that pairs by
        code it only brings the question before the viewer."""
        self.launches += 1
        self.pairing.append("launch")
        if self.legacy:
            self.window_open = True

    async def async_pair_request(self, name):
        self._handshake("pair")
        self.pairing.append("request")
        if self.legacy:
            if self.window_open and self.pair_tokens:
                self.token = self.pair_tokens.pop(0)
                return self.token
            raise agentmod.AgentPairingClosed("closed")
        if self.busy_for is not None:
            raise agentmod.AgentPairingBusy(self.busy_for)
        self.pending = "request-1"
        self.revealed = self.accepted = self.denied = False
        return agentmod.AgentPairRequest(self.pending, b"s" * 32, b"n" * 32, 30)

    async def async_pair_reveal(self, asked):
        self._handshake("pair")
        self.pairing.append("reveal")
        if asked.request_id != self.pending:
            raise agentmod.AgentPairingClosed("gone")
        self.revealed = True
        return agentmod.pairing_code(asked.secret, asked.nonce, self.fingerprint)

    async def async_pair_collect(self, request_id):
        self._handshake("pair")
        if "collect" not in self.pairing:
            self.pairing.append("collect")
        if request_id != self.pending or self.lapse:
            raise agentmod.AgentPairingClosed("lapsed")
        if self.denied:
            raise agentmod.AgentPairingDenied("denied")
        if not self.accepted or not self.pair_tokens:
            return None
        self.pending = None
        self.token = self.pair_tokens.pop(0)
        return self.token

    async def async_arm_setup(self):
        self.setup_armed += 1
        self.pairing.append("setup")
        return self.has_setup_call

    async def async_events(self):
        self._handshake("events")
        self.stream_started.set()
        while True:
            ev = await self.events.get()
            if ev is None:
                raise agentmod.AgentError("ended")
            yield ev

    async def async_artwork(self):
        return b"png"

    async def async_stage_update(self, url, sha256, version):
        if not sha256 or not version:  # as the real client
            raise agentmod.AgentError("update: no checksum or version")
        self.staged = (url, sha256, version)

    async def async_picture_config(self, poll_s, event_refresh, watch):
        self.picture_configs.append((poll_s, event_refresh, list(watch)))
        return {}

    async def async_set_psk(self, psk):
        self._handshake("psk")
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
        await _real_sleep(0.01)


async def _start(hass, entry, fake_holder, *, configure=None, cloud_caps=None):
    """Build and start a coordinator whose agent client is a FakeAgent.

    The agent task starts eagerly inside ``async_start``, so anything the
    fake must know (pair tokens, …) is applied by ``configure`` at creation.
    The fakes never open sockets.
    """

    def factory(session, host, port=8585, token=None, fingerprint=None):
        fake = FakeAgent(session, host, port, token, fingerprint)
        if configure:
            configure(fake)
        fake_holder.append(fake)
        return fake

    with (
        patch(f"{COORD}.agentmod.AgentClient", side_effect=factory),
        patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()),
    ):
        client = _client()

        def exec_command(path, value, *args, **kwargs):
            # The app launch reaches the agent on the TV.
            if path == "system_setting.application" and (
                value == agentmod.AGENT_PACKAGE
            ):
                for fake in fake_holder:
                    fake.launched()
            return True

        client.exec_command.side_effect = exec_command
        coordinator = BraviaTvCoordinator(hass, entry, client)
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
    assert CONF_AGENT_CERT not in entry.data
    assert coordinator.data[AGENT_ONLINE] is True
    # The certificate was read and pinned before the pairing request went
    # out: nothing but the first, tokenless info call ran unpinned.
    fake = fakes[0]
    assert fake.fingerprint_reads == 1
    assert [call for call, pinned in fake.requests if not pinned] == ["info"]
    assert ("pair", True) in fake.requests
    await coordinator.async_shutdown()


async def test_never_pairs_through_the_cloud_deep_link(hass: HomeAssistant):
    """The cloud link put the token through Sony's service and into the TV's
    system log; pairing is local only, even when the cloud link exists."""
    entry = _entry(hass)
    fakes: list[FakeAgent] = []
    sent: list[dict] = []

    async def fake_cloud(name, commands):
        sent.append(commands[0])
        return {}

    with (
        patch(f"{COORD}.asyncio.sleep", new=AsyncMock()),
        patch(
            f"{COORD}.BraviaTvCoordinator.async_cloud_command", side_effect=fake_cloud
        ),
    ):
        coordinator = await _start(
            hass,
            entry,
            fakes,
            cloud_caps={"application.deep_link": ("post",)},
            configure=lambda f: f.pair_tokens.append("local"),
        )
        await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) == "local")
    coordinator.client.exec_command.assert_any_call(
        "system_setting.application", agentmod.AGENT_PACKAGE
    )
    assert not [c for c in sent if c["name"] == "application.deep_link"]
    await coordinator.async_shutdown()


def _by_code(*tokens):
    """Configure a fake as an agent that pairs by code, with tokens to give."""

    def configure(fake):
        fake.by_code()
        fake.pair_tokens.extend(tokens)

    return configure


async def _pairing_flow(hass, entry, coordinator, *, prefix="agent_pairing_"):
    """The repair's fix flow for this entry, once the repair is there."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.bravia_tv_grpc import repairs

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    issue_id = f"{prefix}{entry.entry_id}"
    registry = ir.async_get(hass)
    await _until(lambda: registry.async_get_issue(DOMAIN, issue_id) is not None)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue.is_fixable
    flow = await repairs.async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    return flow, registry, issue_id


async def test_an_agent_that_pairs_by_code_waits_for_the_user(hass: HomeAssistant):
    """Pairing with an agent from 0.10 on takes someone at the TV, so nothing
    is started behind the user's back: no request, no launch, just a repair
    to run when they are there."""
    entry = _entry(hass)
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes, configure=_by_code("fresh"))
    _flow, registry, issue_id = await _pairing_flow(hass, entry, coordinator)
    for _ in range(20):
        await _real_sleep(0)
    fake = fakes[0]
    assert registry.async_get_issue(DOMAIN, issue_id).translation_key == "agent_pairing"
    assert fake.pairing == [] and fake.launches == 0
    coordinator.client.exec_command.assert_not_called()
    assert CONF_AGENT_TOKEN not in entry.data
    assert coordinator.data.get(AGENT_ONLINE) is not True
    await coordinator.async_shutdown()


async def test_pairing_by_code_through_the_repair(hass: HomeAssistant):
    """The whole of it: the user starts the repair, Home Assistant asks the
    agent, reveals its secret, shows the code and opens the agent on the TV;
    the token arrives once the viewer has allowed the request there."""
    entry = _entry(hass)
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes, configure=_by_code("fresh"))
    flow, registry, issue_id = await _pairing_flow(hass, entry, coordinator)
    fake = fakes[0]

    form = await flow.async_step_init()
    assert form["type"] == "form" and form["step_id"] == "start"
    form = await flow.async_step_start({})
    assert form["type"] == "form" and form["step_id"] == "compare"
    # The code shown is the one for this request and the pinned certificate.
    code = agentmod.pairing_code(b"s" * 32, b"n" * 32, fake.presented)
    assert form["description_placeholders"]["code"] == f"{code[:3]} {code[3:]}"
    # Asked and revealed over the pinned connection, and only then launched.
    assert fake.pairing[:2] == ["request", "reveal"]
    assert fake.pairing.index("launch") > fake.pairing.index("reveal")
    assert fake.fingerprint == fake.presented and fake.launches == 1
    assert ("pair", False) not in fake.requests

    # Submitting before the viewer has answered: not yet, and the code stays.
    with patch("custom_components.bravia_tv_grpc.repairs._ANSWER_WAIT", 0.05):
        again = await flow.async_step_compare({})
    assert again["type"] == "form" and again["errors"] == {"base": "not_allowed_yet"}
    assert again["description_placeholders"]["code"] == f"{code[:3]} {code[3:]}"
    assert CONF_AGENT_TOKEN not in entry.data

    # The viewer allows it on the TV. The token is collected without waiting
    # for the user to come back to Home Assistant.
    fake.accepted = True
    await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) == "fresh")
    assert entry.data[CONF_AGENT_FINGERPRINT] == fake.presented
    await _until(lambda: registry.async_get_issue(DOMAIN, issue_id) is None)
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    done = await flow.async_step_compare({})
    assert done["type"] == "create_entry"
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    ("answer", "reason"),
    [("denied", "pairing_denied"), ("lapse", "pairing_lapsed")],
)
async def test_a_request_denied_or_left_unanswered_pairs_nothing(
    hass: HomeAssistant, answer, reason
):
    entry = _entry(hass)
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes, configure=_by_code("unused"))
    flow, registry, issue_id = await _pairing_flow(hass, entry, coordinator)
    fake = fakes[0]
    await flow.async_step_init()
    await flow.async_step_start({})
    setattr(fake, answer, True)
    result = await flow.async_step_compare({})
    assert result["type"] == "abort" and result["reason"] == reason
    assert CONF_AGENT_TOKEN not in entry.data
    assert fake.pair_tokens == ["unused"]
    # The repair stays, for another try.
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    await coordinator.async_shutdown()


async def test_pairing_is_not_started_while_another_request_waits(
    hass: HomeAssistant,
):
    """Something else on the network asked the agent to pair a moment ago.
    The user is told, and the agent is not opened on the TV."""
    entry = _entry(hass)
    fakes: list[FakeAgent] = []

    def configure(fake):
        fake.by_code()
        fake.busy_for = 21
        fake.pair_tokens.append("for-the-other-one")

    coordinator = await _start(hass, entry, fakes, configure=configure)
    flow, _registry, _issue_id = await _pairing_flow(hass, entry, coordinator)
    fake = fakes[0]
    await flow.async_step_init()
    result = await flow.async_step_start({})
    assert result["type"] == "abort" and result["reason"] == "pairing_busy"
    assert result["description_placeholders"]["seconds"] == "21"
    assert fake.launches == 0
    coordinator.client.exec_command.assert_not_called()
    assert CONF_AGENT_TOKEN not in entry.data
    assert fake.pair_tokens == ["for-the-other-one"]
    await coordinator.async_shutdown()


async def test_pairing_cannot_start_while_the_tv_is_off(hass: HomeAssistant):
    entry = _entry(hass)
    fakes: list[FakeAgent] = []
    coordinator = await _start(hass, entry, fakes, configure=_by_code("fresh"))
    flow, _registry, _issue_id = await _pairing_flow(hass, entry, coordinator)
    coordinator._state["power"] = False
    await flow.async_step_init()
    result = await flow.async_step_start({})
    assert result["type"] == "abort" and result["reason"] == "agent_unreachable"
    assert fakes[0].pairing == []
    await coordinator.async_shutdown()


async def test_a_rejected_token_asks_for_pairing_again(hass: HomeAssistant):
    """The agent no longer takes the stored token (its pairing was cleared on
    the TV): the repair comes up, and running it pairs again."""
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "stale"})
    fakes: list[FakeAgent] = []

    def configure(fake):
        fake.by_code()
        fake.reject_token = "stale"
        fake.pair_tokens.append("fresh")

    with patch(f"{COORD}.asyncio.sleep", new=AsyncMock()):
        coordinator = await _start(hass, entry, fakes, configure=configure)
        flow, _registry, _issue_id = await _pairing_flow(hass, entry, coordinator)
    fake = fakes[0]
    assert fake.launches == 0
    await flow.async_step_init()
    await flow.async_step_start({})
    fake.accepted = True
    await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) == "fresh")
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    await coordinator.async_shutdown()


async def test_pairs_with_an_agent_from_before_requests(hass: HomeAssistant):
    """Agents before 0.10 know no requests: the launch opens a window and the
    token goes to whoever asks while it is open. They are still paired with
    as before, without the user."""
    from homeassistant.helpers import issue_registry as ir

    entry = _entry(hass)
    fakes: list[FakeAgent] = []
    coordinator = await _start(
        hass, entry, fakes, configure=lambda f: f.pair_tokens.append("old-style")
    )
    await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) == "old-style")
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    fake = fakes[0]
    assert fake.pairing[:3] == ["request", "launch", "request"]
    assert fake.launches == 1
    issue_id = f"agent_pairing_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
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


def _pinned_to_old(hass: HomeAssistant, **extra):
    """An entry paired with an agent whose certificate was "cert-OLD"."""
    return _entry(
        hass,
        **{
            CONF_AGENT_TOKEN: "old",
            CONF_AGENT_FINGERPRINT: agentmod.fingerprint_of(b"cert-OLD"),
            **extra,
        },
    )


async def test_new_certificate_is_not_paired_with_automatically(hass: HomeAssistant):
    """A pinned fingerprint that no longer matches (the agent was reinstalled,
    or something else answers on its port) raises a repair. Nothing is sent to
    the new peer, and the stored pairing is left alone, until the user says
    so."""
    from homeassistant.helpers import issue_registry as ir

    entry = _pinned_to_old(hass)
    fakes: list[FakeAgent] = []
    with patch(f"{COORD}.asyncio.sleep", new=AsyncMock()):
        coordinator = await _start(
            hass, entry, fakes, configure=lambda f: f.pair_tokens.append("renewed")
        )
        issue_id = f"agent_identity_changed_{entry.entry_id}"
        await _until(
            lambda: ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
        )
        for _ in range(20):  # let the loop go round a few times
            await _real_sleep(0)
    fake = fakes[0]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue.is_fixable
    assert issue.translation_key == "agent_identity_changed"
    assert issue.data["entry_id"] == entry.entry_id
    # The new certificate's fingerprint is shown, so it can be compared.
    assert fake.presented.startswith(issue.data["fingerprint"].replace(" ", ""))
    # Untouched: still the old pairing, nothing pinned anew, no request got
    # through to the stranger, no pairing was attempted.
    assert entry.data[CONF_AGENT_TOKEN] == "old"
    assert entry.data[CONF_AGENT_FINGERPRINT] == agentmod.fingerprint_of(b"cert-OLD")
    assert fake.requests == []
    assert fake.pair_tokens == ["renewed"]
    assert fake.psk_calls == []
    assert coordinator.data.get(AGENT_ONLINE) is not True
    coordinator.client.exec_command.assert_not_called()  # the agent was not launched
    await coordinator.async_shutdown()


async def test_confirming_the_repair_pairs_with_the_new_certificate(
    hass: HomeAssistant,
):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.bravia_tv_grpc import repairs

    entry = _pinned_to_old(hass)
    fakes: list[FakeAgent] = []
    with patch(f"{COORD}.asyncio.sleep", new=AsyncMock()):
        coordinator = await _start(
            hass, entry, fakes, configure=lambda f: f.pair_tokens.append("renewed")
        )
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        issue_id = f"agent_identity_changed_{entry.entry_id}"
        registry = ir.async_get(hass)
        await _until(lambda: registry.async_get_issue(DOMAIN, issue_id) is not None)
        issue = registry.async_get_issue(DOMAIN, issue_id)

        flow = await repairs.async_create_fix_flow(hass, issue_id, issue.data)
        flow.hass = hass
        form = await flow.async_step_init()
        assert form["type"] == "form" and form["step_id"] == "confirm"
        assert (
            form["description_placeholders"]["fingerprint"] == issue.data["fingerprint"]
        )
        done = await flow.async_step_confirm({})
        assert done["type"] == "create_entry"

        await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) == "renewed")
        await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    fake = fakes[0]
    assert entry.data[CONF_AGENT_FINGERPRINT] == fake.presented
    assert fake.fingerprint == fake.presented
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    await coordinator.async_shutdown()


async def test_a_new_certificate_is_proved_by_the_code(hass: HomeAssistant):
    """With an agent that pairs by code, confirming the changed certificate
    leads straight into the pairing: the code is tied to the certificate, so
    the viewer's answer on the real TV is what proves the new one."""
    entry = _pinned_to_old(hass)
    fakes: list[FakeAgent] = []
    # (No patched sleep: the wait for the viewer's answer polls with it.)
    coordinator = await _start(hass, entry, fakes, configure=_by_code("renewed"))
    flow, registry, issue_id = await _pairing_flow(
        hass, entry, coordinator, prefix="agent_identity_changed_"
    )
    fake = fakes[0]
    form = await flow.async_step_init()
    assert form["type"] == "form" and form["step_id"] == "confirm"
    assert fake.pairing == []  # nothing before the user confirms
    form = await flow.async_step_confirm({})
    assert form["type"] == "form" and form["step_id"] == "compare"
    code = agentmod.pairing_code(b"s" * 32, b"n" * 32, fake.presented)
    assert form["description_placeholders"]["code"] == f"{code[:3]} {code[3:]}"
    assert entry.data.get(CONF_AGENT_TOKEN) is None  # the old pairing is gone
    fake.accepted = True
    await _until(lambda: entry.data.get(CONF_AGENT_TOKEN) == "renewed")
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    assert entry.data[CONF_AGENT_FINGERPRINT] == fake.presented
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert registry.async_get_issue(DOMAIN, f"agent_pairing_{entry.entry_id}") is None
    await coordinator.async_shutdown()


async def test_known_agent_returning_clears_the_repair(hass: HomeAssistant):
    """The stranger goes away and the agent we are paired with answers again:
    the link resumes with the stored pairing and the repair clears itself."""
    from homeassistant.helpers import issue_registry as ir

    entry = _pinned_to_old(hass)
    fakes: list[FakeAgent] = []
    with patch(f"{COORD}._AGENT_IDENTITY_RECHECK", 0.01):
        coordinator = await _start(hass, entry, fakes)
        issue_id = f"agent_identity_changed_{entry.entry_id}"
        registry = ir.async_get(hass)
        await _until(lambda: registry.async_get_issue(DOMAIN, issue_id) is not None)
        fakes[0].PRESENTED = b"cert-OLD"  # the real agent is back
        await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert entry.data[CONF_AGENT_TOKEN] == "old"
    await coordinator.async_shutdown()


async def test_stranger_gets_no_key_and_no_dashboard_token(hass: HomeAssistant):
    """What an impostor on the agent's port used to be handed, it no longer
    is: neither the TV's IP-control key nor a Home Assistant access token,
    whatever its state document claims."""

    class Impostor(FakeAgent):
        PRESENTED = b"cert-IMPOSTOR"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.ha_tokens: list[str] = []
            self.pair_tokens.append("impostor-token")
            self.state_doc = {
                **_DOC,
                "rest": {"auth": "none", "available": True},
                "dashboard": {"showing": True, "mode": "full", "token_needed": True},
            }

        async def async_dashboard_token(self, token, expires_in):
            self.ha_tokens.append(token)

    await hass.auth.async_create_user("owner")
    entry = _pinned_to_old(hass)
    hass.config_entries.async_update_entry(entry, options={"rest_psk": "tv-key"})
    fakes: list[Impostor] = []

    def factory(session, host, port=8585, token=None, fingerprint=None):
        fake = Impostor(session, host, port, token, fingerprint)
        fakes.append(fake)
        return fake

    with (
        patch(f"{COORD}.agentmod.AgentClient", side_effect=factory),
        patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()),
        patch(f"{COORD}.asyncio.sleep", new=AsyncMock()),
    ):
        coordinator = BraviaTvCoordinator(hass, entry, _client())
        coordinator._state["power"] = True
        await coordinator.async_start()
        for _ in range(50):
            await _real_sleep(0)
    impostor = fakes[0]
    assert impostor.psk_calls == []
    assert impostor.ha_tokens == []
    assert impostor.pair_tokens == ["impostor-token"]  # never consumed
    assert entry.data[CONF_AGENT_TOKEN] == "old"
    # No display user was conjured up for it either.
    assert not [
        u for u in await hass.auth.async_get_users() if "HA TV Agent" in (u.name or "")
    ]
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


async def test_agent_setup_without_the_cloud_link_announces_itself_first(
    hass: HomeAssistant,
):
    """Without the cloud deep link the app is launched bare. An agent from
    0.10 on opens its permission screens for such a launch only when it was
    told just before that this is what the launch is for."""
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []

    def configure(f):
        f.info = {
            **_INFO,
            "permissions": {"notification_access": False},
            "pairing": {"paired": True, "method": "compare"},
        }

    coordinator = await _start(hass, entry, fakes, configure=configure)
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    fake = fakes[0]
    await coordinator.async_agent_setup()
    assert fake.pairing[-2:] == ["setup", "launch"]
    assert fake.setup_armed == 1 and fake.launches == 1

    # It could not be told (the link dropped): then it is not launched at
    # all, rather than launched with a meaning it would get wrong.
    async def unreachable():
        raise agentmod.AgentError("gone")

    fake.async_arm_setup = unreachable
    with pytest.raises(HomeAssistantError):
        await coordinator.async_agent_setup()
    assert fake.launches == 1
    coordinator._set_agent_online(False)
    with pytest.raises(HomeAssistantError):
        await coordinator.async_agent_setup()
    assert fake.launches == 1
    await coordinator.async_shutdown()


async def test_agent_setup_launches_an_older_agent_as_before(hass: HomeAssistant):
    """An agent from before 0.10 has no setup call and is simply launched."""
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []

    def configure(f):
        f.legacy = True
        f.info = {**_INFO, "permissions": {"notification_access": False}}

    coordinator = await _start(hass, entry, fakes, configure=configure)
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    await coordinator.async_agent_setup()
    assert fakes[0].setup_armed == 0 and fakes[0].launches == 1
    await coordinator.async_shutdown()


async def test_agent_update_needs_the_manifests_checksum(hass: HomeAssistant):
    """An update is staged only with the SHA-256 the download must have. A
    manifest without one (a release whose checksum step failed) is refused
    here, and nothing is opened on the TV."""
    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    manifest = {"version": "9.9.9", "url": "https://x/bravia-agent.apk", "sha256": ""}
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
        with pytest.raises(HomeAssistantError, match="Could not stage"):
            await coordinator.async_agent_install_update()
    assert not hasattr(fakes[0], "staged")
    assert not sent
    coordinator.client.exec_command.assert_not_called()
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
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))

    coordinator._state["display_setting.picture_mode"] = "custom"
    coordinator._apply_delta("display_setting.picture_mode", "cinema")
    coordinator._apply_delta("system_setting.input", "x")  # first value: no trigger
    advance()
    await _until(lambda: fake.refreshes == 1)
    video = "playback_control.signal_info.video"

    def sig(hdr, width):
        # As the TV sends it: a JSON-encoded list (seen live).
        return json.dumps([{"hdr_type": hdr, "width": width, "frame_rate": 23.976}])

    coordinator._apply_delta(video, sig("sdr", 1920))  # first
    advance()
    await _until(lambda: fake.refreshes == 2)
    coordinator._apply_delta(video, sig("sdr", 3840))
    coordinator._apply_delta(video, None)  # no video: SDR values on the TV
    coordinator._apply_delta(video, "[]")
    coordinator._apply_delta(video, sig("sdr", 1280))
    advance()
    await hass.async_block_till_done()
    assert fake.refreshes == 2  # resolution / no video / SDR: same picture set
    coordinator._apply_delta(video, None)
    coordinator._apply_delta(video, sig("hdr10", 3840))
    advance()
    await _until(lambda: fake.refreshes == 3)
    coordinator._apply_delta(video, sig("hdr10", 1920))  # resolution only
    advance()
    await hass.async_block_till_done()
    assert fake.refreshes == 3
    coordinator._apply_delta(video, None)  # HDR stopped: TV back on SDR values
    advance()
    await _until(lambda: fake.refreshes == 4)
    coordinator._apply_delta(video, sig("dolby", 3840))
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


async def test_unreachable_ip_control_repair_without_further_updates(
    hass: HomeAssistant,
):
    """The agent only sends state on change: a timer re-checks once the grace
    has passed (no new document needed), and the repair clears when IP control
    answers again."""
    from datetime import timedelta

    from homeassistant.helpers import issue_registry as ir
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = _entry(hass, **{CONF_AGENT_TOKEN: "tok"})
    fakes: list[FakeAgent] = []
    coordinator = await _start(
        hass, entry, fakes, configure=_with_picture({**_REST_OK, "available": False})
    )
    await _until(lambda: coordinator.data.get(AGENT_ONLINE) is True)
    issue_id = f"rest_unavailable_{entry.entry_id}"
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is None  # within the grace
    assert coordinator._cancel_rest_recheck is not None  # a re-check is scheduled
    # Pretend the grace has passed, then let the scheduled re-check run.
    coordinator._rest_unavailable_since -= timedelta(minutes=4)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3, seconds=5))
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    fakes[0].events.put_nowait(("state", {**fakes[0].state_doc, "rest": _REST_OK}))
    await _until(lambda: registry.async_get_issue(DOMAIN, issue_id) is None)
    assert coordinator._cancel_rest_recheck is None
    await coordinator.async_shutdown()
