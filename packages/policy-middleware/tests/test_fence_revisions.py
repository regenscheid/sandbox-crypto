# SPDX-License-Identifier: MIT
"""A stale fence is never wider than the middleware (D96, D98): the agent runtime's native
rules are compiled with the project's classification in force when the runtime starts
(``PolicyStore.fence_classifications``), while the middleware reads the binding at every
call. A widening reaches the fence when it is compiled again; a narrowing applies to the
middleware's next call whatever a fence compiled before it admits."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling, reasons
from sandbox_science.policy.openshell import (
    AgentMcpTarget,
    ToolAdmission,
    compile_agent_mcp,
    tool_admission,
)
from sandbox_science.policy.store import Acceptance
from sandbox_science.policy_middleware.evaluator import HttpRequestFacts, PolicyMiddleware
from sandbox_science.policy_middleware.events import EventSink
from sandbox_science.policy_middleware.mcp import ToolListings
from sandbox_science.policy_middleware.store import PolicyStore

PROJECT = "crypto"
SERVER = "crypto-research-mcp"
READ_TOOL = "attack-catalog-mcp_search_attacks"
NEW_TOOL = "attack-catalog-mcp_brand_new"
OPEN = PoolCeiling.unrestricted()
TARGET = AgentMcpTarget(
    binaries=("/opt/sandbox-science/bin/python*",),
    middleware="sandbox-science-policy",
    project=PROJECT,
    caller="agent-runtime",
)


def fence(store: PolicyStore, loaded: LoadedPolicy) -> ToolAdmission:
    """The tools the runtime's fence admits, compiled as the launcher compiles it on ``up``
    and at each replacement: with the revisions the store binds at that moment."""
    found = store.fence_classifications(PROJECT, [SERVER])
    compiled = compile_agent_mcp(loaded, TARGET, ceiling=OPEN, classifications=found.revisions)
    (endpoint,) = compiled.policy.network_policies[f"mcp-{SERVER}"].endpoints
    return tool_admission(endpoint)


def accept(store: PolicyStore, tools: dict[str, str]) -> None:
    entries = "".join(
        f'  {name}:\n    class: {effect}\n    reason: "Reviewed."\n'
        for name, effect in tools.items()
    )
    data = (
        "apiVersion: sandbox-science/v0alpha1\nkind: ToolClassification\n"
        f"server: {SERVER}\ntools:\n{entries}"
    ).encode()
    store.accept_classification(
        PROJECT, SERVER, data, Acceptance("user", datetime.now(UTC), "reviewed")
    )


def call(middleware: PolicyMiddleware, loaded: LoadedPolicy, tool: str, request_id: str) -> Any:
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {"query": "SNOVA"}},
    }
    return middleware.evaluate_http(
        HttpRequestFacts(
            sandbox_id="sbx-agent",
            request_id=request_id,
            scheme="https",
            host="mcp.example.invalid",
            port=443,
            method="POST",
            path="/crypto-research",
            query="",
            headers=(("content-type", "application/json"), ("sandbox-science-session", "s-1")),
            body=json.dumps(body).encode(),
            config={
                "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
                "mcp": {"project": PROJECT, "caller": "agent-runtime"},
            },
        )
    )


def setup(store: PolicyStore, sink: EventSink) -> tuple[LoadedPolicy, PolicyMiddleware]:
    source = Path(__file__).resolve().parents[3] / "examples" / "policy.yaml"
    loaded = store.add_policy(source.read_bytes())
    store.bind("sbx-agent", loaded.digest)
    middleware = PolicyMiddleware(
        store, ceiling=OPEN, floor=HardFloor(), events=sink, listings=ToolListings()
    )
    return loaded, middleware


def test_without_a_revision_the_fence_and_the_middleware_take_the_catalogs(
    store: PolicyStore, sink: EventSink
) -> None:
    loaded, middleware = setup(store, sink)
    found = store.fence_classifications(PROJECT, [SERVER])
    assert (dict(found.revisions), dict(found.unreadable)) == ({}, {})
    assert fence(store, loaded) == tool_admission(
        compile_agent_mcp(loaded, TARGET, ceiling=OPEN)
        .policy.network_policies[f"mcp-{SERVER}"]
        .endpoints[0]
    )
    assert fence(store, loaded).admits(READ_TOOL)
    assert call(middleware, loaded, READ_TOOL, "r0").allowed


def test_a_widening_reaches_the_fence_when_it_is_compiled_again(
    store: PolicyStore, sink: EventSink
) -> None:
    loaded, middleware = setup(store, sink)
    stale = fence(store, loaded)
    assert not stale.admits(NEW_TOOL)  # the catalog does not name it
    accept(store, {READ_TOOL: "read", NEW_TOOL: "read"})
    # The middleware allows it from the next call, but the stale fence still refuses it
    # by name: stricter, never wider.
    assert call(middleware, loaded, NEW_TOOL, "r1").allowed
    assert not stale.admits(NEW_TOOL)
    # The replacement's compile admits it.
    assert fence(store, loaded).admits(NEW_TOOL)


def test_a_narrowing_is_refused_by_the_middleware_whatever_the_stale_fence_admits(
    store: PolicyStore, sink: EventSink
) -> None:
    loaded, middleware = setup(store, sink)
    stale = fence(store, loaded)
    assert stale.admits(READ_TOOL)
    assert call(middleware, loaded, READ_TOOL, "r0").allowed
    accept(store, {READ_TOOL: "browse"})  # browser automation is off: denied
    refused = call(middleware, loaded, READ_TOOL, "r1")
    assert not refused.allowed
    assert refused.decision is not None
    assert refused.decision.effect == "browse"
    assert stale.admits(READ_TOOL)  # the fence compiled before admits it still
    assert not fence(store, loaded).admits(READ_TOOL)  # the next compile does not


def test_an_unreadable_binding_leaves_the_fence_on_the_catalogs_and_the_middleware_refuses(
    store: PolicyStore, sink: EventSink, tmp_path: Path
) -> None:
    loaded, middleware = setup(store, sink)
    catalog = fence(store, loaded)
    accept(store, {READ_TOOL: "read", NEW_TOOL: "read"})
    current = tmp_path / "state" / "classifications" / PROJECT / SERVER / "current.json"
    for broken in ("not json", json.dumps({"revision": "a" * 64})):
        current.write_text(broken)
        found = store.fence_classifications(PROJECT, [SERVER])
        assert dict(found.revisions) == {}
        assert SERVER in found.unreadable
        # The fence falls back to the catalog's, so the revision's widening is not admitted…
        assert fence(store, loaded) == catalog
        assert not fence(store, loaded).admits(NEW_TOOL)
        # …and the middleware refuses every call of the server, the catalog's reads too.
        refused = call(middleware, loaded, READ_TOOL, f"r-{len(broken)}")
        assert refused.reason_code == reasons.POLICY_UNAVAILABLE
