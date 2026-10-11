# SPDX-License-Identifier: MIT
"""The part of JSON-RPC 2.0 and Server-Sent Events that MCP's Streamable HTTP transport uses.

Parsing is strict, so that the proxy and a server cannot read one message two
ways: duplicate object keys, ``NaN`` and infinities are refused (OpenShell's
MCP inspection refuses duplicate keys too), and the proxy sends its own
serialization of what it decided on, never the bytes it received.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final, cast

from .errors import MessageError

JsonObject = dict[str, object]
RequestId = str | int

MAX_ID_LENGTH: Final = 256
MAX_SAFE_INTEGER: Final = 2**53 - 1
_REQUEST_KEYS: Final = frozenset({"jsonrpc", "id", "method", "params"})
_RESPONSE_KEYS: Final = frozenset({"jsonrpc", "id", "result", "error"})


def _unique_keys(pairs: list[tuple[str, object]]) -> JsonObject:
    fields: JsonObject = {}
    for key, value in pairs:
        if key in fields:
            raise MessageError("a JSON object has a duplicate key")
        fields[key] = value
    return fields


def _no_constant(name: str) -> object:
    raise MessageError(f"{name} is not JSON")


def loads(data: bytes) -> object:
    """Parse UTF-8 JSON; raise MessageError for anything a strict parser would refuse."""
    try:
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_no_constant
        )
    except (ValueError, RecursionError) as exc:
        # ValueError covers JSONDecodeError, UnicodeDecodeError and integers
        # longer than Python converts (sys.get_int_max_str_digits()).
        raise MessageError(f"not valid JSON: {exc}") from exc


def dumps(value: object) -> bytes:
    """Serialize compactly as UTF-8; raise MessageError for values JSON cannot carry."""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        # A lone surrogate (from a "\ud800" escape) has no UTF-8 encoding.
        return text.encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError, RecursionError) as exc:
        raise MessageError(f"cannot serialize JSON: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Request:
    """A JSON-RPC request: it has an id and expects a response."""

    id: RequestId
    method: str
    params: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class Notification:
    """A JSON-RPC notification: no id, no response."""

    method: str
    params: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class Response:
    """A JSON-RPC response: exactly one of ``result`` and ``error``."""

    id: RequestId
    result: JsonObject | None = None
    error: JsonObject | None = None


Message = Request | Notification | Response


def check_id(value: object) -> RequestId:
    """Return a request id if it is a string or a safe integer, as MCP requires."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise MessageError("a JSON-RPC id must be a string or an integer")
    if isinstance(value, str) and not 0 < len(value) <= MAX_ID_LENGTH:
        raise MessageError(f"a JSON-RPC id string must be 1 to {MAX_ID_LENGTH} characters")
    if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
        raise MessageError("a JSON-RPC id integer must be within +-(2^53 - 1)")
    return value


def parse_message(value: object, *, strict: bool = True) -> Message:
    """Read one JSON-RPC message.

    ``strict`` refuses members JSON-RPC does not define, for messages from
    callers; messages from servers may carry others, which the proxy drops.
    """
    if isinstance(value, list):
        raise MessageError("JSON-RPC batches are not supported")
    if not isinstance(value, dict):
        raise MessageError("a JSON-RPC message must be an object")
    fields = cast("JsonObject", value)
    if fields.get("jsonrpc") != "2.0":
        raise MessageError('a JSON-RPC message needs "jsonrpc": "2.0"')
    if "method" in fields:
        if strict and not fields.keys() <= _REQUEST_KEYS:
            raise MessageError("a request has members JSON-RPC does not define")
        method = fields["method"]
        if not isinstance(method, str) or not method:
            raise MessageError("a JSON-RPC method must be a non-empty string")
        params: JsonObject | None = None
        if "params" in fields:
            raw = fields["params"]
            if not isinstance(raw, dict):
                raise MessageError("MCP params must be an object")
            params = cast("JsonObject", raw)
        if "id" in fields:
            return Request(check_id(fields["id"]), method, params)
        return Notification(method, params)
    if strict and not fields.keys() <= _RESPONSE_KEYS:
        raise MessageError("a response has members JSON-RPC does not define")
    if "id" not in fields:
        raise MessageError("a JSON-RPC message needs a method or an id")
    request_id = check_id(fields["id"])
    if ("result" in fields) == ("error" in fields):
        raise MessageError("a JSON-RPC response needs exactly one of result and error")
    if "result" in fields:
        result = fields["result"]
        if not isinstance(result, dict):
            raise MessageError("an MCP result must be an object")
        return Response(request_id, result=cast("JsonObject", result))
    return Response(request_id, error=_error_object(fields["error"]))


