# SPDX-License-Identifier: MIT
"""What both MCP enforcement points change in answers (mcp_answers): listings without
denied tools, binary items withheld, and answers rewritten as JSON or event streams."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.policy import PoolCeiling, load_policy
from sandbox_science.policy.mcp_answers import (
    ESCALATES_META,
    Withheld,
    binary_contents,
    callable_tools,
    rewrite_answer,
    tool_summary,
    withheld_note,
    withhold_binary,
)
from sandbox_science.policy.mcp_wire import McpWireError

ROOT = Path(__file__).resolve().parents[3]
SERVER = "crypto-research-mcp"


def test_a_listing_loses_denied_tools_and_marks_escalated_ones() -> None:
    loaded = load_policy(ROOT / "examples" / "policy.yaml")
    names = [
        "attack-catalog-mcp_search_attacks",
        "firecrawl-mcp_firecrawl_monitor_create",
        "portal_toggle_servers",
    ]
    result: dict[str, object] = {"tools": [{"name": name} for name in names], "nextCursor": "n"}
    kept, denied = callable_tools(SERVER, result, loaded, ceiling=PoolCeiling.unrestricted())
    assert denied == 1
    tools = cast("list[dict[str, Any]]", kept["tools"])
    assert [tool["name"] for tool in tools] == names[:2]
    assert ESCALATES_META not in tools[0]["_meta"]
    assert tools[1]["_meta"][ESCALATES_META] is True
    assert kept["nextCursor"] == "n"
    assert cast("list[object]", result["tools"])[0] == {"name": names[0]}  # not in place


def test_binary_items_are_found_and_replaced_by_the_callers_note() -> None:
    result: dict[str, object] = {
        "content": [
            {"type": "text", "text": "kept"},
            {"type": "image", "mimeType": "image/png", "data": "AAAA"},
            {"type": "resource", "resource": {"uri": "file:///x", "blob": "QkI=", "mimeType": "x"}},
        ]
    }
    found = binary_contents("tools/call", result)
    assert found == [Withheld(0, "image", "image/png", "AAAA"), Withheld(1, "blob", None, "QkI=")]
    replaced, count = withhold_binary("tools/call", result, lambda item: f"kept #{item.index}")
    assert count == 2
    assert cast("list[object]", replaced["content"])[1:] == [
        {"type": "text", "text": "kept #0"},
        {"type": "text", "text": "kept #1"},
    ]
    note = withheld_note("the policy middleware")(found[0])
    assert note.startswith("[image/png withheld by the policy middleware: 4 bytes")
    assert binary_contents("tools/list", result) == []


def test_a_summary_names_a_calls_significant_arguments() -> None:
    loaded = load_policy(ROOT / "examples" / "policy.yaml")
    server = loaded.policy.mcp.servers[SERVER]
    params = {
        "name": "firecrawl-mcp_firecrawl_monitor_create",
        "arguments": {"email": "a@example.org", "page": "https://example.org/p"},
    }
    summary = tool_summary(server, "firecrawl-mcp_firecrawl_monitor_create", params)
    assert summary["recipient"] == "a@example.org"
    assert tool_summary(server, None, params) == {}


def _drop_b(result: dict[str, object]) -> dict[str, object]:
    tools = cast("list[dict[str, Any]]", result["tools"])
    return {**result, "tools": [tool for tool in tools if tool["name"] != "b"]}


def test_a_json_answer_is_rewritten_only_for_its_request() -> None:
    answer = {"jsonrpc": "2.0", "id": 7, "result": {"tools": [{"name": "a"}, {"name": "b"}]}}
    body = json.dumps(answer).encode()
    rewritten = json.loads(rewrite_answer(body, stream=False, request_id=7, rewrite=_drop_b))
    assert rewritten["result"]["tools"] == [{"name": "a"}]
    # Another id, or the same number as text, is not this request's response.
    for other in (8, "7"):
        same = json.loads(rewrite_answer(body, stream=False, request_id=other, rewrite=_drop_b))
        assert same == answer


def test_an_event_stream_is_rewritten_event_by_event() -> None:
    notice = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"p": 1}}
    answer = {"jsonrpc": "2.0", "id": 7, "result": {"tools": [{"name": "a"}, {"name": "b"}]}}
    body = (
        b": a comment\r\n"
        + b"id: 1\r\nevent: message\r\ndata: "
        + json.dumps(notice).encode()
        + b"\r\n\r\nevent: message\ndata: "
        + json.dumps(answer).encode()
        + b"\n\n"
    )
    out = rewrite_answer(body, stream=True, request_id=7, rewrite=_drop_b)
    messages = [json.loads(line[6:]) for line in out.splitlines() if line.startswith(b"data: ")]
    assert messages[0] == notice
    assert messages[1]["result"]["tools"] == [{"name": "a"}]
    assert out.startswith(b"id: 1\n")


@pytest.mark.parametrize(
    "body",
    [
        b"event: message\ndata: {not json\n\n",
        b'event: message\ndata: {"id":7,"id":8}\n\n',
        b"event: message\ndata: 5\n\n",
    ],
)
def test_an_unreadable_stream_is_refused_not_passed(body: bytes) -> None:
    with pytest.raises(McpWireError):
        rewrite_answer(body, stream=True, request_id=7, rewrite=_drop_b)
