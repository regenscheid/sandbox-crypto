# SPDX-License-Identifier: MIT
"""What the proxy reads from results, and what it changes in them before callers see them.

Binary content is withheld. The policy asks for binary results as artifact
references (``mcp.proxy.binary_results: artifact``), which are not built yet;
until they are, image and audio blocks and blob resources are replaced by a
text note naming their type and size, rather than passing base64 into model
context (docs/MCP-CONNECTORS.md, "Not built yet").
"""

from __future__ import annotations

import re
from typing import Final, cast

from .errors import MessageError
from .jsonrpc import JsonObject

_MIME_RE: Final = re.compile(r"[A-Za-z0-9.+-]{1,64}/[A-Za-z0-9.+-]{1,64}")


def tool_names(result: JsonObject) -> tuple[list[str], str | None]:
    """The tool names and the next cursor of a ``tools/list`` result.

    Raises MessageError when the result does not have the shape MCP gives it.
    """
    tools = result.get("tools")
    cursor = result.get("nextCursor")
    if not isinstance(tools, list) or (cursor is not None and not isinstance(cursor, str)):
        raise MessageError("a tools/list result needs a tools list and a string nextCursor")
    if cursor == "":
        cursor = None  # an empty cursor leads nowhere; some servers send it on the last page
    names: list[str] = []
    for tool in cast("list[object]", tools):
        name = cast("JsonObject", tool).get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name:
            raise MessageError("every tool in a tools/list result needs a name")
        names.append(name)
    return names, cursor


def withhold_binary(method: str, result: JsonObject) -> tuple[JsonObject, int]:
    """Replace binary content in a result with text notes; return the result and how many."""
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
    withheld = 0
    for item in cast("list[object]", items):
        replacement = _replacement(method, item)
        if replacement is not None:
            withheld += 1
        kept.append(item if replacement is None else replacement)
    return {**result, key: kept}, withheld


def _replacement(method: str, item: object) -> JsonObject | None:
    """What replaces one item of a result's content, or None to keep it."""
    if method == "tools/call":
        return _note_block(item)
    if method == "resources/read":
        return _note_resource(item)
    if not isinstance(item, dict):  # a prompts/get message: its content is one block
        return None
    message = cast("JsonObject", item)
    block = _note_block(message.get("content"))
    return None if block is None else {**message, "content": block}


def _note(mime: object, data: object) -> str:
    kind = mime if isinstance(mime, str) and _MIME_RE.fullmatch(mime) else "binary data"
    size = len(data) if isinstance(data, str) else 0
    return (
        f"[{kind} withheld by the MCP proxy: {size} bytes of base64; binary results are not "
        "passed inline]"
    )


def _note_block(value: object) -> JsonObject | None:
    """A text block in place of an image, audio or blob-resource content block, or None."""
    if not isinstance(value, dict):
        return None
    block = cast("JsonObject", value)
    kind = block.get("type")
    if kind in ("image", "audio"):
        return {"type": "text", "text": _note(block.get("mimeType"), block.get("data"))}
    resource = block.get("resource")
    if kind == "resource" and isinstance(resource, dict):
        entry = cast("JsonObject", resource)
        if "blob" in entry:
            return {"type": "text", "text": _note(entry.get("mimeType"), entry.get("blob"))}
    return None


def _note_resource(value: object) -> JsonObject | None:
    """A text resource entry in place of a blob entry of ``resources/read``, or None."""
    if not isinstance(value, dict) or "blob" not in value:
        return None
    entry = cast("JsonObject", value)
    replacement: JsonObject = {
        "mimeType": "text/plain",
        "text": _note(entry.get("mimeType"), entry.get("blob")),
    }
    if "uri" in entry:
        replacement["uri"] = entry["uri"]
    return replacement