def _error_object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise MessageError("a JSON-RPC error must be an object")
    fields = cast("JsonObject", value)
    code = fields.get("code")
    message = fields.get("message")
    if isinstance(code, bool) or not isinstance(code, int) or not isinstance(message, str):
        raise MessageError("a JSON-RPC error needs an integer code and a string message")
    error: JsonObject = {"code": code, "message": message}
    if "data" in fields:
        error["data"] = fields["data"]
    return error


def request(request_id: RequestId, method: str, params: JsonObject | None = None) -> JsonObject:
    """A request object."""
    message: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params: JsonObject | None = None) -> JsonObject:
    """A notification object."""
    message: JsonObject = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def result(request_id: RequestId, value: JsonObject) -> JsonObject:
    """A successful response object."""
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def error(
    request_id: RequestId | None, code: int, message: str, data: JsonObject | None = None
) -> JsonObject:
    """An error response object; its id is null when the request's id is unknown."""
    body: JsonObject = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


@dataclass(frozen=True, slots=True)
class Event:
    """One server-sent event; ``type`` is ``message`` unless the server named another."""

    type: str
    data: str
    id: str | None


_EOL: Final = re.compile(rb"\r\n?|\n")
_BOM: Final = b"\xef\xbb\xbf"


class EventStreamParser:
    """Incremental parser for ``text/event-stream`` (WHATWG HTML, "Server-sent events").

    Lines end with CRLF, LF or CR, and a blank line ends an event. Lines that
    start with a colon are comments. An event left without its blank line at
    the end of the stream is discarded, as the standard says. The caller
    bounds the bytes it feeds.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._data: list[str] = []
        self._type = ""
        self._last_id: str | None = None
        self._started = False

    def feed(self, chunk: bytes) -> list[Event]:
        """Parse more of the stream; return the events it completed."""
        self._buffer += chunk
        if not self._started:
            if len(self._buffer) < len(_BOM) and _BOM.startswith(bytes(self._buffer)):
                return []
            if self._buffer.startswith(_BOM):
                del self._buffer[: len(_BOM)]
            self._started = True
        events: list[Event] = []
        position = 0
        while (match := _EOL.search(self._buffer, position)) is not None:
            if match.group() == b"\r" and match.end() == len(self._buffer):
                break  # the CR may be the first half of a CRLF
            event = self._line(bytes(self._buffer[position : match.start()]))
            position = match.end()
            if event is not None:
                events.append(event)
        del self._buffer[:position]
        return events

    def finish(self) -> list[Event]:
        """End the stream: a CR at its very end ends a line. Return the events completed."""
        if not self._buffer.endswith(b"\r"):
            return []
        event = self._line(bytes(self._buffer[:-1]))
        self._buffer.clear()
        return [] if event is None else [event]

    def _line(self, raw: bytes) -> Event | None:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MessageError("the event stream is not UTF-8") from exc
        if not line:
            if not self._data:
                self._type = ""
                return None
            event = Event(self._type or "message", "\n".join(self._data), self._last_id)
            self._data = []
            self._type = ""
            return event
        if line.startswith(":"):
            return None
        name, colon, value = line.partition(":")
        if colon and value.startswith(" "):
            value = value[1:]
        match name:
            case "data":
                self._data.append(value)
            case "event":
                self._type = value
            case "id" if "\0" not in value:
                self._last_id = value
            case _:
                pass  # "retry" and unknown fields are ignored
        return None
