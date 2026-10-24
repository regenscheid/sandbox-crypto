# SPDX-License-Identifier: MIT
"""Lockdown modes and effect classes compiled into OpenShell policy (D95, D96): the fence
admits what the library would, and under a lockdown never more."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sandbox_science.policy import (
    Action,
    LoadedPolicy,
    PoolCeiling,
    evaluate_mcp_message,
    parse_policy,
)
from sandbox_science.policy.classification import catalog
from sandbox_science.policy.openshell import (
    AgentMcpTarget,
    AnyOf,
    Compiled,
    admission_refusal,
    compile_agent_mcp,
    compile_mcp_proxy,
    compile_sandbox,
    load_targets,
)

ROOT = Path(__file__).resolve().parents[3]
TARGETS = load_targets(ROOT / "examples" / "openshell" / "targets.yaml")
OPEN = PoolCeiling.unrestricted()
AGENT = AgentMcpTarget(
    middleware="sandbox-science-policy", project="crypto", binaries=("/usr/bin/python3.13",)
)
HEADER = "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: test}\n"
SERVER = "crypto-research-mcp"


def mcp_policy(top: str = "", tools: str = "{}", mcp: str = "") -> LoadedPolicy:
    return parse_policy(
        HEADER
        + top
        + "mcp:\n"
        + mcp
        + "  servers:\n    crypto-research-mcp:\n      url: https://mcp.example.org/crypto\n"
        + "      revision: '2025-11-25'\n      auth: {mode: none}\n"
        + f"      classification: crypto-research-mcp\n      tools: {tools}\n"
    )


def fence_admits(compiled: Compiled, tool: str) -> bool:
    """Whether the compiled native rules admit ``tools/call`` of ``tool`` (OpenShell's globs)."""
    (endpoint,) = compiled.policy.network_policies[f"mcp-{SERVER}"].endpoints
    denied = [entry.tool for entry in endpoint.deny_rules or () if isinstance(entry.tool, str)]
    if any(_glob(pattern, tool) for pattern in denied):
        return False
    for rule in endpoint.rules or ():
        allowed = rule.allow
        if allowed.method != "tools/call":
            continue
        names = allowed.tool.any if isinstance(allowed.tool, AnyOf) else (allowed.tool,)
        if any(isinstance(name, str) and _glob(name, tool) for name in names):
            return True
    return False


def _glob(pattern: str, name: str) -> bool:
    if pattern == "**":
        return True
    pieces = re.split(r"(\*)", pattern)
    return (
        re.fullmatch("".join("[^.]*" if p == "*" else re.escape(p) for p in pieces), name)
        is not None
    )


def decide(policy: LoadedPolicy, tool: str) -> Action:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}}
    return evaluate_mcp_message(SERVER, message, policy, ceiling=OPEN).action


@pytest.mark.parametrize("admit", [False, True])
def test_the_fence_admits_classified_tools_as_their_class_allows(admit: bool) -> None:
    policy = mcp_policy()
    compiled = compile_mcp_proxy(policy, TARGETS.mcp_proxy, ceiling=OPEN, admit_escalated=admit)
    for tool in catalog(SERVER).tools:
        action = decide(policy, tool)
        expected = action is Action.ALLOW or (admit and action is Action.ESCALATE)
        assert fence_admits(compiled, tool) is expected, (tool, action)


def test_the_agent_fence_admits_escalations_and_never_browsing_while_it_is_off() -> None:
    compiled = compile_agent_mcp(mcp_policy(), AGENT, ceiling=OPEN)
    assert fence_admits(compiled, "firecrawl-mcp_firecrawl_monitor_create")
    assert fence_admits(
        compiled, "firecrawl-mcp_firecrawl_scrape"
    )  # read; actions are the middleware's
    assert not fence_admits(compiled, "firecrawl-mcp_firecrawl_interact")
    enabled = compile_agent_mcp(
        mcp_policy(mcp="  browser_automation: enabled\n"), AGENT, ceiling=OPEN
    )
    assert fence_admits(enabled, "firecrawl-mcp_firecrawl_interact")


@pytest.mark.parametrize("compile_fence", ["proxy", "agent"])
def test_locked_mcp_compiles_exact_names_only(compile_fence: str) -> None:
    tools = '{allow: ["*_search"]}'
    declared, locked = mcp_policy(tools=tools), mcp_policy("lockdown: {mcp: locked}\n", tools=tools)

    def fence(policy: LoadedPolicy) -> Compiled:
        if compile_fence == "proxy":
            return compile_mcp_proxy(policy, TARGETS.mcp_proxy, ceiling=OPEN)
        return compile_agent_mcp(policy, AGENT, ceiling=OPEN)

    added = "e-print-mcp_brand_new_search"
    assert fence_admits(fence(declared), added)
    assert decide(declared, added) is Action.ALLOW
    compiled = fence(locked)
    assert not fence_admits(compiled, added)
    assert decide(locked, added) is Action.DENY
    assert fence_admits(compiled, "e-print-mcp_search_eprint")
    assert any("MCP is locked" in finding.message for finding in compiled.findings)


ALLOWLIST = (
    HEADER.replace("{", "{{").replace("}", "}}")
    + "lockdown:\n  network: allowlist\n  hosts: [eprint.iacr.org, '*.arxiv.org']\n"
    + "  unlisted: {unlisted}\n"
    + "network:\n  rules:\n"
    + "    - {{name: reads, operation: http, match: {{hosts: ['*'], methods: [GET]}},"
    + " action: allow}}\n"
    + "    - {{name: hub, operation: http, match: {{hosts: [github.com]}}, action: allow}}\n"
)


def egress_hosts(compiled: Compiled) -> set[str]:
    return {
        endpoint.host
        for name, entry in compiled.policy.network_policies.items()
        if name.startswith("sandbox-science-egress") or name == "sandbox-science-admitted"
        for endpoint in entry.endpoints
        if endpoint.host is not None
    }


def test_an_allowlist_names_only_listed_hosts_and_admits_nothing_else() -> None:
    policy = parse_policy(ALLOWLIST.format(unlisted="deny"))
    compiled = compile_sandbox(policy, TARGETS.sandbox, ceiling=OPEN, admitted=["example.org"])
    hosts = egress_hosts(compiled)
    assert hosts == {"eprint.iacr.org", "**.arxiv.org"}
    assert "github.com" not in hosts  # a rule names it, the allowlist does not
    reason = admission_refusal(policy, TARGETS.sandbox, "example.org", 443, ceiling=OPEN)
    assert reason is not None
    assert admission_refusal(policy, TARGETS.sandbox, "export.arxiv.org", 443, ceiling=OPEN) is None


def test_an_allowlist_that_escalates_admits_unlisted_hosts_on_demand() -> None:
    policy = parse_policy(ALLOWLIST.format(unlisted="escalate"))
    assert admission_refusal(policy, TARGETS.sandbox, "example.org", 443, ceiling=OPEN) is None
    compiled = compile_sandbox(policy, TARGETS.sandbox, ceiling=OPEN, admitted=["example.org"])
    assert {"example.org", "github.com", "eprint.iacr.org"} <= egress_hosts(compiled)


def test_open_reads_compile_as_before() -> None:
    lockdown = "lockdown:\n  network: allowlist\n  hosts: [eprint.iacr.org, '*.arxiv.org']\n"
    text = ALLOWLIST.format(unlisted="deny").replace(lockdown + "  unlisted: deny\n", "")
    assert "lockdown" not in text
    compiled = compile_sandbox(parse_policy(text), TARGETS.sandbox, ceiling=OPEN)
    assert "github.com" in egress_hosts(compiled)
