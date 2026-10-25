# SPDX-License-Identifier: MIT
"""What both MCP enforcement points change in a server's answers before a caller sees them.

The MCP proxy and the policy middleware (D98) share these rules, so that an agent sees the
same through either:

- **Listings lose denied tools** (lab 39, B5). Each tool of a ``tools/list`` page is judged
  as a call by name alone; a tool the policy denies is dropped, so the agent is never
  offered what it can never call. Every kept tool carries the enforcement point's word on
  its class (``EFFECT_META``) and whether a call of it asks a person first
  (``ESCALATES_META``), over anything the server put there.
- **Binary content is withheld.** Image and audio blocks and blob resources never pass
  inline: each is replaced by a text note naming its type and size, which a caller that can
  keep the bytes (the harness, as an artifact) words itself.
- **Significant arguments** of a held call are summarized for its card from the server's
  classification (D97), never the arguments themselves.

``rewrite_answer`` applies a change to one answer as it travels, JSON or an event stream.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast

from .approvals.summary import call_summary
from .ceiling import PoolCeiling
from .classification import EFFECT_META, ServerClassification, listed_effect, tool_entry
from .decision import Action
from .effects import EffectClass
from .evaluate import DEFAULT_FLOOR, evaluate_mcp_message
from .floor import HardFloor
from .loader import LoadedPolicy
from .mcp_wire import EventStreamParser, McpWireError, dumps, loads
from .model import McpServer
from .operations import Origin

JsonObject = dict[str, object]

ESCALATES_META: Final = "sandbox-science/escalates"
"""The ``_meta`` key under which an enforcement point says that calling a listed tool asks
a person first (the policy escalates it by name): ``true``, or absent."""
BINARY_METHODS: Final = frozenset({"tools/call", "prompts/get", "resources/read"})
"""The methods whose results can carry binary content."""
_MIME_RE: Final = re.compile(r"[A-Za-z0-9.+-]{1,64}/[A-Za-z0-9.+-]{1,64}")


def tool_summary(
    server: McpServer,
    tool: str | None,
    params: object,
    classification: ServerClassification | None = None,
) -> dict[str, str]:
    """A tool call's significant arguments, from its server's classification in force (D97):
    the project's accepted revision (``classification``), else the catalog's."""
    if tool is None or not isinstance(params, dict):
        return {}
    entry = tool_entry(server, tool.removeprefix(server.tool_prefix or ""), classification)
    arguments = cast("JsonObject", params).get("arguments")
    if entry is None or not entry.significant or not isinstance(arguments, dict):
        return {}
    return call_summary(entry.significant, cast("JsonObject", arguments))


def callable_tools(
    server: str,
    result: JsonObject,
    loaded: LoadedPolicy,
    *,
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
    origin: Origin = "orchestrator",
    hints: Mapping[str, EffectClass] | None = None,
    classification: ServerClassification | None = None,
) -> tuple[JsonObject, int]:
    """A ``tools/list`` page without the tools the policy denies a caller of ``origin``, and
    how many it dropped. Each tool is judged as a call by name alone, with ``hints`` for
    tools the policy does not classify and the project's accepted ``classification`` of the
    server, if any. Escalated tools stay, marked: they ask a person. A page without a tools
    list is returned as it is."""
    tools = result.get("tools")
    if not isinstance(tools, list):
        return result, 0
    entry = loaded.policy.mcp.servers[server]
    kept: list[object] = []
    denied = 0
    for tool in cast("list[object]", tools):
        name = _name(tool)
        if isinstance(name, str):
            message: JsonObject = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "tools/call",
                "params": {"name": name, "arguments": {}},
            }
            decision = evaluate_mcp_message(
                server,
                message,
                loaded,
                origin=origin,
                ceiling=ceiling,
                floor=floor,
                hints=hints,
                classification=classification,
            )
            if decision.action is Action.DENY:
                denied += 1
                continue
            tool = _marked(
                tool,
                listed_effect(entry, name, decision, classification),
                escalates=decision.action is Action.ESCALATE,
            )
        kept.append(tool)
    return {**result, "tools": kept}, denied


def _name(tool: object) -> object:
    """A listed tool's name, as the server gave it."""
    return cast("JsonObject", tool).get("name") if isinstance(tool, dict) else None


def _marked(tool: object, effect: str, *, escalates: bool) -> object:
    """A listed tool with the enforcement point's marks in ``_meta``, over any server's."""
    if not isinstance(tool, dict):
        return tool
    fields = cast("JsonObject", tool)
    meta = fields.get("_meta")
    own = dict(cast("JsonObject", meta)) if isinstance(meta, dict) else {}
    own[EFFECT_META] = effect
    own.pop(ESCALATES_META, None)
    if escalates:
        own[ESCALATES_META] = True
    return {**fields, "_meta": own}


@dataclass(frozen=True, slots=True)
class Withheld:
    """One binary item of a result: its place among the binary items, kind, media type (as
    the server named it, if well formed) and its base64 text, as the server sent it."""

    index: int
    kind: Literal["image", "audio", "blob"]
    mime: str | None
    data: str

    @property
    def size(self) -> int:
        """The length of the base64 text."""
        return len(self.data)


Note = Callable[[Withheld], str]


