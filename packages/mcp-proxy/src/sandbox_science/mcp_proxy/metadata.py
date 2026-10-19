# SPDX-License-Identifier: MIT
"""Bounded passive metadata from actual proxy traffic, never an upstream probe."""

from __future__ import annotations

import json
import math
import re
from collections import OrderedDict
from datetime import datetime
from typing import Literal, cast

from sandbox_science.contracts.mcp_metadata import McpToolMetadata

from .jsonrpc import JsonObject


def safe_text(value: str) -> str:
    """Omit addresses, credential placeholders and control characters in server prose."""
    value = value[:4000]
    value = re.sub(r"(?:https?://|file://|openshell:)[^\s\"']+", "[address omitted]", value)
    value = re.sub(r"(?i)bearer\s+\S+", "[credential omitted]", value)
    return "".join(char for char in value if char.isprintable() or char == "\n")[:2000]


def structure(value: object, depth: int = 0) -> dict[str, object]:
    """Keep structural schema keywords only, with explicit depth/property bounds."""
    if not isinstance(value, dict) or depth > 6:
        return {}
    source = cast(dict[str, object], value)
    result: dict[str, object] = {}
    kind = source.get("type")
    kinds = {"object", "array", "string", "number", "integer", "boolean", "null"}
    if isinstance(kind, str) and kind in kinds:
        result["type"] = kind
    for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        bound = source.get(key)
        if type(bound) is int or (type(bound) is float and math.isfinite(bound)):
            result[key] = bound
    properties = source.get("properties")
    if isinstance(properties, dict):
        result["properties"] = {
            name: structure(child, depth + 1)
            for name, child in list(cast(dict[str, object], properties).items())[:64]
            if re.fullmatch(r"[A-Za-z0-9_.$-]{1,64}", name)
        }
    required = source.get("required")
    if isinstance(required, list):
        result["required"] = [
            name
            for name in cast(list[object], required)[:64]
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.$-]{1,64}", name)
        ]
    if "items" in source:
        result["items"] = structure(source["items"], depth + 1)
    for key in ("oneOf", "anyOf", "allOf"):
        options = source.get(key)
        if isinstance(options, list):
            result[key] = [structure(item, depth + 1) for item in cast(list[object], options)[:8]]
    return result


class Metadata:
    """A bounded process-lifetime snapshot; restart makes observations unknown."""

    def __init__(self) -> None:
        self.tools: OrderedDict[
            tuple[str, str], tuple[datetime | None, str, tuple[McpToolMetadata, ...], bool]
        ] = OrderedDict()
        self.incomplete: set[tuple[str, str]] = set()
        self.observations: OrderedDict[
            tuple[str, str],
            tuple[datetime, Literal["unknown", "listed", "responded", "unavailable", "denied"]],
        ] = OrderedDict()

    def observe(
        self,
        project: str,
        server: str,
        at: datetime,
        status: Literal["unknown", "listed", "responded", "unavailable", "denied"],
    ) -> None:
        key = project, server
        snapshot = self.tools.get(key)
        if snapshot is not None and snapshot[0] is None:
            self.tools[key] = at, snapshot[1], snapshot[2], snapshot[3]
        self.observations[key] = at, status
        self.observations.move_to_end(key)
        while len(self.observations) > 32:
            self.observations.popitem(last=False)

    def listing(
        self,
        project: str,
        server: str,
        at: datetime | None,
        revision: str,
        result: JsonObject,
        *,
        first: bool,
    ) -> None:
        key = project, server
        previous = self.tools.get(key)
        tools = [] if first or previous is None or previous[1] != revision else list(previous[2])
        raw = result.get("tools")
        truncated = False if first or previous is None else previous[3]
        if isinstance(raw, list):
            for value in cast(list[object], raw):
                if (
                    sum(len(tool.parameters_json) + len(tool.description) for tool in tools)
                    > 512 * 1024
                ):
                    truncated = True
                    break
                if len(tools) >= 128:
                    truncated = True
                    break
                if not isinstance(value, dict):
                    continue
                item = cast(dict[str, object], value)
                name = item.get("name")
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,256}", name):
                    continue
                encoded = json.dumps(structure(item.get("inputSchema")), allow_nan=False)
                if len(encoded) > 16384:
                    encoded = "{}"
                    truncated = True
                if any(tool.name == name for tool in tools):
                    continue
                tools.append(
                    McpToolMetadata(
                        name=name,
                        description=safe_text(str(item.get("description", ""))),
                        parameters_json=encoded,
                        action="unknown",
                    )
                )
        if result.get("nextCursor"):
            self.incomplete.add(key)
        else:
            self.incomplete.discard(key)
        self.tools[key] = at, revision, tuple(tools), truncated
        self.tools.move_to_end(key)
        while len(self.tools) > 32:
            expired, _ = self.tools.popitem(last=False)
            self.incomplete.discard(expired)
