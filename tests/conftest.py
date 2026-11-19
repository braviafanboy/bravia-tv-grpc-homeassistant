"""Test bootstrap.

The wire-decode modules under ``custom_components/bravia_tv_grpc/grpc/`` are
pure (no Home Assistant, no grpcio) but use package-relative imports. Import
them under a synthetic package whose ``__path__`` points at that directory, so
tests can exercise them without pulling in the HA-dependent parent package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

_GRPC = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "bravia_tv_grpc"
    / "grpc"
)

if "btvgrpc" not in sys.modules:
    _pkg = types.ModuleType("btvgrpc")
    _pkg.__path__ = [str(_GRPC)]
    sys.modules["btvgrpc"] = _pkg


import pytest  # noqa: E402

# Without Home Assistant only the pure tier runs; the fixtures below that
# touch the integration package then step aside.
_HAS_HA = importlib.util.find_spec("homeassistant") is not None

# Loggers on which Home Assistant reports that an integration uses a
# deprecated or incorrect API, and the wordings it uses.
_HA_REPORT_MARKERS = (
    "custom integration 'bravia_tv_grpc'",
    "from bravia_tv_grpc",
    "by custom integration bravia_tv_grpc",
)


@pytest.fixture(autouse=True)
def _no_agent_probe(request):
    """Keep the HA-harness tests hermetic: the coordinator probes the TV's host
    for the HA TV Agent at start, which would hit the resolver. Tests that
    exercise the agent opt back in with the ``agent_probe`` marker."""
    if not _HAS_HA or request.node.get_closest_marker("agent_probe") is not None:
        yield
        return
    from unittest.mock import AsyncMock, patch

    with patch(
        "custom_components.bravia_tv_grpc.coordinator.BraviaTvCoordinator.async_start_agent",
        new=AsyncMock(return_value=None),
    ):
        yield


@pytest.fixture(autouse=True)
def _isolated_config_dir(request, tmp_path):
    """Give every Home Assistant test its own config directory.

    The harness's default one lives inside the installed test package; the
    integration writes picture profiles and stored schemas under the config
    directory, which would otherwise pile up there and leak between tests."""
    if _HAS_HA and "hass" in request.fixturenames:
        request.getfixturevalue("hass").config.config_dir = str(tmp_path)
    yield


def _ha_reports(caplog) -> list[str]:
    return [
        record.getMessage()
        for phase in ("setup", "call", "teardown")
        for record in caplog.get_records(phase)
        if record.name.startswith("homeassistant")
        and any(marker in record.getMessage() for marker in _HA_REPORT_MARKERS)
        and (
            record.name == "homeassistant.helpers.frame"
            or "deprecated" in record.getMessage().lower()
        )
    ]


@pytest.fixture(autouse=True)
def _no_ha_usage_reports(caplog):
    """Fail a test when Home Assistant reports that this integration uses a
    deprecated or incorrect API. HA only logs these, and the message names the
    HA version that will break, so turning them into failures catches a
    deprecation the day it appears.

    Covers the frame helper's "Detected that custom integration
    'bravia_tv_grpc' ..." reports and the deprecation helpers' "... was used
    from bravia_tv_grpc" / "... called by custom integration ..." messages
    (deprecated constants, functions and classes), in every test phase this
    fixture can see."""
    yield
    reports = _ha_reports(caplog)
    if reports:
        pytest.fail("Home Assistant reported:\n" + "\n".join(reports), pytrace=False)
