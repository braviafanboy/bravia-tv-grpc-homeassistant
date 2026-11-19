"""Picture profiles: back up and restore a full set of picture settings."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402

from custom_components.bravia_tv_grpc import picture_profiles as pp  # noqa: E402
from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    AGENT_ONLINE,
    BraviaTvCoordinator,
    agent_path,
)

COORD = "custom_components.bravia_tv_grpc.coordinator"


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _no_wait_for_the_agents_reread():
    """A capture waits for the state event of the agent's fresh read; the
    fakes here send none unless a test makes them."""
    with patch(f"{COORD}._PICTURE_FRESH_WAIT", 0):
        yield


def test_capture_picks_writable_available_non_mode():
    targets = {
        "pictureMode": {
            "value": "professional",
            "available": True,
            "candidates": [{"value": "cinema"}],
        },
        "contrast": {"value": "90", "available": True, "candidates": [{"min": 0}]},
        "redOffset": {
            "value": "[1, 2]",
            "available": True,
            "candidates": [{"min": -50}],
        },
        "hdmiSignalFormat": {"value": "standard", "available": True},  # read-only
        "hdrMode": {"value": "on", "available": False, "candidates": [{"value": "on"}]},
    }
    mode, settings = pp.capture(targets)
    assert mode == "professional"
    assert settings == {"contrast": "90", "redOffset": "[1, 2]"}


def test_diff():
    assert pp.diff({"a": "1", "b": "2"}, {"a": "1", "b": "3", "c": "4"}) == {
        "b": {"a": "2", "b": "3"},
        "c": {"a": None, "b": "4"},
    }


def _coordinator(hass: HomeAssistant) -> BraviaTvCoordinator:
    from uuid import uuid4

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    # Unique title per coordinator: the profile folder is derived from it, so
    # this isolates each test's on-disk store in the shared test config dir.
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev",
        title=f"tv {uuid4().hex}",
        data={"host": "tv", "agent_token": "t"},
    )
    entry.add_to_hass(hass)
    client = MagicMock()
    client.capabilities = {}
    c = BraviaTvCoordinator(hass, entry, client)
    # async_start (which builds the folder store) isn't run in these unit tests,
    # so point the store at a unique temp folder for isolation.
    c._picture_profiles = pp.PictureProfileStore(
        hass, hass.config.path("bravia_picture_profiles", uuid4().hex)
    )
    c._state["power"] = True
    c._state[AGENT_ONLINE] = True
    c._state[agent_path("rest")] = {"available": True, "write_authorized": True}
    c._state[agent_path("picture")] = {
        "targets": {
            "pictureMode": {
                "value": "professional",
                "available": True,
                "candidates": [{"value": "professional"}, {"value": "cinema"}],
            },
            "contrast": {"value": "90", "available": True, "candidates": [{"min": 0}]},
            "color": {"value": "50", "available": True, "candidates": [{"min": 0}]},
            # Excluded from profiles (global light-sensor setting) but present so
            # the restore's auto_luminance_on flag reads it.
            "autoLuminanceLevel": {
                "value": "off",
                "available": True,
                "candidates": [{"value": "off"}, {"value": "on"}],
            },
        }
    }
    c.agent = MagicMock()
    c.agent.async_set_picture = AsyncMock()
    c.agent.async_picture_refresh = AsyncMock()
    c.agent.async_picture_batch = AsyncMock(
        return_value={"applied": 2, "failed": 0, "results": []}
    )
    return c


async def test_save_then_restore(hass: HomeAssistant):
    c = _coordinator(hass)
    saved = await c.async_save_picture_profile("cal")
    assert saved["picture_mode"] == "professional"
    assert saved["count"] == 2  # contrast + color (not pictureMode/autoLuminanceLevel)
    res = await c.async_restore_picture_profile("cal")
    c.agent.async_set_picture.assert_awaited_once_with("pictureMode", "professional")
    c.agent.async_picture_batch.assert_awaited_once()
    assert res["applied"] == 2
    assert res["auto_luminance_on"] is False
    # No ambient learner on the agent -> nothing to reset.
    assert res["ambient_reset"] is False


async def test_save_captures_what_the_tv_has_now(hass: HomeAssistant):
    """What Home Assistant holds is the agent's last read, up to a poll
    interval old. A save asks for a fresh read first, so a backup taken right
    after adjusting the picture with the remote stores the new values."""
    c = _coordinator(hass)

    async def reread():
        # The agent's re-read finds the change and reports it.
        targets = c._state[agent_path("picture")]["targets"]
        fresh = {**targets, "contrast": {**targets["contrast"], "value": "77"}}
        c._apply_agent_document(
            {"picture": {"targets": fresh}, "rest": c._state[agent_path("rest")]}
        )

    c.agent.async_picture_refresh.side_effect = reread
    with patch(f"{COORD}._PICTURE_FRESH_WAIT", 5):
        await c.async_save_picture_profile("after tweak")
    c.agent.async_picture_refresh.assert_awaited_once()
    assert (await c._picture_profiles.get("after tweak"))["settings"][
        "contrast"
    ] == "77"

    # If the agent cannot be asked, the last read is used: still a save.
    from custom_components.bravia_tv_grpc.grpc import agent as agentmod

    c.agent.async_picture_refresh.side_effect = agentmod.AgentError("down")
    assert (await c.async_save_picture_profile("anyway"))["count"] == 2


async def test_restore_backs_up_what_it_overwrites(hass: HomeAssistant):
    """A restore overwrites the mode's settings for good, so it first keeps
    them as "Before last restore"; restoring that one swaps the two (undo)."""
    c = _coordinator(hass)
    targets = c._state[agent_path("picture")]["targets"]
    await c.async_save_picture_profile("cal")  # contrast 90
    targets["contrast"]["value"] = "55"  # the picture as it is before the restore
    c.agent.reset_mock()

    res = await c.async_restore_picture_profile("cal")
    assert res["backup"] == pp.BACKUP_NAME
    backup = await c._picture_profiles.get(pp.BACKUP_NAME)
    assert backup["settings"]["contrast"] == "55"
    assert backup["picture_mode"] == "professional" and backup["hdr_class"] == "sdr"
    assert pp.BACKUP_NAME in c.picture_profile_names
    # It was read after the mode switch and before the writes.
    order = [name for name, *_ in c.agent.method_calls]
    assert order.index("async_set_picture") < order.index("async_picture_refresh")
    assert order.index("async_picture_refresh") < order.index("async_picture_batch")

    # Undo: restore the backup. What it overwrites (the restored "cal" values)
    # becomes the new backup, so the same press redoes.
    targets["contrast"]["value"] = "90"
    undo = await c.async_restore_picture_profile(pp.BACKUP_NAME)
    written = {
        item["target"]: item["value"]
        for item in c.agent.async_picture_batch.await_args.args[0]
    }
    assert written["contrast"] == "55"
    assert undo["backup"] == pp.BACKUP_NAME
    assert (await c._picture_profiles.get(pp.BACKUP_NAME))["settings"][
        "contrast"
    ] == "90"


async def test_restore_without_readable_settings_still_restores(hass: HomeAssistant):
    c = _coordinator(hass)
    await c.async_save_picture_profile("cal")
    c._state[agent_path("picture")] = {"targets": {}}
    res = await c.async_restore_picture_profile("cal")
    assert res["backup"] is None and res["applied"] == 2


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (("rest_unauthorized", 403), "rest_key_rejected"),
        (("rest_unavailable", 503), "rest_unavailable"),
        (("invalid_value", 400), "picture_write_refused"),
        (None, "agent_offline"),
    ],
)
async def test_restore_reports_what_actually_failed(hass: HomeAssistant, error, key):
    """A refusal from the agent used to be reported as "the agent is not
    connected", whatever it was."""
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.bravia_tv_grpc.grpc import agent as agentmod

    c = _coordinator(hass)
    await c.async_save_picture_profile("cal")
    c.agent.async_picture_batch.side_effect = (
        agentmod.AgentRequestError(*error) if error else agentmod.AgentError("down")
    )
    with pytest.raises(HomeAssistantError) as err:
        await c.async_restore_picture_profile("cal")
    assert err.value.translation_key == key
    if error:
        assert err.value.translation_placeholders["code"] == error[0]


