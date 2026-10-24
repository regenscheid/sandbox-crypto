# SPDX-License-Identifier: MIT
"""What an approval card shows of a request (D97): a structured summary, not the payload.

A tool call is summarized by its classification's significant arguments (URL or host,
recipient, title, size, query); an HTTP request by its method, host, path, content type
and size and, for a JSON body, its top-level fields with long strings truncated. The
components that hold a request (the MCP proxy, the policy middleware) build the summary
when they record it, so the request's record carries it and nobody needs the payload.
Every text is bounded and printable; the workspace still shows it as text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final, cast
from urllib.parse import urlsplit

from sandbox_science.policy.effects import SignificantKind, argument_value

TEXT: Final = 160
"""Characters of one summarized value kept."""
FIELDS: Final = 20
"""Top-level JSON fields an HTTP summary lists."""
LISTED: Final = 3
"""Items of a list argument (URLs, recipients) a summary names."""
SCANNED_BODY: Final = 64 * 1024
"""Bytes of a body read for its fields: a larger JSON body is summarized by its size."""


def shown(value: object, limit: int = TEXT) -> str:
    """A value as one bounded printable line: strings as they are, other values as
    compact JSON, objects and arrays by their size."""
    if isinstance(value, Mapping):
        text = f"{{{len(cast('Mapping[str, object]', value))} fields}}"
    elif isinstance(value, list):
        text = f"[{len(cast('list[object]', value))} items]"
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value)
    text = " ".join("".join(c if c.isprintable() else " " for c in text[: limit * 2]).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def call_summary(
    significant: Mapping[SignificantKind, str], arguments: Mapping[str, object] | None
) -> dict[str, str]:
    """A tool call's significant arguments, by kind, from its classification entry. A URL
    gives the host too, when the entry names none."""
    found: dict[str, str] = {}
    first_url: str | None = None
    for kind, path in significant.items():
        present, value = argument_value(arguments or {}, path)
        if not present or value is None:
            continue
        texts = _texts(value)
        if texts:  # a list of URLs or recipients: the first few, and how many more
            more = f" (+{len(texts) - LISTED} more)" if len(texts) > LISTED else ""
            found[kind] = shown(", ".join(texts[:LISTED])) + more
            value = texts[0]
        else:
            found[kind] = shown(value)
        if kind == "url" and isinstance(value, str):
            first_url = value
    if first_url is not None and "host" not in found:
        host = _host(first_url)
        if host:
            found["host"] = shown(host)
    return found


def _texts(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = cast(list[object], value)
    texts = [item for item in items if isinstance(item, str)]
    return texts if texts and len(texts) == len(items) else None


def _host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def http_summary(
    method: str, host: str, port: int, path: str, content_type: str | None, body: bytes
) -> dict[str, object]:
    """An HTTP request as a card shows it: never the body itself."""
    summary: dict[str, object] = {
        "method": shown(method, 16),
        "host": shown(host, 253),
        "port": port,
        "path": shown(path, 300),
        "size": len(body),
    }
    if content_type:
        summary["content_type"] = shown(content_type, 100)
    fields = _json_fields(content_type, body)
    if fields is not None:
        summary["fields"] = fields
    return summary


def _json_fields(content_type: str | None, body: bytes) -> dict[str, str] | None:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if (
        not body
        or len(body) > SCANNED_BODY
        or not (media == "application/json" or media.endswith("+json"))
    ):
        return None
    try:
        value: object = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    items = cast(dict[str, object], value)
    return {shown(name, 64): shown(item) for name, item in list(items.items())[:FIELDS]}
