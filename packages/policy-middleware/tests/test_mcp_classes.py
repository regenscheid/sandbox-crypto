# SPDX-License-Identifier: MIT
"""Effect classes at the policy middleware (D96, D98): calls are decided by class, as the
library and the MCP proxy decide them; listings give hints and are checked against the
classification's pins; ``mcp: locked`` refuses a tool the policy does not name."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling, evaluate_mcp_message
from sandbox_science.policy_middleware.evaluator import HttpRequestFacts, PolicyMiddleware, Verdict
from sandbox_science.policy_middleware.mcp import ToolListings
from sandbox_science.policy_middleware.store import PolicyStore

from .conftest import RecordingSink

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages" / "policy" / "tests" / "fixtures"
MATRIX = json.loads((FIXTURES / "mcp-class-matrix.json").read_text())
SANDBOX = "sbx-0001"
SERVER = "crypto-research-mcp"
ATTRIBUTION = (("sandbox-science-session", "s-42"), ("sandbox-science-run", "main-1"))
EXAMPLE = ROOT / "examples" / "policy.yaml"


def kinds(sink: RecordingSink, kind: str) -> list[Any]:
    return [payload for name, payload in sink.events if name == kind]


def bind(store: PolicyStore, text: bytes) -> LoadedPolicy:
    loaded = store.add_policy(text)
    store.bind(SANDBOX, loaded.digest)
    return loaded


@pytest.fixture
def listings() -> ToolListings:
    return ToolListings()


def make(store: PolicyStore, listings: ToolListings, sink: RecordingSink) -> PolicyMiddleware:
    return PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=sink, listings=listings
    )


def facts_for(policy: LoadedPolicy) -> Callable[..., HttpRequestFacts]:
    def build(body: bytes, request_id: str = "req-1") -> HttpRequestFacts:
        return HttpRequestFacts(
            sandbox_id=SANDBOX,
            request_id=request_id,
            scheme="https",
            host="mcp.example.invalid",
            port=443,
            method="POST",
            path="/crypto-research",
            query="",
            headers=(("content-type", "application/json"), *ATTRIBUTION),
            body=body,
            config={
                "policy": {"name": policy.policy.metadata.name, "digest": policy.digest},
                "mcp": {"project": "crypto", "caller": "agent-runtime"},
            },
        )

    return build


def message(method: str, params: dict[str, Any]) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 7, "method": method, "params": params}).encode()


def outcome(verdict: Verdict) -> str:
    if verdict.allowed:
        return "allow"
    return "escalate" if verdict.reason_code.startswith("approval_required") else "deny"


@pytest.mark.parametrize("entry", MATRIX["calls"], ids=lambda entry: entry["tool"])
def test_the_shared_matrix(
    store: PolicyStore, listings: ToolListings, sink: RecordingSink, entry: dict[str, Any]
) -> None:
    """The middleware's half of the agreement with the library and the MCP proxy."""
    loaded = bind(store, EXAMPLE.read_bytes())
    middleware = make(store, listings, sink)
    params = {"name": entry["tool"], "arguments": entry["arguments"]}
    verdict = middleware.evaluate_http(facts_for(loaded)(message("tools/call", params)))
    assert outcome(verdict) == entry["action"], verdict.reason
    assert verdict.decision is not None
    assert verdict.decision.effect == entry["effect"]
    library = evaluate_mcp_message(
        SERVER,
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params},
        loaded,
        ceiling=PoolCeiling.unrestricted(),
    )
    assert library.action == verdict.decision.action


def test_locked_mcp_refuses_a_tool_the_policy_does_not_name(
    store: PolicyStore, listings: ToolListings, sink: RecordingSink
) -> None:
    text = EXAMPLE.read_text().replace("  mcp: declared ", "  mcp: locked   ", 1)
    assert "mcp: locked" in text
    loaded = bind(store, text.encode())
    middleware = make(store, listings, sink)
    facts = facts_for(loaded)
    new = message("tools/call", {"name": "e-print-mcp_brand_new", "arguments": {}})
    verdict = middleware.evaluate_http(facts(new))
    assert (verdict.allowed, verdict.reason_code) == (False, "policy_denied")
    assert verdict.decision is not None
    assert verdict.decision.rule == "lockdown.mcp"
    known = message("tools/call", {"name": "e-print-mcp_search_eprint", "arguments": {}})
    assert middleware.evaluate_http(facts(known)).allowed


def pinned_tools() -> list[dict[str, Any]]:
    folder = FIXTURES / "mcp-tools" / SERVER
    return [
        tool
        for path in sorted(folder.glob("*.json"))
        for tool in json.loads(path.read_text())["tools"]
    ]


def answer_listing(
    middleware: PolicyMiddleware,
    facts: Callable[..., HttpRequestFacts],
    tools: list[dict[str, Any]],
) -> object:
    """Let a tools/list through and pass its answer, as OpenShell would; the drift, if any."""
    verdict = middleware.evaluate_http(facts(message("tools/list", {}), request_id="list-1"))
    assert verdict.allowed
    check = middleware.check_listing("list-1", SANDBOX, stream=False)
    assert check is not None
    check.feed(json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"tools": tools}}).encode())
    return check.finish().drift


def test_the_pinned_listing_is_no_drift_and_a_changed_schema_is(
    store: PolicyStore, listings: ToolListings, sink: RecordingSink
) -> None:
    loaded = bind(store, EXAMPLE.read_bytes())
    middleware = make(store, listings, sink)
    facts = facts_for(loaded)
    tools = pinned_tools()
    assert answer_listing(middleware, facts, tools) is None
    assert kinds(sink, "mcp-tools-drift") == []
    changed = [dict(tool) for tool in tools]
    scrape = next(t for t in changed if t["name"] == "firecrawl-mcp_firecrawl_scrape")
    scrape["inputSchema"] = {**scrape["inputSchema"], "properties": {"url": {"type": "string"}}}
    changed.append({"name": "e-print-mcp_brand_new", "inputSchema": {"type": "object"}})
    drift = answer_listing(middleware, facts, changed)
    assert drift is not None
    [record] = kinds(sink, "mcp-tools-drift")
    assert record["finding"] == "tool_list_drift"
    assert record["changed"] == ["firecrawl-mcp_firecrawl_scrape"]
    assert record["added"] == ["e-print-mcp_brand_new"]


def test_a_listings_hints_decide_tools_the_policy_does_not_classify(
    store: PolicyStore, listings: ToolListings, sink: RecordingSink
) -> None:
    loaded = bind(store, EXAMPLE.read_bytes())
    middleware = make(store, listings, sink)
    facts = facts_for(loaded)
    new = message("tools/call", {"name": "e-print-mcp_brand_new", "arguments": {}})
    assert outcome(middleware.evaluate_http(facts(new))) == "escalate"  # no hint yet
    hinted = {
        "name": "e-print-mcp_brand_new",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    }
    answer_listing(middleware, facts, [*pinned_tools(), hinted])
    verdict = middleware.evaluate_http(facts(new, request_id="req-2"))
    assert verdict.allowed, verdict.reason
    assert verdict.decision is not None
    assert verdict.decision.effect == "read"