async def test_restore_refuses_a_mode_the_tv_does_not_offer(hass: HomeAssistant):
    """Before anything is written, not as a failed write afterwards."""
    c = _coordinator(hass)
    await c._picture_profiles.put(
        "dv", {"picture_mode": "dolbyVisionDark", "settings": {"contrast": "90"}}
    )
    with pytest.raises(ServiceValidationError) as err:
        await c.async_restore_picture_profile("dv")
    assert err.value.translation_key == "picture_mode_unavailable"
    assert err.value.translation_placeholders["mode"] == "dolbyVisionDark"
    c.agent.async_set_picture.assert_not_awaited()
    c.agent.async_picture_batch.assert_not_awaited()


async def test_restore_resets_ambient_learner_when_enabled(hass: HomeAssistant):
    """Option A: a restore changes the brightness baseline, so if the ambient
    learner is on, its stale per-mode curve is reset to relearn."""
    c = _coordinator(hass)
    c._state[agent_path("ambient")] = {"enabled": True}
    c.agent.async_ambient = AsyncMock(return_value={"enabled": True})
    await c.async_save_picture_profile("cal")
    res = await c.async_restore_picture_profile("cal")
    # The restored mode is passed explicitly so the agent resets the right
    # curve without depending on its (possibly lagging) cached current mode.
    c.agent.async_ambient.assert_awaited_once_with("reset", {"mode": "professional"})
    assert res["ambient_reset"] is True


