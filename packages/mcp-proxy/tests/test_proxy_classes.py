# SPDX-License-Identifier: MIT
"""Effect classes at the MCP proxy (D96): calls are decided by class with their arguments,
as the library and the policy middleware decide them, and a listing's hints decide tools the
policy does not classify."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from .conftest import Harness

ROOT = Path(__file__).resolve().parents[3]
MATRIX = json.loads(
    (ROOT / "packages" / "policy" / "tests" / "fixtures" / "mcp-class-matrix.json").read_text()
)
TOOLS = '        allow: [search, image, stream, large, broken, unlisted, "*_search"]\n'


def classified(harness: Harness, lockdown: str = "") -> str:
    """The test policy with the example's MCP settings for its server."""
    text = harness.policy()
    start = text.index("      tools:\n")
    return (
        text[:start]
        + "      classification: crypto-research-mcp\n"
        + '      tools: {allow: [portal_list_servers], deny: ["portal_*"]}\n'
        + lockdown
    )


def outcome(body: Any) -> str:
    if "result" in body:
        return "allow"
    reason = body["error"]["data"]["reason"]
    return "escalate" if reason == "approval_required" else "deny"


async def test_the_shared_matrix(harness: Harness) -> None:
    """The proxy's half of the agreement with the library and the policy middleware."""
    harness.fake.tools = sorted({entry["tool"] for entry in MATRIX["calls"]})
    harness.bind(classified(harness))
    session = await harness.initialize()
    seen = []
    for entry in MATRIX["calls"]:
        answer = await harness.tool(session, entry["tool"], entry["arguments"])
        seen.append((entry["tool"], outcome(answer.body)))
    assert seen == [(entry["tool"], entry["action"]) for entry in MATRIX["calls"]]
    forwarded = [entry["tool"] for entry in MATRIX["calls"] if entry["action"] == "allow"]
    assert harness.fake.tool_calls() == forwarded
    calls = harness.sink.kinds("mcp-call")
    assert {call["tool"] for call in calls} >= {entry["tool"] for entry in MATRIX["calls"]}


@pytest.mark.parametrize(
    ("annotations", "expected"), [({"readOnlyHint": True}, "allow"), ({}, "escalate")]
)
async def test_a_listings_hint_decides_an_unclassified_tool(
    harness: Harness, annotations: dict[str, object], expected: str
) -> None:
    harness.fake.tools = ["e-print-mcp_search_eprint", "e-print-mcp_brand_new"]
    tools = [
        {"name": name, "inputSchema": {"type": "object"}, "annotations": annotations}
        for name in harness.fake.tools
    ]
    harness.fake.listing = lambda _: {"tools": tools}
    harness.bind(classified(harness))
    session = await harness.initialize()
    listed = await harness.call(session, "tools/list", {})
    assert outcome(listed.body) == "allow"
    answer = await harness.tool(session, "e-print-mcp_brand_new", {})
    assert outcome(answer.body) == expected


async def test_locked_mcp_refuses_a_tool_the_policy_does_not_name(harness: Harness) -> None:
    harness.fake.tools = ["e-print-mcp_search_eprint", "e-print-mcp_brand_new"]
    harness.bind(classified(harness, "lockdown: {mcp: locked}\n"))
    session = await harness.initialize()
    new = await harness.tool(session, "e-print-mcp_brand_new", {})
    body: Any = new.body
    assert body["error"]["data"]["reason"] == "policy_denied"
    known = await harness.tool(session, "e-print-mcp_search_eprint", {"query": "SNOVA"})
    assert outcome(known.body) == "allow"


async def test_listings_tell_callers_each_tools_class(harness: Harness) -> None:
    harness.fake.tools = [
        "e-print-mcp_search_eprint",
        "firecrawl-mcp_firecrawl_scrape",
        "firecrawl-mcp_firecrawl_feedback",
        "e-print-mcp_brand_new",
    ]
    tools = [
        {
            "name": name,
            "inputSchema": {"type": "object"},
            "_meta": {"sandbox-science/effect": "read"},
        }
        for name in harness.fake.tools
    ]
    harness.fake.listing = lambda _: {"tools": tools}
    harness.bind(classified(harness))
    session = await harness.initialize()
    listed: Any = (await harness.call(session, "tools/list", {})).body
    effects = {
        tool["name"]: tool["_meta"]["sandbox-science/effect"] for tool in listed["result"]["tools"]
    }
    assert effects == {
        "e-print-mcp_search_eprint": "read",
        "firecrawl-mcp_firecrawl_scrape": "browse",  # a call with actions is not a read
        "firecrawl-mcp_firecrawl_feedback": "write",
        "e-print-mcp_brand_new": "unclassified",  # the server's own word is replaced
    }
