"""Audio Signal sensor tests (require HA test harness).

The eARC/soundbar path leaves signal_info.audio empty while an app decodes PCM;
the sensor infers PCM from active playback without mislabelling bitstream audio.
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


async def _sensor(hass: HomeAssistant) -> BraviaTvAudioSignalSensor:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id="dev", data={"host": "x"})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.capabilities = {}
    client.get_states.return_value = {}
    client.read_application_list.return_value = []
    coordinator = BraviaTvCoordinator(hass, entry, client)
    await coordinator.async_start()
    return BraviaTvAudioSignalSensor(coordinator)


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