async def test_restore_skips_ambient_reset_when_learner_off(hass: HomeAssistant):
    c = _coordinator(hass)
    c._state[agent_path("ambient")] = {"enabled": False}
    c.agent.async_ambient = AsyncMock(return_value={"enabled": False})
    await c.async_save_picture_profile("cal")
    res = await c.async_restore_picture_profile("cal")
    c.agent.async_ambient.assert_not_awaited()
    assert res["ambient_reset"] is False


async def test_restore_ambient_reset_is_best_effort(hass: HomeAssistant):
    """A failed learner reset never fails the restore itself."""
    from custom_components.bravia_tv_grpc.grpc import agent as agentmod

    c = _coordinator(hass)
    c._state[agent_path("ambient")] = {"enabled": True}
    c.agent.async_ambient = AsyncMock(
        side_effect=agentmod.AgentRequestError("boom", 500)
    )
    await c.async_save_picture_profile("cal")
    res = await c.async_restore_picture_profile("cal")
    assert res["applied"] == 2  # the picture restore still succeeded
    assert res["ambient_reset"] is False


async def test_restore_unknown_raises(hass: HomeAssistant):
    c = _coordinator(hass)
    with pytest.raises(ServiceValidationError) as err:
        await c.async_restore_picture_profile("nope")
    assert err.value.translation_key == "picture_profile_unknown"


async def test_list_and_delete(hass: HomeAssistant):
    c = _coordinator(hass)
    await c.async_save_picture_profile("a")
    assert "a" in (await c.async_list_picture_profiles())["profiles"]
    await c.async_delete_picture_profile("a")
    assert "a" not in (await c.async_list_picture_profiles())["profiles"]


async def test_compare_to_current(hass: HomeAssistant):
    c = _coordinator(hass)
    await c.async_save_picture_profile("a")
    c._state[agent_path("picture")]["targets"]["contrast"]["value"] = "80"
    cmp = await c.async_compare_picture_profile("a")
    assert cmp["differences"].get("contrast") == {"a": "90", "b": "80"}


async def test_select_and_restore_button(hass: HomeAssistant):
    from custom_components.bravia_tv_grpc.button import BraviaTvRestoreProfileButton
    from custom_components.bravia_tv_grpc.select import BraviaTvPictureProfileSelect

    c = _coordinator(hass)
    sel = BraviaTvPictureProfileSelect(c)
    assert sel.options == [] and sel.current_option is None
    await c.async_save_picture_profile("a")
    assert sel.options == ["a"]
    assert sel.current_option == "a"  # saving a profile chooses it
    await c.async_save_picture_profile("b")
    assert set(sel.options) == {"a", "b"}
    await sel.async_select_option("b")
    assert sel.current_option == "b"
    await BraviaTvRestoreProfileButton(c).async_press()
    c.agent.async_picture_batch.assert_awaited()


async def test_restore_button_none_selected(hass: HomeAssistant):
    from homeassistant.exceptions import ServiceValidationError

    c = _coordinator(hass)
    await c._picture_profiles.put("a", {"settings": {"contrast": "90"}})
    await c._refresh_profile_names()
    # Profiles exist, but none was chosen: Restore must not pick one itself.
    assert c.picture_profile_names == ["a"]
    assert c.selected_picture_profile is None
    with pytest.raises(ServiceValidationError) as err:
        await c.async_restore_selected_picture_profile()
    assert err.value.translation_key == "picture_profile_none_selected"
    c.agent.async_picture_batch.assert_not_awaited()


