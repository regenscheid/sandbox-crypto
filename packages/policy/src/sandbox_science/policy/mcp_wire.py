# SPDX-License-Identifier: MIT
"""The parts of MCP's Streamable HTTP wire format that both MCP enforcement points read.

The MCP proxy and the policy middleware (D98) decide a message only as the server will
read it: strict JSON (duplicate keys, ``NaN`` and infinities are refused, as OpenShell's
MCP inspection refuses duplicate keys), only the parameters MCP defines for a request
(a server whose JSON decoder ignores the case of keys, as Go's does, would read ``NAME``
as the tool name the policy never saw), and server-sent events for answers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from .errors import ClassificationError

JsonObject = dict[str, object]

MCP_REQUEST_PARAMS: Final[Mapping[str, frozenset[str]]] = {
    "ping": frozenset(),
    "tools/list": frozenset({"cursor"}),
    "tools/call": frozenset({"name", "arguments"}),
    "resources/list": frozenset({"cursor"}),
    "resources/templates/list": frozenset({"cursor"}),
    "resources/read": frozenset({"uri"}),
    "resources/subscribe": frozenset({"uri"}),
    "resources/unsubscribe": frozenset({"uri"}),
    "prompts/list": frozenset({"cursor"}),
    "prompts/get": frozenset({"name", "arguments"}),
    "completion/complete": frozenset({"ref", "argument", "context"}),
    "logging/setLevel": frozenset({"level"}),
}
"""The parameters MCP (2025-03-26 to 2025-11-25) defines for each request an enforcement
point passes on, besides ``_meta``."""

PLACEHOLDER_MARK: Final = b"openshell:resolve:"
"""What every OpenShell placeholder starts with."""


class McpWireError(ClassificationError):
    """A message, its parameters or an event stream is malformed or unsupported."""


def _unique_keys(pairs: list[tuple[str, object]]) -> JsonObject:
    fields: JsonObject = {}
    for key, value in pairs:
        if key in fields:
            raise McpWireError("a JSON object has a duplicate key")
        fields[key] = value
    return fields


def _no_constant(name: str) -> object:
    raise McpWireError(f"{name} is not JSON")


def _finite(text: str) -> float:
    value = float(text)
    if math.isinf(value):
        raise McpWireError("a number is too large to represent")
    return value


def loads(data: bytes) -> object:
    """Parse UTF-8 JSON; raise McpWireError for anything a strict parser would refuse."""
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_keys,
            parse_constant=_no_constant,
            parse_float=_finite,
        )
    except McpWireError:
        raise
    except (ValueError, RecursionError) as exc:
        # ValueError covers JSONDecodeError, UnicodeDecodeError and integers
        # longer than Python converts (sys.get_int_max_str_digits()).
        raise McpWireError(f"not valid JSON: {exc}") from exc


def dumps(value: object) -> bytes:
    """Serialize compactly as UTF-8; raise McpWireError for values JSON cannot carry."""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        # A lone surrogate (from a "\ud800" escape) has no UTF-8 encoding.
        return text.encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError, RecursionError) as exc:
        raise McpWireError(f"cannot serialize JSON: {exc}") from exc


def forwarded_params(method: str, params: Mapping[str, object] | None) -> JsonObject | None:
    """A request's parameters as they are decided: those MCP defines for the method.

    ``_meta`` is left out: it carries progress tokens and other caller metadata, not the
    request's effect. A task-augmented request is refused, since it would start work whose
    result only the task methods return, and the policy denies those. Any other member is
    refused (see ``MCP_REQUEST_PARAMS``).
    """
    known = MCP_REQUEST_PARAMS.get(method)
    if known is None:
        raise McpWireError(f"{method!r} is not a request MCP defines parameters for")
    if params is None:
        return None
    if "task" in params:
        raise McpWireError("task-augmented requests are not supported")
    if not params.keys() - {"_meta"} <= known:
        raise McpWireError(f"{method} has parameters MCP does not define")
    arguments = params.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        raise McpWireError(f"{method} arguments must be an object")
    return {key: params[key] for key in sorted(known) if key in params}


def params_digest(encoded: bytes) -> str:
    """The digest a permission request's key holds for a request's encoded parameters."""
    return hashlib.sha256(encoded).hexdigest()


def contains_placeholder(body: bytes) -> bool:
    """Whether a body carries something that reads as an OpenShell placeholder, in any case."""
    return PLACEHOLDER_MARK in body.lower()


def tool_names(result: Mapping[str, object]) -> tuple[list[str], str | None]:
    """The tool names and the next cursor of a ``tools/list`` result.

    Raises McpWireError when the result does not have the shape MCP gives it.
    """
    tools = result.get("tools")
    cursor = result.get("nextCursor")
    if not isinstance(tools, list) or (cursor is not None and not isinstance(cursor, str)):
        raise McpWireError("a tools/list result needs a tools list and a string nextCursor")
    if cursor == "":
        cursor = None  # an empty cursor leads nowhere; some servers send it on the last page
    names: list[str] = []
    for tool in cast("list[object]", tools):
        name = cast("JsonObject", tool).get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name:
            raise McpWireError("every tool in a tools/list result needs a name")
        names.append(name)
    return names, cursor


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
    bounds the bytes it feeds; each byte is scanned once, however small the
    chunks it arrives in.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._scanned = 0  # the buffer holds no line end before here
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
        while (match := _EOL.search(self._buffer, max(position, self._scanned))) is not None:
            if match.group() == b"\r" and match.end() == len(self._buffer):
                break  # the CR may be the first half of a CRLF
            event = self._line(bytes(self._buffer[position : match.start()]))
            position = match.end()
            if event is not None:
                events.append(event)
        del self._buffer[:position]
        # Only a CR at the very end can still start a line end.
        self._scanned = max(len(self._buffer) - 1, 0)
        return events

    def finish(self) -> list[Event]:
        """End the stream: a CR at its very end ends a line. Return the events completed."""
        if not self._buffer.endswith(b"\r"):
            return []
        event = self._line(bytes(self._buffer[:-1]))
        self._buffer.clear()
        self._scanned = 0
        return [] if event is None else [event]

    def _line(self, raw: bytes) -> Event | None:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise McpWireError("the event stream is not UTF-8") from exc
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
