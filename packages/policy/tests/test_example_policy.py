# SPDX-License-Identifier: MIT
"""The shipped example policy stays valid and behaves as docs/POLICY.md describes."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import TypedDict

import pytest

from sandbox_science.policy import (
    Action,
    EgressUsage,
    HttpRequest,
    LoadedPolicy,
    McpOperation,
    PackageOperation,
    PoolCeiling,
    effective_resources,
    evaluate,
    evaluate_mcp_message,
    evaluate_monitors,
    evaluate_request,
    load_policy,
)

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "policy.yaml"
FIXTURE = Path(__file__).parent / "fixtures" / "crypto-research-mcp-tools.json"
CEILING = PoolCeiling.unrestricted()
PREFIX = "crypto_research_mcp-"


class Tool(TypedDict):
    name: str
    # READ, SPEND, WRITE or CONFIG, from each tool's own description (2026-10-10).
    class_: str


# Deliberate choices in examples/policy.yaml that differ from a tool's class.
ALLOWED_DESPITE_CLASS = {
    # Per-call billed searches: cheap, and searching is the core research workflow.
    "firecrawl-mcp_firecrawl_search",
    "paper-search-mcp_scholar_search",
    "paper-search-mcp_search_papers",
    "patent-search-mcp_patent_search",
    # Reads that fill the server's own document cache on first use.
    "e-print-mcp_eprint_file",
    "e-print-mcp_eprint_fulltext",
    "nist-mcp_csrc_file",
    "nist-mcp_csrc_fulltext",
}
EXPECTED_BY_CLASS = {
    "READ": Action.ALLOW,
    "SPEND": Action.ESCALATE,
    "WRITE": Action.ESCALATE,
    "CONFIG": Action.DENY,
}


def load_tools() -> list[Tool]:
    data = json.loads(FIXTURE.read_text())
    return [Tool(name=entry["name"], class_=entry["class"]) for entry in data["tools"]]


@pytest.fixture(scope="module")
def policy() -> LoadedPolicy:
    return load_policy(EXAMPLE)


@pytest.mark.parametrize("tool", load_tools(), ids=lambda tool: tool["name"].removeprefix(PREFIX))
def test_every_crypto_research_mcp_tool(policy: LoadedPolicy, tool: Tool) -> None:
    short = tool["name"].removeprefix(PREFIX)
    expected = Action.ALLOW if short in ALLOWED_DESPITE_CLASS else EXPECTED_BY_CLASS[tool["class_"]]
    operation = McpOperation(server="crypto-research-mcp", method="tools/call", tool=tool["name"])
    decision = evaluate(operation, policy, ceiling=CEILING)
    assert decision.action is expected, decision.reason


def test_fixture_covers_the_deliberate_exceptions() -> None:
    names = {tool["name"].removeprefix(PREFIX) for tool in load_tools()}
    assert names >= ALLOWED_DESPITE_CLASS
    assert len(names) == 78


def test_search_feedback_is_not_allowed_by_the_search_pattern(policy: LoadedPolicy) -> None:
    tool = PREFIX + "firecrawl-mcp_firecrawl_search_feedback"
    decision = evaluate(
        McpOperation(server="crypto-research-mcp", method="tools/call", tool=tool),
        policy,
        ceiling=CEILING,
    )
    assert decision.action is Action.ESCALATE
    assert decision.rule == "mcp.servers.crypto-research-mcp.tools.escalate[*feedback*]"


@pytest.mark.parametrize(
    ("method", "url", "expected", "rule"),
    [
        ("GET", "https://eprint.iacr.org/2024/123.pdf", Action.ALLOW, "read-only-web"),
        ("HEAD", "https://example.org/", Action.ALLOW, "read-only-web"),
        ("POST", "https://example.org/api", Action.ESCALATE, "external-writes"),
        ("DELETE", "https://example.org/x", Action.ESCALATE, "external-writes"),
        (
            "POST",
            "https://github.com/regenscheid/sandbox-crypto.git/git-upload-pack",
            Action.ALLOW,
            "git-fetch",
        ),
        (
            "GET",
            "https://github.com/regenscheid/sandbox-crypto.git/info/refs?service=git-upload-pack",
            Action.ALLOW,
            "git-fetch",
        ),
        (
            "POST",
            "https://github.com/regenscheid/sandbox-crypto.git/git-receive-pack",
            Action.ESCALATE,
            "git-push",
        ),
        (
            "GET",
            "https://github.com/regenscheid/sandbox-crypto.git/info/refs?service=git-receive-pack",
            Action.ESCALATE,
            "git-push",
        ),
        # A fetch from a git host the policy does not list: no git rule, so the default applies.
        ("POST", "https://git.example.org/x.git/git-upload-pack", Action.ESCALATE, None),
        # Non-default port: no rule names it, so the default applies.
        ("GET", "https://example.org:8443/", Action.ESCALATE, None),
        # Non-public destinations: a wildcard allow cannot grant them.
        ("GET", "http://192.168.1.10/admin", Action.DENY, "read-only-web"),
        ("GET", "http://nas.local/", Action.DENY, "read-only-web"),
        # Hard floor.
        ("GET", "http://169.254.169.254/latest/meta-data/", Action.DENY, None),
        ("GET", "http://localhost:8080/", Action.DENY, None),
        # URL length limit on the broad read rule.
        ("GET", "https://example.org/" + "a" * 5000, Action.DENY, "read-only-web"),
        # Ambiguous request.
        ("GET", "https://example.org/a/../admin", Action.DENY, None),
    ],
)
def test_example_network_rules(
    policy: LoadedPolicy, method: str, url: str, expected: Action, rule: str | None
) -> None:
    decision = evaluate_request(HttpRequest(method, url), policy, ceiling=CEILING)
    assert decision.action is expected, decision.reason
    assert decision.rule == rule
    assert decision.policy_digest == policy.digest


def test_external_writes_wait_thirty_minutes_then_deny(policy: LoadedPolicy) -> None:
    decision = evaluate_request(
        HttpRequest("POST", "https://example.org/api"), policy, ceiling=CEILING
    )
    assert decision.escalation is not None
    assert decision.escalation.timeout == timedelta(minutes=30)
    assert decision.escalation.on_timeout is Action.DENY


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (PackageOperation("conda", "sagelib", channel="conda-forge"), Action.ALLOW),
        (PackageOperation("conda", "something", channel="bioconda"), Action.DENY),
        (PackageOperation("pypi", "Lattice_Estimator"), Action.ALLOW),
    ],
)
def test_example_packages(
    policy: LoadedPolicy, operation: PackageOperation, expected: Action
) -> None:
    assert evaluate(operation, policy, ceiling=CEILING).action is expected


@pytest.mark.parametrize(
    ("message", "origin", "expected"),
    [
        (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            "orchestrator",
            Action.ALLOW,
        ),
        ({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "worker", Action.ALLOW),
        (
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": PREFIX + "paper-search-mcp_arxiv_search", "arguments": {}},
            },
            "worker",
            Action.ALLOW,
        ),
        (
            {"jsonrpc": "2.0", "id": 4, "method": "x-vendor/reconfigure"},
            "orchestrator",
            Action.DENY,
        ),
        ([{"jsonrpc": "2.0", "id": 5, "method": "ping"}], "orchestrator", Action.DENY),
    ],
)
def test_example_mcp_messages(
    policy: LoadedPolicy, message: object, origin: str, expected: Action
) -> None:
    assert origin in ("orchestrator", "worker")
    decision = evaluate_mcp_message(
        "crypto-research-mcp",
        message,
        policy,
        origin="worker" if origin == "worker" else "orchestrator",
        ceiling=CEILING,
    )
    assert decision.action is expected, decision.reason


def test_example_large_egress_monitor(policy: LoadedPolicy) -> None:
    threshold = 500 * 1024**2
    assert evaluate_monitors(policy, EgressUsage(threshold)) == ()
    alerts = evaluate_monitors(policy, EgressUsage(threshold + 1))
    assert [alert.rule for alert in alerts] == ["large-egress"]


def test_example_resources_are_capped_by_the_ceiling(policy: LoadedPolicy) -> None:
    ceiling = PoolCeiling.model_validate({"resources": {"memory": "32Gi", "cpu": 32}})
    resources, notices = effective_resources(policy.policy, ceiling)
    assert resources.memory == 32 * 1024**3
    assert resources.cpu == 16
    assert resources.wall_time == timedelta(hours=72)
    assert len(notices) == 1
    assert "memory" in notices[0]