async def test_choice_survives_a_reload_but_is_never_a_guess(hass: HomeAssistant):
    """After a restart nothing is chosen (it used to be the first profile,
    which the Restore button would then write). The user's own choice is kept
    across a reload of the entry."""
    from fake_tv import CREDS, FakeTv
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.bravia_tv_grpc.grpc import credentials as cred

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev",
        title="Living room",
        data={"host": "tv", "grpc_keys": cred.credentials_to_json(CREDS)},
    )
    entry.add_to_hass(hass)

    async def start() -> BraviaTvCoordinator:
        coordinator = BraviaTvCoordinator(
            hass, entry, FakeTv("tv", 1, "dev", CREDS["hmac_key"])
        )
        with patch(f"{COORD}.BraviaTvCoordinator._async_load_cloud", new=AsyncMock()):
            await coordinator.async_start()
        return coordinator

    first = await start()
    for name in ("a", "b"):
        await first._picture_profiles.put(name, {"settings": {"contrast": "90"}})
    await first._refresh_profile_names()
    assert first.picture_profile_names == ["a", "b"]
    assert first.selected_picture_profile is None
    await first.async_select_picture_profile("b")
    await first.async_shutdown()

    second = await start()  # the entry was reloaded
    assert second.selected_picture_profile == "b"
    # A chosen profile whose file is gone is not replaced by another.
    await second._picture_profiles.delete("b")
    await second._refresh_profile_names()
    assert second.selected_picture_profile is None
    await second.async_shutdown()

    third = await start()
    assert third.selected_picture_profile is None
    await third.async_shutdown()


async def test_name_text_and_save_button(hass: HomeAssistant):
    from custom_components.bravia_tv_grpc.button import BraviaTvSaveProfileButton
    from custom_components.bravia_tv_grpc.text import BraviaTvProfileNameText

    c = _coordinator(hass)
    name = BraviaTvProfileNameText(c)
    assert name.native_value == ""
    await name.async_set_value("  Night  ")  # trimmed
    assert c.profile_name_input == "Night"
    assert name.native_value == "Night"
    await BraviaTvSaveProfileButton(c).async_press()
    assert "Night" in c.picture_profile_names
    assert c.selected_picture_profile == "Night"
    assert c.profile_name_input == ""  # box cleared after a save


async def test_save_button_blank_name_raises(hass: HomeAssistant):
    c = _coordinator(hass)
    with pytest.raises(ServiceValidationError) as err:
        await c.async_save_picture_profile_from_input()
    assert err.value.translation_key == "picture_profile_name_required"


async def test_delete_selected_button(hass: HomeAssistant):
    from custom_components.bravia_tv_grpc.button import BraviaTvDeleteProfileButton

    c = _coordinator(hass)
    await c.async_save_picture_profile("a")
    await c.async_save_picture_profile("b")  # b now selected
    await BraviaTvDeleteProfileButton(c).async_press()
    assert "b" not in c.picture_profile_names
    assert "a" in c.picture_profile_names
    # Nothing takes the deleted profile's place: a second press must not
    # delete "a" as well.
    assert c.selected_picture_profile is None
    with pytest.raises(ServiceValidationError):
        await BraviaTvDeleteProfileButton(c).async_press()
    assert "a" in c.picture_profile_names


async def test_delete_selected_none_raises(hass: HomeAssistant):
    c = _coordinator(hass)
    c._selected_profile = None
    with pytest.raises(ServiceValidationError) as err:
        await c.async_delete_selected_picture_profile()
    assert err.value.translation_key == "picture_profile_none_selected"


async def test_restore_refuses_on_range_mismatch(hass: HomeAssistant):
    """A profile saved in one dynamic range refuses to restore while another is
    playing, unless forced."""
    from custom_components.bravia_tv_grpc.coordinator import _SIGNAL_VIDEO

    c = _coordinator(hass)
    # saved while SDR (no signal -> sdr)
    saved = await c.async_save_picture_profile("sdr-cal")
    assert saved["hdr_class"] == "sdr"
    # now HDR10 is playing
    c._state[_SIGNAL_VIDEO] = '[{"hdr_type": "hdr10"}]'
    with pytest.raises(ServiceValidationError) as err:
        await c.async_restore_picture_profile("sdr-cal")
    assert err.value.translation_key == "picture_profile_range_mismatch"
    assert err.value.translation_placeholders == {
        "name": "sdr-cal",
        "saved": "sdr",
        "current": "hdr10",
    }
    c.agent.async_picture_batch.assert_not_awaited()
    # force overrides
    res = await c.async_restore_picture_profile("sdr-cal", force=True)
    assert res["applied"] == 2
    c.agent.async_picture_batch.assert_awaited_once()


