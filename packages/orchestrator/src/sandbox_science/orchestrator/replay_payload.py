# SPDX-License-Identifier: MIT
"""Project archived provider responses onto the fields our adapters replay."""

from __future__ import annotations

import json
from typing import Any, cast


def replay_native(native: bytes | None, native_format: str | None) -> bytes | None:
    """Keep replay semantics without retaining echoed requests in working context.

    The caller keeps the original in the archive. Unknown or malformed formats
    retain their bytes rather than silently assuming a future adapter's contract.
    """
    if native is None or native_format not in {"responses", "messages", "chat_completions"}:
        return native
    if native_format == "chat_completions":
        return None  # This adapter replays only the normalized chat message.
    try:
        parsed: object = json.loads(native)
    except (ValueError, TypeError, RecursionError):
        return native
    if not isinstance(parsed, dict):
        return native
    source = cast(dict[str, Any], parsed)
    if native_format == "responses":
        kept = {"output": source.get("output")}
    else:
        raw_usage = source.get("usage")
        usage = cast(dict[str, Any], raw_usage) if isinstance(raw_usage, dict) else {}
        kept = {
            "content": source.get("content"),
            "usage": {
                key: usage[key]
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
                if key in usage
            },
        }
    return json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode()
