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


async def test_async_events_assembles_line_delivered_frames():
    """aiohttp yields the body line by line; events must be assembled across lines."""

    lines = [
        b"event: state\n",
        b'data: {"environment": {"illuminance_lx": 5}}\n',
        b"\n",
        b"event: heartbeat\n",
        b"data: {}\n",
        b"\n",
        b"event: state\n",
        b'data: {"environment": {"illuminance_lx": 6}}\n',
        b"\n",
    ]

    class Content:
        def __init__(self):
            self._it = iter(lines)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

    class Resp:
        status = 200

        def __init__(self):
            self.content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def get(self, *a, **k):
            return Resp()

    client = agent.AgentClient(Session(), "tv", token="t")

    async def collect():
        out = []
        try:
            async for ev, payload in client.async_events():
                out.append((ev, payload["environment"]["illuminance_lx"]))
        except agent.AgentError as err:
            assert "stream ended" in str(err)
        return out

    assert await collect() == [("state", 5), ("state", 6)]