async def test_restore_matching_range_proceeds(hass: HomeAssistant):
    c = _coordinator(hass)
    await c.async_save_picture_profile("cal")  # sdr
    res = await c.async_restore_picture_profile("cal")  # still sdr
    assert res["hdr_class"] == "sdr"
    c.agent.async_picture_batch.assert_awaited()


async def test_restore_legacy_profile_without_range_proceeds(hass: HomeAssistant):
    """Profiles saved before the range stamp have no hdr_class and must still
    restore (no mismatch to enforce)."""
    c = _coordinator(hass)
    await c._picture_profiles.put(
        "legacy", {"picture_mode": "professional", "settings": {"contrast": "90"}}
    )
    res = await c.async_restore_picture_profile("legacy")
    assert "applied" in res


async def test_select_exposes_selected_profile_details(hass: HomeAssistant):
    from custom_components.bravia_tv_grpc.select import BraviaTvPictureProfileSelect

    c = _coordinator(hass)
    await c.async_save_picture_profile("a")  # professional, sdr, 2 settings
    sel = BraviaTvPictureProfileSelect(c)
    attrs = sel.extra_state_attributes
    assert attrs == {
        "picture_mode": "Professional",
        "dynamic_range": "SDR",
        "model": None,  # fixture entry has no model set
        "settings": 2,
        "saved": attrs["saved"],  # an ISO timestamp
    }
    assert attrs["saved"]
    # switching selection switches the details
    c._state[agent_path("picture")]["targets"]["contrast"]["value"] = "80"
    await c.async_save_picture_profile("b")
    await sel.async_select_option("a")
    assert sel.extra_state_attributes["settings"] == 2


async def test_select_details_none_when_empty(hass: HomeAssistant):
    from custom_components.bravia_tv_grpc.select import BraviaTvPictureProfileSelect

    c = _coordinator(hass)
    assert BraviaTvPictureProfileSelect(c).extra_state_attributes is None


async def test_select_details_follow_what_the_select_shows(hass: HomeAssistant):
    """A chosen SDR profile is hidden while HDR plays: the select then shows
    nothing chosen, and its attributes must not describe the hidden one."""
    from custom_components.bravia_tv_grpc.coordinator import _SIGNAL_VIDEO
    from custom_components.bravia_tv_grpc.select import (
        BraviaTvPictureProfileSelect,
        _range_label,
    )

    c = _coordinator(hass)
    await c.async_save_picture_profile("sdr cal")
    sel = BraviaTvPictureProfileSelect(c)
    assert sel.extra_state_attributes["dynamic_range"] == "SDR"
    c._state[_SIGNAL_VIDEO] = '[{"hdr_type": "dolby"}]'
    assert sel.current_option is None
    assert sel.extra_state_attributes is None
    # The TV reports Dolby Vision as the bare "dolby".
    await c.async_save_picture_profile("dv cal")
    assert sel.extra_state_attributes["dynamic_range"] == "Dolby Vision"
    assert _range_label("hdr10plus") == "HDR10+"


async def test_ambient_optimization_excluded_from_profile(hass: HomeAssistant):
    """sensorAutoAdjustment (Ambient Optimization) is a global, non-per-mode
    setting: never captured, and never written back from a legacy profile."""
    from custom_components.bravia_tv_grpc import picture_profiles as pp

    c = _coordinator(hass)
    c._state[agent_path("picture")]["targets"]["sensorAutoAdjustment"] = {
        "value": "on",
        "available": True,
        "candidates": [{"value": "off"}, {"value": "on"}],
    }
    for t in ("colorSpace", "hdrMode", "contentType", "pixelShift"):
        c._state[agent_path("picture")]["targets"][t] = {
            "value": "x",
            "available": True,
            "candidates": [{"value": "x"}, {"value": "y"}],
        }
    _, settings = pp.capture(c._state[agent_path("picture")]["targets"])
    for t in pp.PROFILE_EXCLUDED:
        assert t not in settings

    # a legacy profile that already stored it must not push it on restore
    await c._picture_profiles.put(
        "legacy",
        {"settings": {"contrast": "90", "sensorAutoAdjustment": "on"}},
    )
    await c.async_restore_picture_profile("legacy")
    batch = c.agent.async_picture_batch.await_args.args[0]
    assert all(s["target"] != "sensorAutoAdjustment" for s in batch)


