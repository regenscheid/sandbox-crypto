# SPDX-License-Identifier: MIT
"""The compiled policy of a sandbox whose agent calls MCP servers directly (D98)."""

from __future__ import annotations

from pathlib import Path

import yaml

from sandbox_science.policy import PoolCeiling, load_policy, parse_policy
from sandbox_science.policy.openshell import (
    AgentMcpTarget,
    AnyOf,
    Endpoint,
    ToolAdmission,
    compile_agent_mcp,
    tool_admission,
)

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = load_policy(ROOT / "examples" / "policy.yaml")
OPEN = PoolCeiling.unrestricted()
TARGET = AgentMcpTarget(
    middleware="sandbox-science-policy", project="crypto", binaries=("/usr/bin/python3.13",)
)


def endpoint() -> Endpoint:
    compiled = compile_agent_mcp(EXAMPLE, TARGET, ceiling=OPEN)
    (entry,) = compiled.policy.network_policies.values()
    (only,) = entry.endpoints
    return only


def called(point: Endpoint) -> set[str]:
    names: set[str] = set()
    for rule in point.rules or ():
        tool = rule.allow.tool
        if rule.allow.method == "tools/call":
            names |= set(tool.any) if isinstance(tool, AnyOf) else {str(tool)}
    return names


def test_each_declared_server_is_an_inspected_mcp_endpoint() -> None:
    point = endpoint()
    assert (point.protocol, point.enforcement) == ("mcp", "enforce")
    assert (point.host, point.ports, point.path) == (
        "mcp.example.invalid",
        (443,),
        "/crypto-research",
    )
    assert point.mcp is not None
    assert point.mcp.versions == ("2025-11-25",)
    assert point.credential_binding is not None
    assert point.credential_binding.provider == "crypto-research-mcp"
    methods = {rule.allow.method for rule in point.rules or ()}
    assert {"initialize", "notifications/initialized", "tools/list", "tools/call"} <= methods


def test_native_rules_admit_reads_and_escalations_and_never_a_denied_tool() -> None:
    names = called(endpoint())
    assert "attack-catalog-mcp_search_attacks" in names
    # The middleware holds escalated tools until approved, so the fence admits them by name.
    assert "firecrawl-mcp_firecrawl_monitor_create" in names
    assert not {
        name for name in names if name.startswith("portal_") and name != "portal_list_servers"
    }


def test_the_runtimes_client_shows_what_the_native_rules_admit_and_nothing_else() -> None:
    point = endpoint()
    admission = tool_admission(point)
    for name in called(point):
        if "*" not in name:
            assert admission.admits(name)
    assert admission.admits("firecrawl-mcp_firecrawl_monitor_create")  # escalated, admitted
    assert not admission.admits("portal_toggle_servers")  # denied by the policy
    assert not admission.admits("never-declared")
    # A deny entry wins, as at OpenShell; no rules admit nothing.
    assert not ToolAdmission(allow=("a_*",), deny=("a_b",)).admits("a_b")
    assert ToolAdmission(allow=("a_*",), deny=("a_b",)).admits("a_c")
    assert not ToolAdmission(allow=("a_*",)).admits("a_c.d")  # '*' stops at '.'
    assert not ToolAdmission().admits("a")


def test_the_middleware_decides_every_request_to_the_servers() -> None:
    compiled = compile_agent_mcp(EXAMPLE, TARGET, ceiling=OPEN)
    middlewares = compiled.policy.network_middlewares
    assert middlewares is not None
    entry = middlewares["sandbox-science-policy"]
    assert (entry.middleware, entry.on_error) == ("sandbox-science-policy", "fail_closed")
    assert entry.endpoints.include == ("mcp.example.invalid",)
    assert entry.config == {
        "policy": {"name": EXAMPLE.policy.metadata.name, "digest": EXAMPLE.digest},
        "mcp": {"project": "crypto", "caller": "agent-runtime"},
    }
    assert compiled.hosts == frozenset({"mcp.example.invalid"})
    document = yaml.safe_load(compiled.policy.to_yaml())
    assert document["network_policies"]["mcp-crypto-research-mcp"]["endpoints"][0]["protocol"] == (
        "mcp"
    )


def test_without_servers_there_is_nothing_to_reach() -> None:
    policy = parse_policy(
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: test}\n"
    )
    compiled = compile_agent_mcp(policy, TARGET, ceiling=OPEN)
    assert compiled.policy.network_policies == {}
    assert compiled.policy.network_middlewares is None
