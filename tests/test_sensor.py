"""Sensor / media_player tests (require HA test harness).

Covers the Audio Signal PCM inference and the power-gating of the App sensor
and media_player source (the TV keeps the last app in networked standby).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.bravia_tv_grpc.const import DOMAIN  # noqa: E402
from custom_components.bravia_tv_grpc.coordinator import (  # noqa: E402
    BraviaTvCoordinator,
)
from custom_components.bravia_tv_grpc.sensor import (  # noqa: E402
    BraviaTvAudioSignalSensor,
)

AUDIO = "playback_control.signal_info.audio"
VIDEO = "playback_control.signal_info.video"
PLAYING_VIDEO = '[{"package":"tv.twitch.android.app","width":1664,"height":936}]'


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


async def _coordinator(hass: HomeAssistant) -> BraviaTvCoordinator:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id="dev", data={"host": "x"})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.capabilities = {}
    client.get_states.return_value = {}
    client.read_application_list.return_value = []
    coordinator = BraviaTvCoordinator(hass, entry, client)
    await coordinator.async_start()
    return coordinator


async def _sensor(hass: HomeAssistant) -> BraviaTvAudioSignalSensor:
    return BraviaTvAudioSignalSensor(await _coordinator(hass))


async def test_app_sensor_blanks_when_powered_off(hass: HomeAssistant) -> None:
    """In standby the TV still reports the last app; the sensor must show nothing
    once power is off, rather than a phantom app."""
    from custom_components.bravia_tv_grpc.sensor import BraviaTvAppSensor

    coordinator = await _coordinator(hass)
    sensor = BraviaTvAppSensor(coordinator)
    coordinator._apply_delta("system_setting.application", "tv.twitch.android.app")
    coordinator._apply_delta("system_setting.tvapp.input", '{"type":"none"}')

    coordinator._apply_delta("power", True)
    assert sensor.native_value is not None  # reports the app while on

    coordinator._apply_delta("power", False)  # app field is still set (standby)
    assert sensor.native_value is None
    await coordinator.async_shutdown()


async def test_media_player_source_blanks_when_powered_off(hass: HomeAssistant) -> None:
    from custom_components.bravia_tv_grpc.media_player import BraviaTvMediaPlayer

    coordinator = await _coordinator(hass)
    mp = BraviaTvMediaPlayer(coordinator)
    coordinator._apply_delta("system_setting.application", "tv.twitch.android.app")
    coordinator._apply_delta("system_setting.tvapp.input", '{"type":"none"}')

    coordinator._apply_delta("power", True)
    assert mp.source is not None

    coordinator._apply_delta("power", False)
    assert mp.source is None
    await coordinator.async_shutdown()


async def test_operation_speaker_captures_opspk_setup(hass: HomeAssistant) -> None:
    """The Operation Speaker sensor surfaces opspk.setup raw (the combine value)
    plus the generic-command-in-use flag."""
    from custom_components.bravia_tv_grpc.sensor import BraviaTvOperationSpeakerSensor

    coordinator = await _coordinator(hass)
    sensor = BraviaTvOperationSpeakerSensor(coordinator)

    coordinator._apply_delta("opspk.setup", "none")
    coordinator._apply_delta("opspk.generic_command_in_use", False)
    assert sensor.native_value == "none"
    assert sensor.extra_state_attributes == {"command_in_use": False}

    # A (hypothetical) combined value must pass through verbatim for replay.
    coordinator._apply_delta("opspk.setup", '{"id":"audiosystem@hdmi"}')
    coordinator._apply_delta("opspk.generic_command_in_use", True)
    assert sensor.native_value == '{"id":"audiosystem@hdmi"}'
    assert sensor.extra_state_attributes["command_in_use"] is True
    await coordinator.async_shutdown()


async def test_empty_audio_while_playing_reports_pcm(hass: HomeAssistant) -> None:
    """Twitch + Audio System output: audio empty, video playing -> PCM."""
    sensor = await _sensor(hass)
    sensor.coordinator._apply_delta(AUDIO, "[]")
    sensor.coordinator._apply_delta(VIDEO, PLAYING_VIDEO)
    assert sensor.native_value == "PCM"
    await sensor.coordinator.async_shutdown()


async def test_empty_audio_idle_reports_none(hass: HomeAssistant) -> None:
    """Nothing playing (no video signal) -> genuinely unknown, not PCM."""
    sensor = await _sensor(hass)
    sensor.coordinator._apply_delta(AUDIO, "[]")
    sensor.coordinator._apply_delta(VIDEO, "[]")
    assert sensor.native_value is None
    await sensor.coordinator.async_shutdown()


async def test_app_decoded_tv_speakers_reports_pcm(hass: HomeAssistant) -> None:
    """TV-speaker output reports a package-only audio entry -> PCM (unchanged)."""
    sensor = await _sensor(hass)
    sensor.coordinator._apply_delta(AUDIO, '[{"package":"tv.twitch.android.app"}]')
    assert sensor.native_value == "PCM"
    await sensor.coordinator.async_shutdown()


async def test_bitstream_codec_not_affected(hass: HomeAssistant) -> None:
    """A bitstream track carries its codec regardless of output -> not PCM."""
    sensor = await _sensor(hass)
    sensor.coordinator._apply_delta(
        AUDIO, '[{"codec":"dolby_atmos_digital_plus","channels":"5.1"}]'
    )
    # Even with a video signal present, the codec wins over the PCM inference.
    sensor.coordinator._apply_delta(VIDEO, PLAYING_VIDEO)
    assert sensor.native_value == "5.1 Surround Dolby Atmos"
    await sensor.coordinator.async_shutdown()