async def test_store_folder_roundtrip_and_external_files(hass: HomeAssistant, tmp_path):
    from custom_components.bravia_tv_grpc.picture_profiles import PictureProfileStore

    d = tmp_path / "profiles"
    store = PictureProfileStore(hass, str(d))
    await store.async_ensure_dir()
    assert d.is_dir()

    await store.put(
        "pro",
        {
            "settings": {"contrast": "90"},
            "picture_mode": "professional",
            "hdr_class": "sdr",
            "created": "x",
        },
    )
    assert (d / "pro.yaml").exists()  # filename IS the profile name
    assert (await store.get("pro"))["settings"] == {"contrast": "90"}
    assert await store.names() == ["pro"]

    # a file dropped in externally is picked up on the next scan
    (d / "night.yaml").write_text("settings:\n  color: '40'\npicture_mode: cinema\n")
    assert "night" in await store.names()

    # the signature changes when the folder changes (drives the re-scan)
    sig = await store.async_signature()
    (d / "extra.yaml").write_text("settings:\n  a: '1'\n")
    assert await store.async_signature() != sig

    assert await store.delete("pro") is True
    assert "pro" not in await store.names()
    assert await store.delete("pro") is False  # already gone


async def test_store_skips_bad_and_hostile_files(hass: HomeAssistant, tmp_path):
    from custom_components.bravia_tv_grpc.picture_profiles import (
        MAX_FILE_BYTES,
        PictureProfileStore,
    )

    d = tmp_path / "p"
    store = PictureProfileStore(hass, str(d))
    await store.async_ensure_dir()
    (d / "ok.yaml").write_text("settings:\n  contrast: '90'\n")
    (d / "badyaml.yaml").write_text("not: [a, b\n")  # broken YAML
    (d / "nosettings.yaml").write_text("picture_mode: cinema\n")  # no settings
    (d / "alias.yaml").write_text("settings: &a\n  x: '1'\ndup: *a\n")  # alias vector
    # Valid in every other respect, so only the size cap can reject it.
    (d / "huge.yaml").write_text(
        "settings:\n  contrast: '90'\n" + "#" * (MAX_FILE_BYTES + 1) + "\n"
    )
    (d / "manykeys.yaml").write_text(
        "settings:\n" + "".join(f"  k{i}: '1'\n" for i in range(201))
    )
    (d / "longvalue.yaml").write_text(f"settings:\n  a: '{'x' * 513}'\n")
    names = await store.names()
    assert names == ["ok"]  # only the valid file survives


def test_profile_name_validation():
    from custom_components.bravia_tv_grpc.picture_profiles import is_valid_profile_name

    assert is_valid_profile_name("Calibrated (Pro, SDR)")
    assert is_valid_profile_name("Kalibriert für Wohnzimmer 🎬")
    for bad in ("", "a/b", "a\\b", ".", "..", "x" * 129, "bad\nname", 5, None):
        assert not is_valid_profile_name(bad)
    # Within the character limit, but too many bytes for a file name.
    assert not is_valid_profile_name("🎬" * 100)


async def test_profile_slug_persist_and_collision(hass: HomeAssistant):
    """The folder slug comes from the friendly name, is disambiguated against
    other TVs, and is persisted (stable thereafter)."""
    from custom_components.bravia_tv_grpc.const import CONF_PROFILE_SLUG

    c1 = _coordinator(hass)
    c1._state["system_setting.friendly_name"] = "BRAVIA 8 II"
    assert c1._resolve_profile_slug() == "bravia_8_ii"
    assert c1.config_entry.data[CONF_PROFILE_SLUG] == "bravia_8_ii"  # persisted
    assert c1._resolve_profile_slug() == "bravia_8_ii"  # idempotent

    # a second, identically-named TV is disambiguated
    c2 = _coordinator(hass)
    c2._state["system_setting.friendly_name"] = "BRAVIA 8 II"
    assert c2._resolve_profile_slug() == "bravia_8_ii-2"


async def test_model_stamped_and_mismatch(hass: HomeAssistant):
    from custom_components.bravia_tv_grpc.const import CONF_MODEL

    c = _coordinator(hass)
    hass.config_entries.async_update_entry(
        c.config_entry, data={**c.config_entry.data, CONF_MODEL: "BRAVIA 8 II"}
    )
    saved = await c.async_save_picture_profile("a")
    assert saved["model"] == "BRAVIA 8 II"
    assert (await c._picture_profiles.get("a"))["model"] == "BRAVIA 8 II"

    res = await c.async_restore_picture_profile("a")  # same model
    assert res["model"] == "BRAVIA 8 II"
    assert res["model_mismatch"] is False

    # same profile, but the TV is now a different model -> soft mismatch flag
    hass.config_entries.async_update_entry(
        c.config_entry, data={**c.config_entry.data, CONF_MODEL: "BRAVIA 9"}
    )
    assert (await c.async_restore_picture_profile("a"))["model_mismatch"] is True


