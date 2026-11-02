"""Release-notes page reduction for the firmware update entity."""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from custom_components.bravia_tv_grpc.update import _html_to_text  # noqa: E402


def test_html_to_text_keeps_structure_and_drops_chrome() -> None:
    page = (
        "<html><head><title>x</title><style>p{}</style></head><body>"
        "<nav>menu</nav><h1>Firmware 114.603</h1>"
        "<p>Improvements &amp; fixes:</p><ul><li>Faster boot</li>"
        "<li>Stability</li></ul><script>alert(1)</script></body></html>"
    )
    assert _html_to_text(page) == (
        "Firmware 114.603\nImprovements & fixes:\n- Faster boot\n- Stability"
    )


def test_html_to_text_empty() -> None:
    assert _html_to_text("") == ""
