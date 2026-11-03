"""Pure tests for the BRAVIA TV agent client module (no Home Assistant)."""

from __future__ import annotations

from btvgrpc import agent


def test_parse_sse_basic_events():
    text = (
        "event: state\n"
        'data: {"a": 1}\n'
        "\n"
        ": comment\n"
        "event: heartbeat\n"
        "data: {}\n"
        "\n"
        "data: line1\n"
        "data: line2\n"
        "\n"
    )
    events = list(agent.parse_sse(text.splitlines(keepends=True)))
    assert events == [
        ("state", '{"a": 1}'),
        ("heartbeat", "{}"),
        ("message", "line1\nline2"),
    ]


def test_parse_sse_trailing_event_without_blank_line():
    events = list(agent.parse_sse(["event: state\n", "data: {}\n"]))
    assert events == [("state", "{}")]


def test_parse_sse_crlf_and_no_space_after_colon():
    events = list(agent.parse_sse(["event:state\r\n", 'data:{"x":2}\r\n', "\r\n"]))
    assert events == [("state", '{"x":2}')]


def test_pair_deep_link_encodes_name():
    uri = agent.pair_deep_link("ab" * 32, "Home Assistant")
    assert uri == "braviaagent://pair?token=" + "ab" * 32 + "&name=Home%20Assistant"


def test_new_token_is_64_hex():
    tok = agent.new_token()
    assert len(tok) == 64
    assert int(tok, 16) >= 0


def test_state_blocks_fills_missing_with_none():
    blocks = agent.state_blocks({"environment": {"illuminance_lx": 3}, "junk": 1})
    assert blocks["environment"] == {"illuminance_lx": 3}
    assert blocks["media"] is None
    assert "junk" not in blocks
    assert set(blocks) == set(agent.BLOCKS)