async def test_subset_profile_restores_only_its_keys(hass: HomeAssistant):
    """A hand-made file with just a couple of settings (and no mode/range)
    restores only those settings and leaves the mode alone."""
    c = _coordinator(hass)
    await c._picture_profiles.put(
        "twopoint", {"settings": {"redGain": "2", "blueGain": "-1"}}
    )
    res = await c.async_restore_picture_profile("twopoint")
    c.agent.async_set_picture.assert_not_awaited()  # no picture_mode -> mode untouched
    batch = c.agent.async_picture_batch.await_args.args[0]
    assert {s["target"] for s in batch} == {"redGain", "blueGain"}
    assert res["picture_mode"] is None


async def test_restore_ambient_reset_without_mode_uses_empty_body(hass: HomeAssistant):
    """A mode-less (subset) profile doesn't switch the mode, so the learner
    reset sends an empty body and the agent resets whatever mode is current."""
    c = _coordinator(hass)
    c._state[agent_path("ambient")] = {"enabled": True}
    c.agent.async_ambient = AsyncMock(return_value={"enabled": True})
    await c._picture_profiles.put("twopoint", {"settings": {"redGain": "2"}})
    res = await c.async_restore_picture_profile("twopoint")
    c.agent.async_ambient.assert_awaited_once_with("reset", {})
    assert res["ambient_reset"] is True


async def test_no_signal_counts_as_sdr(hass: HomeAssistant):
    """No video signal (a menu / home screen) is treated as SDR, so an SDR
    profile can be saved and restored there with nothing playing."""
    c = _coordinator(hass)  # fixture sets no video signal
    assert c._current_hdr_class() == "sdr"
    saved = await c.async_save_picture_profile("from-menu")
    assert saved["hdr_class"] == "sdr"
    # and it restores in that same no-signal state (guard matches sdr==sdr)
    assert "applied" in await c.async_restore_picture_profile("from-menu")


async def test_restore_falls_back_to_per_setting_on_old_agent(hass: HomeAssistant):
    """If the agent lacks /v1/picture/batch (404), restore falls back to
    per-setting writes so it still works against the released 0.6.0 agent."""
    from custom_components.bravia_tv_grpc.grpc import agent as agentmod

    c = _coordinator(hass)
    await c.async_save_picture_profile("cal")
    c.agent.async_picture_batch = AsyncMock(
        side_effect=agentmod.AgentRequestError("not_found", 404)
    )
    c.agent.async_set_picture = AsyncMock()
    res = await c.async_restore_picture_profile("cal")
    # per-setting writes used instead of the batch: the mode, contrast, color
    assert [call.args[0] for call in c.agent.async_set_picture.await_args_list] == [
        "pictureMode",
        "color",
        "contrast",
    ]
    assert res["applied"] == 2 and res["failed"] == 0


# ---- restore target ("Restore to picture mode") and the range filter --------


def _offer_modes(c, *modes: str, current: str = "customForPro1") -> None:
    """The TV offers these picture modes now; ``current`` is active."""
    targets = c._state[agent_path("picture")]["targets"]
    targets["pictureMode"] = {
        "value": current,
        "available": True,
        "candidates": [{"value": m} for m in modes],
    }
    c._state[pp.GRPC_MODE_PATH] = current


async def test_choosing_a_profile_sets_the_restore_target(hass: HomeAssistant):
    c = _coordinator(hass)
    _offer_modes(c, "professional", "customForPro1", "customForPro2")
    await c.async_save_picture_profile("pro1")  # saved in customForPro1
    assert c.restore_target == "customForPro1"
    # A deliberate override...
    await c.async_set_restore_target("customForPro2")
    assert c.restore_target == "customForPro2"
    # ...is reset by choosing a profile again.
    await c.async_select_picture_profile("pro1")
    assert c.restore_target == "customForPro1"