def withheld_note(by: str) -> Note:
    """The note an enforcement point that keeps nothing puts in place of a binary item."""

    def note(item: Withheld) -> str:
        kind = item.mime or "binary data"
        return (
            f"[{kind} withheld by {by}: {item.size} bytes of base64; binary results are not "
            "passed inline]"
        )

    return note


def binary_contents(method: str, result: JsonObject) -> list[Withheld]:
    """The binary items of a result, in order; empty for a method that carries none."""
    found: list[Withheld] = []

    def take(item: Withheld) -> str:
        found.append(item)
        return ""

    withhold_binary(method, result, take)
    return found


def withhold_binary(method: str, result: JsonObject, note: Note) -> tuple[JsonObject, int]:
    """Replace each binary item of a result with the text ``note`` gives it; return the new
    result and how many it replaced. The server's result is not changed in place."""
    match method:
        case "tools/call":
            key = "content"
        case "prompts/get":
            key = "messages"
        case "resources/read":
            key = "contents"
        case _:
            return result, 0
    items = result.get(key)
    if not isinstance(items, list):
        return result, 0
    kept: list[object] = []
    count = 0

    def replaced(kind: Literal["image", "audio", "blob"], mime: object, data: object) -> str:
        nonlocal count
        item = Withheld(
            count,
            kind,
            mime if isinstance(mime, str) and _MIME_RE.fullmatch(mime) else None,
            data if isinstance(data, str) else "",
        )
        count += 1
        return note(item)

    for item in cast("list[object]", items):
        replacement = _replacement(method, item, replaced)
        kept.append(item if replacement is None else replacement)
    return {**result, key: kept}, count


_Replace = Callable[[Literal["image", "audio", "blob"], object, object], str]


def _replacement(method: str, item: object, replaced: _Replace) -> JsonObject | None:
    """What replaces one item of a result's content, or None to keep it."""
    if method == "tools/call":
        return _block(item, replaced)
    if method == "resources/read":
        return _resource(item, replaced)
    if not isinstance(item, dict):  # a prompts/get message: its content is one block
        return None
    message = cast("JsonObject", item)
    block = _block(message.get("content"), replaced)
    return None if block is None else {**message, "content": block}


def _block(value: object, replaced: _Replace) -> JsonObject | None:
    """A text block in place of an image, audio or blob-resource content block, or None."""
    if not isinstance(value, dict):
        return None
    block = cast("JsonObject", value)
    kind = block.get("type")
    if kind == "image" or kind == "audio":
        found: Literal["image", "audio"] = "image" if kind == "image" else "audio"
        return {"type": "text", "text": replaced(found, block.get("mimeType"), block.get("data"))}
    resource = block.get("resource")
    if kind == "resource" and isinstance(resource, dict):
        entry = cast("JsonObject", resource)
        if "blob" in entry:
            text = replaced("blob", entry.get("mimeType"), entry.get("blob"))
            return {"type": "text", "text": text}
    return None


def _resource(value: object, replaced: _Replace) -> JsonObject | None:
    """A text resource entry in place of a blob entry of ``resources/read``, or None."""
    if not isinstance(value, dict) or "blob" not in value:
        return None
    entry = cast("JsonObject", value)
    replacement: JsonObject = {
        "mimeType": "text/plain",
        "text": replaced("blob", entry.get("mimeType"), entry.get("blob")),
    }
    if "uri" in entry:
        replacement["uri"] = entry["uri"]
    return replacement


Rewrite = Callable[[JsonObject], JsonObject]
"""A change to one JSON-RPC result."""


def rewrite_answer(body: bytes, *, stream: bool, request_id: object, rewrite: Rewrite) -> bytes:
    """``body``, a server's whole answer to request ``request_id`` as JSON or as an event
    stream, with ``rewrite`` applied to the result of that request's response. Other
    messages of a stream (notifications, the server's requests) pass as the parser read
    them. Raises McpWireError for an answer that cannot be read strictly, which a caller
    refuses rather than passes unread."""
    if not stream:
        return dumps(_rewritten(loads(body), request_id, rewrite))
    parser = EventStreamParser()
    events = [*parser.feed(body), *parser.finish()]
    out = bytearray()
    for event in events:
        data = event.data
        if event.type == "message" and data:
            data = dumps(_rewritten(loads(data.encode("utf-8")), request_id, rewrite)).decode()
        if event.id is not None:
            out += b"id: " + event.id.encode("utf-8") + b"\n"
        if event.type != "message":
            out += b"event: " + event.type.encode("utf-8") + b"\n"
        for line in data.split("\n"):
            out += b"data: " + line.encode("utf-8") + b"\n"
        out += b"\n"
    return bytes(out)


def _rewritten(value: object, request_id: object, rewrite: Rewrite) -> object:
    """One message, with the response to ``request_id`` rewritten. A batch (2025-03-26
    servers may send one in an event) is rewritten item by item."""
    if isinstance(value, list):
        return [_rewritten(item, request_id, rewrite) for item in cast("list[object]", value)]
    if not isinstance(value, dict):
        raise McpWireError("an answer holds JSON-RPC messages")
    message = cast("JsonObject", value)
    result = message.get("result")
    if (
        "method" in message
        or not _same_id(message.get("id"), request_id)
        or not isinstance(result, dict)
    ):
        return message
    return {**message, "result": rewrite(cast("JsonObject", result))}


def _same_id(found: object, wanted: object) -> bool:
    """Whether a response's id is the request's: equal and of the same JSON type."""
    return type(found) is type(wanted) and found == wanted
