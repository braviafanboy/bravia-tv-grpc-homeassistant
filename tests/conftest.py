"""Test bootstrap.

The wire-decode modules under ``custom_components/bravia_tv_grpc/grpc/`` are
pure (no Home Assistant, no grpcio) but use package-relative imports. Import
them under a synthetic package whose ``__path__`` points at that directory, so
tests can exercise them without pulling in the HA-dependent parent package.
"""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def _no_agent_probe(request):
    """Keep the HA-harness tests hermetic: the coordinator probes the TV's host
    for the BRAVIA agent at start, which would hit the resolver. Tests that
    exercise the agent opt back in with the ``agent_probe`` marker."""
    if request.node.get_closest_marker("agent_probe") is not None:
        yield
        return
    try:
        from unittest.mock import AsyncMock, patch

        with patch(
            "custom_components.bravia_tv_grpc.coordinator.BraviaTvCoordinator.async_start_agent",
            new=AsyncMock(return_value=None),
        ):
            yield
    except ImportError:
        yield
