# SPDX-License-Identifier: MIT
"""The part of JSON-RPC 2.0 and Server-Sent Events that MCP's Streamable HTTP transport uses.

Parsing is strict, so that the proxy and a server cannot read one message two
ways: duplicate object keys, ``NaN`` and infinities are refused (OpenShell's
MCP inspection refuses duplicate keys too), and the proxy sends its own
serialization of what it decided on, never the bytes it received. The strict
JSON and the event-stream parser are the policy library's (``mcp_wire``), which
the policy middleware reads MCP with too (D98).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

from sandbox_science.policy import mcp_wire
from sandbox_science.policy.mcp_wire import Event as Event
from sandbox_science.policy.mcp_wire import McpWireError

from .errors import MessageError

JsonObject = mcp_wire.JsonObject
RequestId = str | int

MAX_ID_LENGTH: Final = 256
MAX_SAFE_INTEGER: Final = 2**53 - 1
_REQUEST_KEYS: Final = frozenset({"jsonrpc", "id", "method", "params"})
_RESPONSE_KEYS: Final = frozenset({"jsonrpc", "id", "result", "error"})


def loads(data: bytes) -> object:
    """Parse UTF-8 JSON; raise MessageError for anything a strict parser would refuse."""
    try:
        return mcp_wire.loads(data)
    except McpWireError as exc:
        raise MessageError(str(exc)) from exc


def dumps(value: object) -> bytes:
    """Serialize compactly as UTF-8; raise MessageError for values JSON cannot carry."""
    try:
        return mcp_wire.dumps(value)
    except McpWireError as exc:
        raise MessageError(str(exc)) from exc


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


class EventStreamParser(mcp_wire.EventStreamParser):
    """The library's ``text/event-stream`` parser, raising MessageError for a stream
    that is not UTF-8."""

    def feed(self, chunk: bytes) -> list[Event]:
        """Parse more of the stream; return the events it completed."""
        try:
            return super().feed(chunk)
        except McpWireError as exc:
            raise MessageError(str(exc)) from exc

    def finish(self) -> list[Event]:
        """End the stream; return the events completed."""
        try:
            return super().finish()
        except McpWireError as exc:
            raise MessageError(str(exc)) from exc
