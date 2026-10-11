# SPDX-License-Identifier: MIT
"""Strict JSON-RPC parsing and the event-stream parser."""

import pytest

from sandbox_science.mcp_proxy import jsonrpc
from sandbox_science.mcp_proxy.errors import MessageError
from sandbox_science.mcp_proxy.jsonrpc import (
    Event,
    EventStreamParser,
    Notification,
    Request,
    Response,
)

# --- JSON --------------------------------------------------------------------------------------


def test_loads_reads_json() -> None:
    assert jsonrpc.loads(b'{"a":[1,2.5,"x",null,true]}') == {"a": [1, 2.5, "x", None, True]}


@pytest.mark.parametrize(
    "data",
    [
        b'{"a":1,"a":2}',
        b'{"outer":{"b":1,"b":1}}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b"\xef\xbb\xbf{}",
        b'{"a":"\xff"}',
        b"[" * 100_000 + b"]" * 100_000,
        b"",
        b"{",
        b"[" + b"9" * 5000 + b"]",
    ],
)
def test_loads_refuses_what_a_strict_parser_would(data: bytes) -> None:
    with pytest.raises(MessageError):
        jsonrpc.loads(data)


def test_dumps_is_compact_utf8() -> None:
    assert jsonrpc.dumps({"a": "é", "b": [1]}) == '{"a":"é","b":[1]}'.encode()


def test_dumps_refuses_lone_surrogates() -> None:
    value = jsonrpc.loads(b'{"a":"\\ud800"}')
    with pytest.raises(MessageError):
        jsonrpc.dumps(value)


# --- Messages ----------------------------------------------------------------------------------


def test_parse_message_tells_requests_notifications_and_responses_apart() -> None:
    assert jsonrpc.parse_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"cursor": "c"}}
    ) == Request(1, "tools/list", {"cursor": "c"})
    assert jsonrpc.parse_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) == (
        Notification("notifications/initialized")
    )
    assert jsonrpc.parse_message({"jsonrpc": "2.0", "id": "a", "result": {}}) == Response(
        "a", result={}
    )
    error = {"code": -32601, "message": "no", "data": [1]}
    assert jsonrpc.parse_message({"jsonrpc": "2.0", "id": 2, "error": error}) == Response(
        2, error=error
    )


@pytest.mark.parametrize(
    "message",
    [
        [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
        "ping",
        {"id": 1, "method": "ping"},
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": ""},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": None},
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        {"jsonrpc": "2.0", "id": "", "method": "ping"},
        {"jsonrpc": "2.0", "id": "x" * 257, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2**53, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "extra": 1},
        {"jsonrpc": "2.0", "result": {}},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": 1, "message": "x"}},
        {"jsonrpc": "2.0", "id": 1, "result": [1]},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": "1", "message": "x"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": True, "message": "x"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": 1}},
        {"jsonrpc": "2.0", "id": 1, "error": "bad"},
    ],
)
def test_parse_message_refuses_malformed_messages(message: object) -> None:
    with pytest.raises(MessageError):
        jsonrpc.parse_message(message)


def test_lenient_parsing_drops_members_jsonrpc_does_not_define() -> None:
    message = {"jsonrpc": "2.0", "id": 1, "result": {}, "_meta": {}}
    with pytest.raises(MessageError):
        jsonrpc.parse_message(message)
    assert jsonrpc.parse_message(message, strict=False) == Response(1, result={})
    error = {"code": 1, "message": "x", "extra": True}
    parsed = jsonrpc.parse_message({"jsonrpc": "2.0", "id": 1, "error": error}, strict=False)
    assert parsed == Response(1, error={"code": 1, "message": "x"})


def test_builders() -> None:
    assert jsonrpc.request(1, "ping") == {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert jsonrpc.notification("n", {"a": 1}) == {
        "jsonrpc": "2.0",
        "method": "n",
        "params": {"a": 1},
    }
    assert jsonrpc.result("x", {}) == {"jsonrpc": "2.0", "id": "x", "result": {}}
    assert jsonrpc.error(None, -1, "m", {"reason": "r"}) == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -1, "message": "m", "data": {"reason": "r"}},
    }


# --- Server-sent events ------------------------------------------------------------------------


def feed_all(parser: EventStreamParser, *chunks: bytes) -> list[Event]:
    events: list[Event] = []
    for chunk in chunks:
        events += parser.feed(chunk)
    return events + parser.finish()


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_events_end_at_a_blank_line_whatever_the_line_ends(newline: bytes) -> None:
    stream = newline.join([b"data: one", b"", b"event: other", b"data: two", b"data: lines", b""])
    events = feed_all(EventStreamParser(), stream + newline)
    assert events == [Event("message", "one", None), Event("other", "two\nlines", None)]


def test_events_survive_any_split_into_chunks() -> None:
    stream = b'\xef\xbb\xbf: comment\r\nid: 7\r\ndata:{"a":1}\r\n\r\ndata: x\r\rdata: y\n\n'
    expected = [
        Event("message", '{"a":1}', "7"),
        Event("message", "x", "7"),
        Event("message", "y", "7"),
    ]
    for size in range(1, len(stream) + 1):
        parser = EventStreamParser()
        chunks = [stream[start : start + size] for start in range(0, len(stream), size)]
        assert feed_all(parser, *chunks) == expected, size


def test_events_without_data_are_not_dispatched() -> None:
    parser = EventStreamParser()
    assert parser.feed(b"id: 1\n\nevent: x\n\n: keep-alive\n\nretry: 10\n\n") == []
    # The event type does not carry over to the next event.
    assert parser.feed(b"data: a\n\n") == [Event("message", "a", "1")]


def test_fields_are_read_as_the_standard_says() -> None:
    parser = EventStreamParser()
    events = parser.feed(b"data\ndata:  two spaces\nid: a\x00b\nunknown: x\n\n")
    assert events == [Event("message", "\n two spaces", None)]


def test_an_event_without_its_blank_line_is_not_dispatched() -> None:
    parser = EventStreamParser()
    assert parser.feed(b"data: unfinished\n") == []
    assert parser.finish() == []


def test_a_cr_at_the_end_of_the_stream_ends_its_line() -> None:
    parser = EventStreamParser()
    assert parser.feed(b"data: last\r\r") == []  # the second CR may start a CRLF
    assert parser.finish() == [Event("message", "last", None)]


def test_streams_must_be_utf8() -> None:
    with pytest.raises(MessageError):
        EventStreamParser().feed(b"data: \xff\n\n")
