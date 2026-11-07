"""Picture profiles: back up and restore a full set of picture settings."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
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
                "candidates": [{"value": "cinema"}],
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
    await c.async_save_picture_profile("a")
    sel = BraviaTvPictureProfileSelect(c)
    assert sel.options == ["a"]
    assert sel.current_option == "a"
    await c.async_save_picture_profile("b")
    assert set(sel.options) == {"a", "b"}
    await sel.async_select_option("b")
    assert sel.current_option == "b"
    await BraviaTvRestoreProfileButton(c).async_press()
    c.agent.async_picture_batch.assert_awaited()


async def test_restore_button_none_selected(hass: HomeAssistant):
    from homeassistant.exceptions import ServiceValidationError

    c = _coordinator(hass)
    c._selected_profile = None
    with pytest.raises(ServiceValidationError) as err:
        await c.async_restore_selected_picture_profile()
    assert err.value.translation_key == "picture_profile_none_selected"


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
    # force overrides
    res = await c.async_restore_picture_profile("sdr-cal", force=True)
    assert res["applied"] >= 0


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
    big = "x" * (MAX_FILE_BYTES + 1)
    (d / "huge.yaml").write_text(f"settings:\n  a: '{big}'\n")
    names = await store.names()
    assert names == ["ok"]  # only the valid file survives


def test_profile_name_validation():
    from custom_components.bravia_tv_grpc.picture_profiles import is_valid_profile_name

    assert is_valid_profile_name("Calibrated (Pro, SDR)")
    for bad in ("", "a/b", "a\\b", ".", "..", "x" * 129, "bad\nname", 5, None):
        assert not is_valid_profile_name(bad)


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
    # per-setting writes used instead of the batch
    assert c.agent.async_set_picture.await_count >= 2  # contrast + color at least
    assert res["applied"] >= 2 and res["failed"] == 0