async def test_restore_target_falls_back_to_saved_profile(hass: HomeAssistant):
    c = _coordinator(hass)
    _offer_modes(c, "customForPro1", "customForPro2")
    await c.async_save_picture_profile("pro1")
    # The TV no longer offers the profile's mode (e.g. another source).
    _offer_modes(c, "standard", "cinema", current="standard")
    await c.async_select_picture_profile("pro1")
    assert c.restore_target is None  # "From saved profile"
    # ...which the TV cannot take right now: refused up front.
    with pytest.raises(ServiceValidationError) as err:
        await c.async_restore_selected_picture_profile()
    assert err.value.translation_key == "picture_mode_unavailable"
    # A target that stops being offered also reads as "From saved profile".
    _offer_modes(c, "customForPro1", "customForPro2")
    await c.async_set_restore_target("customForPro2")
    _offer_modes(c, "customForPro1")
    assert c.restore_target is None


async def test_restore_selected_into_another_mode(hass: HomeAssistant):
    c = _coordinator(hass)
    c._state[agent_path("ambient")] = {"enabled": True}
    c.agent.async_ambient = AsyncMock(return_value={"enabled": True})
    _offer_modes(c, "customForPro1", "customForPro2")
    await c.async_save_picture_profile("pro1")
    await c.async_set_restore_target("customForPro2")
    res = await c.async_restore_selected_picture_profile()
    c.agent.async_set_picture.assert_awaited_once_with("pictureMode", "customForPro2")
    assert res["picture_mode"] == "customForPro2"
    assert res["saved_picture_mode"] == "customForPro1"
    # The learner resets the curve of the mode actually written.
    c.agent.async_ambient.assert_awaited_once_with("reset", {"mode": "customForPro2"})


async def test_restore_action_picture_mode_by_value_or_label(hass: HomeAssistant):
    from custom_components.bravia_tv_grpc.util import friendly_enum_label

    c = _coordinator(hass)
    _offer_modes(c, "customForPro1", "customForPro2")
    await c.async_save_picture_profile("pro1")
    await c.async_restore_picture_profile("pro1", picture_mode="customForPro2")
    c.agent.async_set_picture.assert_awaited_with("pictureMode", "customForPro2")
    label = friendly_enum_label("customForPro2")
    await c.async_restore_picture_profile("pro1", picture_mode=label.upper())
    c.agent.async_set_picture.assert_awaited_with("pictureMode", "customForPro2")
    with pytest.raises(ServiceValidationError) as err:
        await c.async_restore_picture_profile("pro1", picture_mode="vivid")
    assert err.value.translation_key == "picture_mode_unavailable"


async def test_profile_list_follows_dynamic_range(hass: HomeAssistant):
    from custom_components.bravia_tv_grpc.coordinator import _SIGNAL_VIDEO

    c = _coordinator(hass)
    await c.async_save_picture_profile("sdr cal")
    c._state[_SIGNAL_VIDEO] = '[{"hdr_type": "hdr10"}]'
    await c.async_save_picture_profile("hdr cal")
    assert c.picture_profile_names == ["hdr cal"]
    assert c.selected_picture_profile == "hdr cal"
    # Back to SDR: only SDR profiles; the HDR selection is hidden, not lost.
    c._state[_SIGNAL_VIDEO] = "[]"
    assert c.picture_profile_names == ["sdr cal"]
    assert c.selected_picture_profile is None
    with pytest.raises(ServiceValidationError):
        await c.async_restore_selected_picture_profile()
    c._state[_SIGNAL_VIDEO] = '[{"hdr_type": "hdr10"}]'
    assert c.selected_picture_profile == "hdr cal"


async def test_restore_target_select_entity(hass: HomeAssistant):
    from custom_components.bravia_tv_grpc.select import (
        FROM_SAVED_PROFILE,
        BraviaTvRestoreTargetSelect,
    )
    from custom_components.bravia_tv_grpc.util import friendly_enum_label

    c = _coordinator(hass)
    _offer_modes(c, "customForPro1", "customForPro2")
    await c.async_save_picture_profile("pro1")
    entity = BraviaTvRestoreTargetSelect(c)
    pro1, pro2 = (
        friendly_enum_label("customForPro1"),
        friendly_enum_label("customForPro2"),
    )
    assert entity.options == [FROM_SAVED_PROFILE, pro1, pro2]
    assert entity.current_option == pro1
    await entity.async_select_option(pro2)
    assert c.restore_target == "customForPro2"
    await entity.async_select_option(FROM_SAVED_PROFILE)
    assert c.restore_target is None
    assert entity.current_option == FROM_SAVED_PROFILE
