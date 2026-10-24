# SPDX-License-Identifier: MIT
"""What the proxy reads from results, and what it changes in them before callers see them.

Binary content is withheld (``sandbox_science.policy.mcp_answers``): image and audio
blocks and blob resources are replaced by a text note naming their type and size, rather
than passing base64 into model context. The proxy cannot reach control's artifact store,
so the bytes are dropped here; on the direct path (D98) the harness keeps them as
artifacts instead (docs/MCP-CONNECTORS.md).
"""

from __future__ import annotations

from typing import Final

from sandbox_science.policy import mcp_answers, mcp_wire
from sandbox_science.policy.mcp_wire import McpWireError

from .errors import MessageError
from .jsonrpc import JsonObject

_NOTE: Final = mcp_answers.withheld_note("the MCP proxy")


def tool_names(result: JsonObject) -> tuple[list[str], str | None]:
    """The tool names and the next cursor of a ``tools/list`` result.

    Raises MessageError when the result does not have the shape MCP gives it.
    """
    try:
        return mcp_wire.tool_names(result)
    except McpWireError as exc:
        raise MessageError(str(exc)) from exc


def withhold_binary(method: str, result: JsonObject) -> tuple[JsonObject, int]:
    """Replace binary content in a result with text notes; return the result and how many.
    The rule is the policy library's, which the policy middleware's path shares (D98)."""
    return mcp_answers.withhold_binary(method, result, _NOTE)
