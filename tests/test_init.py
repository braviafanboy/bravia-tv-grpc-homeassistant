"""Setup, recovery and unload of a config entry (require HA test harness).

A TV reboot invalidates the gRPC session keys. Recovery must retry transient
failures (HA backs off and self-heals) and only force the user through
re-authentication when the Sony refresh token is genuinely rejected.

A TV that cannot be reached (standby, unplugged) must not take the entry
down: once it has been reached from this install, the entry loads and stays
loaded without it, the TV reads as off, and the coordinator reconnects.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from fake_tv import CAPS_TEXT, CLOUD_DEVICE, CLOUD_STATES, CREDS, FakeTv  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.const import CONF_HOST  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402

from custom_components.bravia_tv_grpc import (  # noqa: E402
    coordinator as coordinator_module,
)
from custom_components.bravia_tv_grpc.bravia_tv_client import (  # noqa: E402
    BraviaTvConnectionError,
)
from custom_components.bravia_tv_grpc.const import (  # noqa: E402
    CONF_GRPC_DEVICE_ID,
    CONF_GRPC_KEYS,
    CONF_GRPC_PORT,
    DATA_ADDRESS_HINTS,
    DOMAIN,
)
from custom_components.bravia_tv_grpc.coordinator import schema_store  # noqa: E402
from custom_components.bravia_tv_grpc.grpc import credentials as cred  # noqa: E402
from custom_components.bravia_tv_grpc.grpc.credentials import (  # noqa: E402
    GrpcCredentialsRefreshError,
)

INTG = "custom_components.bravia_tv_grpc"
COORD = f"{INTG}.coordinator"


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    with patch("homeassistant.components.zeroconf.async_setup", return_value=True):
        yield


def _entry(hass: HomeAssistant, unique_id: str = "dev", host: str = "192.0.2.1"):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        title=f"Bravia TV ({host})",
        data={
            CONF_HOST: host,
            CONF_GRPC_PORT: 40000,
            CONF_GRPC_KEYS: cred.credentials_to_json(CREDS),
            CONF_GRPC_DEVICE_ID: "dev",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _remember_schema(hass: HomeAssistant, entry) -> None:
    """As if the TV had been reached from this install before."""
    await schema_store(hass, entry.entry_id).async_save({"capabilities": CAPS_TEXT})


class _Setup:
    """Runs a real config-entry setup against fake TVs (one per entry)."""

    def __init__(self, hass: HomeAssistant, *, port: int | None = 40000) -> None:
        self.hass = hass
        self.port = port  # what port discovery finds; None = TV not found
        self.ports: dict[str, int | None] = {}  # the same, per host
        self.tvs: list[FakeTv] = []
        self.prepare = lambda tv: None
        self.refresh = AsyncMock(return_value={**CREDS, "hmac_key": "ef" * 32})
        self.resolve = AsyncMock(
            side_effect=lambda hass, host, *a, **k: self.ports.get(host, self.port)
        )

    def _client(self, *args, **kwargs) -> FakeTv:
        tv = FakeTv(*args, **kwargs)
        self.prepare(tv)
        self.tvs.append(tv)
        return tv

    def __enter__(self) -> _Setup:
        self._patches = [
            patch(f"{INTG}.BraviaTvGrpcClient", side_effect=self._client),
            patch(f"{INTG}._resolve_port", new=self.resolve),
            patch(f"{COORD}.async_resolve_port", new=self.resolve),
            # Real mDNS would open a zeroconf socket, which the harness forbids.
            patch(
                f"{INTG}.async_resolve_device_mdns", new=AsyncMock(return_value=None)
            ),
            patch(f"{COORD}.cred.async_refresh_credentials", new=self.refresh),
            patch(
                f"{COORD}.cred.async_get_devices",
                new=AsyncMock(
                    return_value={"devices": [CLOUD_DEVICE | {"device_id": "dev"}]}
                ),
            ),
            patch(
                f"{COORD}.cred.async_get_device_states",
                new=AsyncMock(return_value=CLOUD_STATES),
            ),
            patch(f"{INTG}._fetch_device_hardware", new=AsyncMock(return_value=None)),
            # No waiting between reconnect attempts.
            patch(f"{COORD}._RECONNECT_MIN", 0.01),
            patch(f"{COORD}._RECONNECT_MAX", 0.01),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        for p in reversed(self._patches):
            p.stop()

    async def run(self, entry) -> None:
        await self.hass.config_entries.async_setup(entry.entry_id)
        await self.hass.async_block_till_done()


async def _until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _entity_id(hass: HomeAssistant, entry, domain: str, suffix: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        domain, DOMAIN, f"{entry.unique_id}_{suffix}"
    )
    assert entity_id, f"no {domain} entity for {suffix}"
    return entity_id


# -- key refresh on a rejected handshake ---------------------------------------


async def test_transient_refresh_failure_retries(hass: HomeAssistant) -> None:
    """A reset connection during the cloud key refresh is transient: HA should
    retry, not demand re-authentication."""
    entry = _entry(hass)
    with _Setup(hass) as setup:
        setup.prepare = lambda tv: setattr(tv, "reject_keys", True)
        # No HTTP status -> looks like a dropped connection.
        setup.refresh.side_effect = GrpcCredentialsRefreshError("connection reset")
        await setup.run(entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not list(entry.async_get_active_flows(hass, {"reauth"}))


async def test_rejected_refresh_token_triggers_reauth(hass: HomeAssistant) -> None:
    """A 401 from Sony means the refresh token is dead: only re-authentication
    recovers, so the reauth flow must start."""
    entry = _entry(hass)
    with _Setup(hass) as setup:
        setup.prepare = lambda tv: setattr(tv, "reject_keys", True)
        setup.refresh.side_effect = GrpcCredentialsRefreshError("HTTP 401", status=401)
        await setup.run(entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert list(entry.async_get_active_flows(hass, {"reauth"}))


async def test_refreshed_keys_still_rejected_retries(hass: HomeAssistant) -> None:
    """Fresh, valid cloud keys rejected by the TV (still booting) is transient:
    retry rather than force a re-login that would yield equivalent keys."""
    entry = _entry(hass)
    with _Setup(hass) as setup, patch(f"{COORD}._NEW_KEY_WAIT", 0):
        setup.prepare = lambda tv: setattr(tv, "reject_keys", True)
        await setup.run(entry)
        tv = setup.tvs[0]
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not list(entry.async_get_active_flows(hass, {"reauth"}))
    # The fresh keys were fetched once, stored and given to the client.
    setup.refresh.assert_awaited_once()
    assert tv.rekeys == [("ef" * 32, "k1", "cd" * 32)]
    assert '"' + "ef" * 32 + '"' in entry.data[CONF_GRPC_KEYS]
    # Once with the stored keys, then a few times with the new ones.
    assert len(tv.connects) == 1 + coordinator_module._NEW_KEY_ATTEMPTS
    assert not tv.connected  # nothing left open behind a failed setup


async def test_new_keys_are_given_a_moment_to_reach_the_tv(hass: HomeAssistant) -> None:
    """The TV refuses a key for a moment after Sony has issued it (about two
    seconds, measured on a TV). The handshake is tried again, and setup
    carries on, instead of ending in a retry of the whole setup."""
    entry = _entry(hass)
    with _Setup(hass) as setup, patch(f"{COORD}._NEW_KEY_WAIT", 0):

        def slow_to_learn(tv: FakeTv) -> None:
            tv.reject_keys = True
            connect = tv.connect

            def connect_counting(*args, **kwargs):
                # The stored keys, then the new ones twice, are refused.
                if len(tv.connects) >= 3:
                    tv.reject_keys = False
                return connect(*args, **kwargs)

            tv.connect = connect_counting  # type: ignore[method-assign]

        setup.prepare = slow_to_learn
        await setup.run(entry)
        tv = setup.tvs[0]
        assert entry.state is ConfigEntryState.LOADED
        assert len(tv.connects) == 4 and tv.connected
        setup.refresh.assert_awaited_once()
        await hass.config_entries.async_unload(entry.entry_id)


async def test_keys_refreshed_once_then_setup_succeeds(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with _Setup(hass) as setup:

        def reboot(tv: FakeTv) -> None:
            tv.reject_keys = True
            original = tv.rekey

            def rekey(*args, **kwargs):
                original(*args, **kwargs)
                tv.reject_keys = False  # the new keys are accepted

            tv.rekey = rekey  # type: ignore[method-assign]

        setup.prepare = reboot
        await setup.run(entry)
        assert entry.state is ConfigEntryState.LOADED
        assert setup.tvs[0].connected
        await hass.config_entries.async_unload(entry.entry_id)


# -- a TV that cannot be reached -----------------------------------------------


async def test_schema_error_is_retried_not_a_setup_error(hass: HomeAssistant) -> None:
    """A gRPC error from GetCapabilities (TV still booting, a deadline), or a
    reply with no usable schema, used to strand the entry in "setup error",
    which Home Assistant never retries."""
    entry = _entry(hass)
    with _Setup(hass) as setup:
        setup.prepare = lambda tv: setattr(tv, "caps_text", None)
        await setup.run(entry)
        tv = setup.tvs[0]
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not tv.connected and tv.closed >= 1


async def test_unreachable_before_first_contact_retries_with_a_repair(
    hass: HomeAssistant,
) -> None:
    """Never reached from this install: there is nothing to build entities
    from, so setup waits. The repair is per TV: another TV coming up must not
    clear it."""
    first = _entry(hass, "dev-a", "192.0.2.1")
    second = _entry(hass, "dev-b", "192.0.2.2")
    registry = ir.async_get(hass)
    with _Setup(hass) as setup:
        setup.ports["192.0.2.1"] = None  # the first TV is off
        await setup.run(first)  # sets up every entry of the integration
        assert first.state is ConfigEntryState.SETUP_RETRY
        assert second.state is ConfigEntryState.LOADED
        issue_id = f"grpc_port_unreachable_{first.entry_id}"
        issue = registry.async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.translation_placeholders == {"host": "192.0.2.1"}
        assert (
            registry.async_get_issue(DOMAIN, f"grpc_port_unreachable_{second.entry_id}")
            is None
        )
        # The second TV setting up again must not clear the first one's repair.
        await hass.config_entries.async_reload(second.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get_issue(DOMAIN, issue_id) is not None

        # The first TV comes up: its own repair clears.
        setup.ports["192.0.2.1"] = 40000
        await hass.config_entries.async_reload(first.entry_id)
        await hass.async_block_till_done()
        assert first.state is ConfigEntryState.LOADED
        assert registry.async_get_issue(DOMAIN, issue_id) is None
        for entry in (first, second):
            await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_and_unload_with_the_tv_on(hass: HomeAssistant) -> None:
    """The whole thing against the BRAVIA 8 II's schema: entities exist, the
    schema is kept for a later start without the TV, and unloading closes the
    client and leaves nothing running."""
    entry = _entry(hass)
    with _Setup(hass) as setup:
        await setup.run(entry)
        tv = setup.tvs[0]
        assert entry.state is ConfigEntryState.LOADED
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert not coordinator.offline and tv.pushes_started == 1

        player = hass.states.get(_entity_id(hass, entry, "media_player", "power"))
        assert player.state == "on"
        assert player.attributes["volume_level"] == 0.18
        assert (
            len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))
            > 30
        )
        stored = await schema_store(hass, entry.entry_id).async_load()
        assert stored["capabilities"] == CAPS_TEXT
        # The cloud's capabilities are kept too.
        assert "sleep_timer" in stored["cloud"]["capabilities"]

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]
    assert not tv.connected and tv.closed >= 1


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The TV's announcement at its address, with the whole id...
        (
            "BRAVIA 8 II-0A1B2C3D4E5F60718293A4B5C6D7E8F901234567"
            "._sonysmarthome._tcp.local.",
            "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567",
        ),
        # ...and cut to 60 characters by a long device name: what is left of
        # the id is kept, and is enough to know the TV by later.
        (
            "Living Room BRAVIA 8 II-0a1b2c3d4e5f60718293a4b5c6d7e8f90123"
            "._sonysmarthome._tcp.local.",
            "0a1b2c3d4e5f60718293a4b5c6d7e8f90123",
        ),
        ("Living Room TV._sonysmarthome._tcp.local.", None),
    ],
)
async def test_setup_records_the_id_the_tv_announces(
    hass: HomeAssistant, name: str, expected: str | None
) -> None:
    """An entry paired without the TV's unique id (mDNS was blocked then, or it
    predates the id) picks it up from the announcement at the TV's address."""
    from custom_components.bravia_tv_grpc.const import CONF_DEVICE_UNIQUE_ID

    entry = _entry(hass)
    with _Setup(hass) as setup:
        resolved = AsyncMock(return_value=(40000, name, {}))
        with patch(f"{INTG}.async_resolve_device_mdns", new=resolved):
            await setup.run(entry)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.data.get(CONF_DEVICE_UNIQUE_ID) == expected
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_starts_without_the_tv_once_it_was_reached(hass: HomeAssistant) -> None:
    """Home Assistant restarts while the TV is in standby: the entry loads
    from the stored schema, the TV reads as off (and can be turned on), the
    rest is unavailable, and no repair nags about a TV that is merely off."""
    entry = _entry(hass)
    await _remember_schema(hass, entry)
    with _Setup(hass, port=None) as setup:
        await setup.run(entry)
        assert entry.state is ConfigEntryState.LOADED
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.offline
        assert not ir.async_get(hass).async_get_issue(
            DOMAIN, f"grpc_port_unreachable_{entry.entry_id}"
        )
        player_id = _entity_id(hass, entry, "media_player", "power")
        assert hass.states.get(player_id).state == "off"
        brightness_id = _entity_id(hass, entry, "number", "display_setting.brightness")
        assert hass.states.get(brightness_id).state == "unavailable"
        # Cloud-backed controls are known from last time, and wait for the TV.
        sleep_id = _entity_id(hass, entry, "select", "cloud.sleep_timer")
        assert hass.states.get(sleep_id).state == "unavailable"

        # The TV comes on: found again, no reload, everything live.
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            setup.port = 40000
            coordinator.async_request_reconnect()
            await _until(lambda: not coordinator.offline)
            await hass.async_block_till_done()
            reload.assert_not_called()
        tv = setup.tvs[0]
        assert tv.connected and tv.pushes_started == 1
        assert hass.states.get(player_id).state == "on"
        assert float(hass.states.get(brightness_id).state) == 30
        assert hass.states.get(sleep_id).state != "unavailable"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_tv_going_to_standby_keeps_the_entry_loaded(hass: HomeAssistant) -> None:
    """The connection is lost at runtime. It used to reload the entry into a
    retry loop (every entity gone, a port scan per attempt); now the TV reads
    as off and is found again in the background."""
    entry = _entry(hass)
    with _Setup(hass) as setup:
        await setup.run(entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        tv = setup.tvs[0]
        player_id = _entity_id(hass, entry, "media_player", "power")
        brightness_id = _entity_id(hass, entry, "number", "display_setting.brightness")

        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            tv.reachable = False
            setup.port = None
            coordinator._async_connection_lost("push stream lost")
            await hass.async_block_till_done()
            assert coordinator.offline
            assert entry.state is ConfigEntryState.LOADED
            assert hass.states.get(player_id).state == "off"
            assert hass.states.get(brightness_id).state == "unavailable"
            # Writes say why, instead of failing somewhere in gRPC.
            with pytest.raises(HomeAssistantError) as err:
                await coordinator.async_set_field("volume", 5)
            assert err.value.translation_key == "tv_unreachable"

            # Back on a new port after a reboot.
            tv.reachable = True
            tv.states["volume"] = 22
            setup.port = 41000
            coordinator.async_request_reconnect()
            await _until(lambda: not coordinator.offline)
            await hass.async_block_till_done()
            reload.assert_not_called()
        assert entry.data[CONF_GRPC_PORT] == 41000
        assert tv.port == 41000 and tv.pushes_started == 2
        assert hass.states.get(player_id).state == "on"
        assert hass.states.get(player_id).attributes["volume_level"] == 0.22
        await hass.config_entries.async_unload(entry.entry_id)


async def test_reboot_announcement_seen_while_connected_is_tried_first(
    hass: HomeAssistant,
) -> None:
    """A TV back from a reboot announces its new port before the old
    connection is seen to be dead. That announcement is where to look first
    once it is, instead of waiting for an mDNS lookup."""
    entry = _entry(hass)
    with _Setup(hass) as setup:
        await setup.run(entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        tv = setup.tvs[0]
        coordinator.async_note_discovery("192.0.2.1", 45000)  # still connected
        assert not coordinator.offline
        setup.resolve.reset_mock()
        setup.port = None  # the old port is dead; only the hint can find it
        setup.resolve.side_effect = lambda hass, host, cached, manual, **kw: kw.get(
            "hint"
        )
        coordinator._async_connection_lost("push stream lost")
        await _until(lambda: not coordinator.offline)
        assert tv.port == 45000 and entry.data[CONF_GRPC_PORT] == 45000
        assert setup.resolve.await_args.kwargs["hint"] == 45000
        await hass.config_entries.async_unload(entry.entry_id)


async def test_reconnect_without_a_readable_state_does_not_keep_the_tv_off(
    hass: HomeAssistant,
) -> None:
    """Connected again, but the bulk state read fails: the TV must not stay
    "off" (that was only what unreachable meant), nor stay unreachable."""
    entry = _entry(hass)
    with _Setup(hass) as setup:
        await setup.run(entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        tv = setup.tvs[0]
        player_id = _entity_id(hass, entry, "media_player", "power")
        tv.reachable = False
        coordinator._async_connection_lost("push stream lost")
        await hass.async_block_till_done()
        assert hass.states.get(player_id).state == "off"

        def no_bulk_read(paths=None, timeout=10.0):
            raise BraviaTvConnectionError("GetStates failed: DEADLINE_EXCEEDED")

        tv.get_states = no_bulk_read  # type: ignore[method-assign]
        tv.reachable = True
        coordinator.async_request_reconnect()
        await _until(lambda: not coordinator.offline)
        await hass.async_block_till_done()
        assert tv.pushes_started == 2
        # The cloud's snapshot fills in until the TV pushes its own state.
        assert hass.states.get(player_id).state == "on"
        # ...and the next push is the truth.
        coordinator._apply_delta("power", False)
        await hass.async_block_till_done()
        assert hass.states.get(player_id).state == "off"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_two_missed_liveness_probes_go_offline(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with _Setup(hass) as setup:
        await setup.run(entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        tv = setup.tvs[0]
        tv.reachable = False
        setup.port = None
        await coordinator._async_periodic_refresh(None)
        assert not coordinator.offline  # one slow probe is not a dead TV
        await coordinator._async_periodic_refresh(None)
        assert coordinator.offline
        await hass.config_entries.async_unload(entry.entry_id)


async def test_turn_on_while_unreachable_goes_through_the_cloud(
    hass: HomeAssistant,
) -> None:
    """The point of staying loaded: a TV in deep standby is off the network,
    and Sony's cloud is the only way to wake it."""
    entry = _entry(hass)
    await _remember_schema(hass, entry)
    with _Setup(hass, port=None) as setup:
        await setup.run(entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.offline and coordinator.cloud_supports("power")
        player_id = _entity_id(hass, entry, "media_player", "power")
        with patch(
            f"{COORD}.cloud.async_run_commands", new=AsyncMock(return_value={})
        ) as run:
            setup.port = 40000  # the TV wakes
            await hass.services.async_call(
                "media_player", "turn_on", {"entity_id": player_id}, blocking=True
            )
        commands = run.await_args.args[3]
        assert commands == [
            {"name": "power", "command": "set", "args": {"value": True}}
        ]
        await _until(lambda: not coordinator.offline)
        await hass.async_block_till_done()
        assert hass.states.get(player_id).state == "on"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_reauth_started_when_keys_cannot_be_renewed_while_reconnecting(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    await _remember_schema(hass, entry)
    with _Setup(hass, port=None) as setup:
        await setup.run(entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        tv = setup.tvs[0]
        tv.reject_keys = True
        setup.refresh.side_effect = GrpcCredentialsRefreshError("HTTP 401", status=401)
        setup.port = 40000
        coordinator.async_request_reconnect()
        await _until(lambda: list(entry.async_get_active_flows(hass, {"reauth"})))
        assert entry.state is ConfigEntryState.LOADED
        await hass.config_entries.async_unload(entry.entry_id)


async def test_changed_schema_after_reconnect_reloads(hass: HomeAssistant) -> None:
    """A firmware update changed what the TV advertises: the entities no
    longer fit, so the entry is set up afresh (the one case that still does)."""
    entry = _entry(hass)
    with _Setup(hass) as setup:
        await setup.run(entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        tv = setup.tvs[0]
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            tv.reachable = False
            setup.port = None
            coordinator._async_connection_lost("push stream lost")
            await hass.async_block_till_done()
            tv.caps_text = CAPS_TEXT.replace('"remote_start"', '"remote_start_v2"')
            tv.reachable = True
            setup.port = 40000
            coordinator.async_request_reconnect()
            await _until(lambda: reload.called)
        reload.assert_called_once_with(entry.entry_id)
        await hass.config_entries.async_unload(entry.entry_id)


async def test_unknown_source_is_refused_not_ignored(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import ServiceValidationError

    entry = _entry(hass)
    with _Setup(hass) as setup:
        await setup.run(entry)
        tv = setup.tvs[0]
        player_id = _entity_id(hass, entry, "media_player", "power")
        assert "Netflix" in hass.states.get(player_id).attributes["source_list"]
        await hass.services.async_call(
            "media_player",
            "select_source",
            {"entity_id": player_id, "source": "Netflix"},
            blocking=True,
        )
        assert tv.writes[-1] == ("system_setting.application", "com.netflix.ninja")
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                "media_player",
                "select_source",
                {"entity_id": player_id, "source": "Betamax"},
                blocking=True,
            )
        assert err.value.translation_key == "unknown_source"
        assert len(tv.writes) == 1
        await hass.config_entries.async_unload(entry.entry_id)


# -- an address announced on mDNS is a hint, kept only once it proves itself ----


async def test_announced_address_is_kept_only_if_it_is_the_tv(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    with _Setup(hass) as setup:
        await setup.run(entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        tv = setup.tvs[0]

        # Connected: an announcement naming another address is not acted on.
        coordinator.async_note_discovery("192.0.2.66", 50000)
        await hass.async_block_till_done()
        assert coordinator._address_hint is None and tv.host == "192.0.2.1"
        assert tv.connects == [("192.0.2.1", 40000)]

        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            # Unreachable, and something that speaks the protocol but cannot
            # produce the encrypted app list answers at the announced address.
            setup.port = None
            tv.impostor_hosts = {"192.0.2.66"}
            coordinator._async_connection_lost("push stream lost")
            coordinator.async_note_discovery("192.0.2.66", 50000)
            await _until(lambda: ("192.0.2.66", 50000) in tv.connects)
            await hass.async_block_till_done()
            assert coordinator.offline
            assert entry.data[CONF_HOST] == "192.0.2.1"
            assert entry.data[CONF_GRPC_PORT] == 40000
            reload.assert_not_called()

            # The TV really did move (a new DHCP lease): it proves itself, the
            # address is stored, and the entry is set up afresh on it.
            coordinator.async_note_discovery("192.0.2.77", 51000)
            await _until(lambda: reload.called)
            assert entry.data[CONF_HOST] == "192.0.2.77"
            assert entry.data[CONF_GRPC_PORT] == 51000
        await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_tries_an_announced_address_first(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hass.data.setdefault(DATA_ADDRESS_HINTS, {})[entry.entry_id] = ("192.0.2.77", 51000)
    with _Setup(hass) as setup:
        await setup.run(entry)
        assert entry.state is ConfigEntryState.LOADED
        assert setup.tvs[0].connects == [("192.0.2.77", 51000)]
        assert entry.data[CONF_HOST] == "192.0.2.77"
        assert entry.data[CONF_GRPC_PORT] == 51000
        assert entry.entry_id not in hass.data[DATA_ADDRESS_HINTS]
        await hass.config_entries.async_unload(entry.entry_id)

    # An impostor at the announced address: the stored one is used instead.
    other = _entry(hass, "dev-2", "192.0.2.5")
    hass.data[DATA_ADDRESS_HINTS][other.entry_id] = ("192.0.2.66", 50000)
    with _Setup(hass) as setup:
        setup.prepare = lambda tv: tv.impostor_hosts.add("192.0.2.66")
        await setup.run(other)
        assert other.state is ConfigEntryState.LOADED
        assert setup.tvs[0].connects == [("192.0.2.66", 50000), ("192.0.2.5", 40000)]
        assert other.data[CONF_HOST] == "192.0.2.5"
        await hass.config_entries.async_unload(other.entry_id)


# -- removal ---------------------------------------------------------------------


async def test_removing_the_tv_clears_what_was_kept_for_it(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with _Setup(hass) as setup:
        await setup.run(entry)
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"agent_outdated_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="agent_outdated",
            translation_placeholders={"name": "x", "installed": "1", "required": "2"},
        )
        assert await schema_store(hass, entry.entry_id).async_load()
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    assert await schema_store(hass, entry.entry_id).async_load() is None
    assert not ir.async_get(hass).async_get_issue(
        DOMAIN, f"agent_outdated_{entry.entry_id}"
    )
