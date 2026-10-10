# SPDX-License-Identifier: MIT
"""The OpenShell compiler: output shape, findings, and soundness against the library."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import pytest
import yaml

from sandbox_science.policy import (
    Action,
    LoadedPolicy,
    McpOperation,
    PolicyLoadError,
    PoolCeiling,
    evaluate,
    load_policy,
    parse_policy,
)
from sandbox_science.policy.openshell import (
    AnyOf,
    Compiled,
    McpProxyTarget,
    OpenShellPolicy,
    SandboxTarget,
    Severity,
    compile_mcp_proxy,
    compile_sandbox,
    load_targets,
    parse_targets,
)

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = load_policy(ROOT / "examples" / "policy.yaml")
TARGETS = load_targets(ROOT / "examples" / "openshell" / "targets.yaml")
FIXTURE = Path(__file__).parent / "fixtures" / "crypto-research-mcp-tools.json"
OPEN = PoolCeiling.unrestricted()
HEADER = "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: test}\n"

# Fields of OpenShell's authored schema (v0.1.3) that the compiler may emit. OpenShell
# rejects unknown fields, so a typo here would break policy loads.
ALLOWED_KEYS = {
    "version",
    "network_policies",
    "name",
    "endpoints",
    "binaries",
    "path",
    "host",
    "ports",
    "protocol",
    "enforcement",
    "rules",
    "deny_rules",
    "mcp",
    "credential_binding",
    "allow",
    "method",
    "tool",
    "any",
    "versions",
    "strict_tool_names",
    "max_body_bytes",
    "provider",
}


def schema_keys(node: object, *, names_level: bool = False) -> set[str]:
    """Field names used anywhere in a document; network policy names are not fields."""
    if isinstance(node, dict):
        keys: set[str] = set()
        for key, value in node.items():
            if not names_level:
                keys.add(key)
            keys |= schema_keys(value, names_level=key == "network_policies")
        return keys
    if isinstance(node, list):
        return set().union(*(schema_keys(item) for item in node))
    return set()


def leaf_values(node: object) -> list[object]:
    if isinstance(node, dict):
        return [value for item in node.values() for value in leaf_values(item)]
    if isinstance(node, list):
        return [value for item in node for value in leaf_values(item)]
    return [node]


def mcp_policy(tools: str, *, prefix: str = "", default: str = "escalate") -> LoadedPolicy:
    prefix_line = f"      tool_prefix: {prefix}\n" if prefix else ""
    return parse_policy(
        HEADER
        + f"defaults: {{action: {default}}}\n"
        + "mcp:\n  servers:\n    srv:\n      url: https://mcp.example.org/mcp\n"
        + "      revision: '1'\n      auth: {mode: none}\n"
        + prefix_line
        + f"      tools: {tools}\n"
    )


def compile_mcp(policy: LoadedPolicy) -> Compiled:
    return compile_mcp_proxy(policy, TARGETS.mcp_proxy, ceiling=OPEN)


def openshell_glob(pattern: str, name: str) -> bool:
    """OpenShell tool globs: '.' separates, '*' stays within it, '**' matches anything."""
    if pattern == "**":
        return True
    regex = "".join(
        "[^.]*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", pattern)
    )
    return re.fullmatch(regex, name) is not None


def openshell_admits(policy: OpenShellPolicy, server: str, tool: str) -> bool:
    """Simulate OpenShell's decision for tools/call: some allow matches and no deny does."""
    entry = policy.network_policies.get(f"mcp-{server}")
    if entry is None:
        return False
    endpoint = entry.endpoints[0]

    def matches(matcher: str | AnyOf | None) -> bool:
        if isinstance(matcher, AnyOf):
            return any(openshell_glob(name, tool) for name in matcher.any)
        return matcher is not None and openshell_glob(matcher, tool)

    allowed = any(
        rule.allow.method == "tools/call" and matches(rule.allow.tool)
        for rule in endpoint.rules or ()
    )
    denied = any(
        deny.method == "tools/call" and matches(deny.tool) for deny in endpoint.deny_rules or ()
    )
    return allowed and not denied


def library_allows(policy: LoadedPolicy, server: str, tool: str) -> bool:
    decision = evaluate(McpOperation(server, "tools/call", tool), policy, ceiling=OPEN)
    return decision.action in (Action.ALLOW, Action.ALERT)


# Research sandbox fence.


def test_example_sandbox_fence() -> None:
    compiled = compile_sandbox(EXAMPLE, TARGETS.sandbox)
    entries = compiled.policy.network_policies
    assert list(entries) == [
        "sandbox-science-egress-proxy",
        "sandbox-science-package-mirror",
        "sandbox-science-mcp-proxy",
    ]
    hosts = {key: entry.endpoints[0].host for key, entry in entries.items()}
    assert hosts["sandbox-science-egress-proxy"] == "egress-proxy.sandbox-science.internal"
    for entry in entries.values():
        (endpoint,) = entry.endpoints
        assert endpoint.protocol == "tcp"
        assert endpoint.enforcement is None
        assert endpoint.rules is None
        assert [binary.path for binary in entry.binaries] == ["/**"]
    assert (
        compiled.environment["HTTPS_PROXY"] == "http://egress-proxy.sandbox-science.internal:3128"
    )
    assert compiled.environment["NO_PROXY"] == (
        "mirror.sandbox-science.internal,mcp-proxy.sandbox-science.internal"
    )
    assert compiled.policy_digest == EXAMPLE.digest


def test_every_project_network_rule_is_reported_as_proxy_enforced() -> None:
    compiled = compile_sandbox(EXAMPLE, TARGETS.sandbox)
    subjects = {finding.subject for finding in compiled.findings}
    for rule in EXAMPLE.policy.network.rules:
        assert f"network.rules.{rule.name}" in subjects
    warnings = [finding for finding in compiled.findings if finding.severity is Severity.WARNING]
    assert [finding.subject for finding in warnings] == ["credentials.providers.github-readonly"]


def test_without_worker_access_the_sandbox_cannot_reach_the_mcp_proxy() -> None:
    text = (ROOT / "examples" / "policy.yaml").read_text()
    policy = parse_policy(text.replace("worker_access: true", "worker_access: false"))
    compiled = compile_sandbox(policy, TARGETS.sandbox)
    assert "sandbox-science-mcp-proxy" not in compiled.policy.network_policies
    assert compiled.environment["NO_PROXY"] == "mirror.sandbox-science.internal"
    assert any(finding.subject == "mcp.proxy.worker_access" for finding in compiled.findings)


# MCP proxy sandbox.


def test_example_mcp_proxy_policy() -> None:
    compiled = compile_mcp(EXAMPLE)
    entry = compiled.policy.network_policies["mcp-crypto-research-mcp"]
    (endpoint,) = entry.endpoints
    assert (endpoint.host, endpoint.ports, endpoint.path) == (
        "mcp.example.invalid",
        (443,),
        "/crypto-research",
    )
    assert endpoint.protocol == "mcp"
    assert endpoint.enforcement == "enforce"
    assert endpoint.deny_rules is None
    assert endpoint.mcp is not None
    assert endpoint.mcp.versions == ("2025-11-25",)
    assert endpoint.credential_binding is not None
    assert endpoint.credential_binding.provider == "crypto-research-mcp"
    tool_rules = [rule for rule in endpoint.rules or () if rule.allow.method == "tools/call"]
    assert len(tool_rules) == 1
    names = tool_rules[0].allow.tool
    assert isinstance(names, AnyOf)
    assert len(names.any) == 62
    assert all(name.startswith("crypto_research_mcp-") for name in names.any)
    assert [binary.path for binary in entry.binaries] == ["/opt/sandbox-science/bin/mcp-proxy"]


def test_the_example_fence_matches_the_library_for_every_known_tool() -> None:
    compiled = compile_mcp(EXAMPLE)
    tools = [entry["name"] for entry in json.loads(FIXTURE.read_text())["tools"]]
    made_up = [
        "crypto_research_mcp-github-mcp_delete_repository",
        "crypto_research_mcp-portal_toggle_servers",
        "crypto_research_mcp-e-print-mcp_get_or_create_note",
        "firecrawl-mcp_firecrawl_search",
    ]
    for tool in tools + made_up:
        assert openshell_admits(compiled.policy, "crypto-research-mcp", tool) == library_allows(
            EXAMPLE, "crypto-research-mcp", tool
        ), tool


@pytest.mark.parametrize(
    "tools",
    [
        "{allow: ['*_search'], escalate: ['*_feedback'], deny: ['portal_*']}",
        "{allow: ['*'], deny: ['admin_*', 'reset']}",
        "{allow: ['*', 'portal_list'], deny: ['portal_*']}",
        "{allow: ['a_*', 'b_get'], escalate: ['*'], deny: []}",
        "{allow: ['x_*_y', 'q'], escalate: ['x_secret_*'], deny: ['*_y']}",
        "{allow: [], escalate: [], deny: []}",
        "{allow: ['x.*', 'a_*', 'q.r'], deny: ['*_secret', 'x.b*']}",
        "{allow: ['**'], escalate: ['a**b']}",
    ],
)
@pytest.mark.parametrize("prefix", ["", "portal-", "srv.v1-"])
@pytest.mark.parametrize("default", ["escalate", "allow", "deny"])
def test_compiled_tool_rules_are_never_looser_than_the_library(
    tools: str, prefix: str, default: str
) -> None:
    policy = mcp_policy(tools, prefix=prefix, default=default)
    compiled = compile_mcp(policy)
    rng = random.Random(f"{tools}{prefix}{default}")  # noqa: S311 - test data, not secrets
    alphabet = [
        "a_",
        "b_",
        "x_",
        "_y",
        "q",
        "get",
        "search",
        "_feedback",
        "portal_",
        "list",
        "admin_",
        "reset",
        "secret_",
        ".",
    ]
    names = {"".join(rng.choice(alphabet) for _ in range(rng.randint(1, 5))) for _ in range(2000)}
    names |= {"portal_list", "b_get", "q", "reset", "admin_x", "x_secret_y", "a_b"}
    for bare in sorted(names):
        tool = prefix + bare
        if openshell_admits(compiled.policy, "srv", tool):
            assert library_allows(policy, "srv", tool), tool


def test_deny_entries_are_compiled_only_with_allow_patterns() -> None:
    exact_only = compile_mcp(mcp_policy("{allow: [a, b], deny: ['portal_*']}"))
    assert exact_only.policy.network_policies["mcp-srv"].endpoints[0].deny_rules is None
    patterns = compile_mcp(mcp_policy("{allow: ['*_search'], deny: ['portal_*']}"))
    deny_rules = patterns.policy.network_policies["mcp-srv"].endpoints[0].deny_rules
    assert deny_rules is not None
    assert [deny.tool for deny in deny_rules] == ["portal_*"]


def test_an_exact_allow_under_a_compiled_deny_pattern_is_reported_as_stricter() -> None:
    compiled = compile_mcp(mcp_policy("{allow: ['*', portal_list], deny: ['portal_*']}"))
    stricter = [
        finding for finding in compiled.findings if "portal_list is allowed" in finding.message
    ]
    assert stricter
    assert stricter[0].severity is Severity.WARNING


def test_a_lone_star_allow_becomes_openshells_any_value_glob() -> None:
    compiled = compile_mcp(mcp_policy("{allow: ['*'], deny: [reset]}"))
    rules = compiled.policy.network_policies["mcp-srv"].endpoints[0].rules or ()
    assert [rule.allow.tool for rule in rules if rule.allow.method == "tools/call"] == ["**"]


def test_a_star_block_drops_allow_patterns() -> None:
    compiled = compile_mcp(mcp_policy("{allow: ['a_*', b_get], escalate: ['*']}"))
    endpoint = compiled.policy.network_policies["mcp-srv"].endpoints[0]
    tools = [rule.allow.tool for rule in endpoint.rules or () if rule.allow.method == "tools/call"]
    assert tools == [AnyOf(any=("b_get",))]
    assert endpoint.deny_rules is None


def test_a_permissive_default_is_reported_as_stricter_at_the_fence() -> None:
    compiled = compile_mcp(mcp_policy("{allow: [a]}", default="allow"))
    assert any(
        finding.severity is Severity.WARNING and "defaults.action is allow" in finding.message
        for finding in compiled.findings
    )


def test_servers_on_the_floor_or_outside_the_ceiling_are_not_compiled() -> None:
    policy = parse_policy(
        HEADER
        + "mcp:\n  servers:\n"
        + "    meta: {url: 'https://metadata.google.internal/x', revision: '1',"
        + " auth: {mode: none}}\n"
        + "    kept: {url: 'https://mcp.example.org/x', revision: '1', auth: {mode: none}}\n"
        + "    other: {url: 'https://other.example.org/x', revision: '1', auth: {mode: none}}\n"
    )
    ceiling = PoolCeiling.model_validate({"mcp_servers": ["kept", "meta"]})
    compiled = compile_mcp_proxy(policy, TARGETS.mcp_proxy, ceiling=ceiling)
    assert list(compiled.policy.network_policies) == ["mcp-kept"]
    skipped = {
        finding.subject for finding in compiled.findings if "not compiled" in finding.message
    }
    assert skipped == {"mcp.servers.meta", "mcp.servers.other"}


@pytest.mark.parametrize(
    ("headers", "bound"),
    [
        ("{X-Api-Key: '${provider:one.KEY}', X-Account: research}", "one"),
        ("{X-Api-Key: '${provider:one.KEY}', X-Other-Key: '${provider:two.KEY}'}", None),
    ],
)
def test_header_credentials_bind_a_single_provider(headers: str, bound: str | None) -> None:
    policy = parse_policy(
        HEADER
        + "mcp:\n  servers:\n    srv:\n      url: https://mcp.example.org/mcp\n"
        + f"      revision: '1'\n      auth: {{mode: headers, headers: {headers}}}\n"
    )
    endpoint = compile_mcp(policy).policy.network_policies["mcp-srv"].endpoints[0]
    binding = endpoint.credential_binding
    assert (binding.provider if binding else None) == bound


def test_older_mcp_revisions_are_reported() -> None:
    target = McpProxyTarget(binaries=("/bin/proxy",), versions=("2025-03-26", "2025-11-25"))
    compiled = compile_mcp_proxy(EXAMPLE, target, ceiling=OPEN)
    assert any(finding.subject == "versions" for finding in compiled.findings)
    endpoint = compiled.policy.network_policies["mcp-crypto-research-mcp"].endpoints[0]
    assert endpoint.mcp is not None
    assert endpoint.mcp.versions == ("2025-03-26", "2025-11-25")


# Output shape.


@pytest.mark.parametrize("which", ["sandbox", "mcp"])
def test_output_uses_only_known_openshell_fields_and_no_nulls(which: str) -> None:
    compiled = (
        compile_sandbox(EXAMPLE, TARGETS.sandbox) if which == "sandbox" else compile_mcp(EXAMPLE)
    )
    document = yaml.safe_load(compiled.policy.to_yaml())
    assert document["version"] == 1
    keys = schema_keys(document)
    assert keys <= ALLOWED_KEYS, keys - ALLOWED_KEYS
    assert None not in leaf_values(document)
    for entry in document["network_policies"].values():
        assert entry["binaries"]
        for endpoint in entry["endpoints"]:
            assert endpoint["ports"]
            assert "port" not in endpoint
            assert "tls" not in endpoint
            if endpoint["protocol"] == "mcp":
                assert endpoint["enforcement"] == "enforce"


def test_output_is_deterministic() -> None:
    first = compile_mcp(EXAMPLE).policy.to_yaml()
    assert compile_mcp(load_policy(ROOT / "examples" / "policy.yaml")).policy.to_yaml() == first


def test_rendered_examples_are_current() -> None:
    header = (
        "# Generated by scripts/render_openshell_examples.py from examples/policy.yaml\n"
        "# and examples/openshell/targets.yaml. Do not edit by hand.\n"
    )
    expected = {
        "research-sandbox.yaml": compile_sandbox(EXAMPLE, TARGETS.sandbox).policy.to_yaml(),
        "mcp-proxy.yaml": compile_mcp(EXAMPLE).policy.to_yaml(),
    }
    for name, text in expected.items():
        current = (ROOT / "examples" / "openshell" / name).read_text()
        assert current == header + text, (
            f"examples/openshell/{name} is stale; run scripts/render_openshell_examples.py"
        )


# Targets.


def test_example_targets_parse() -> None:
    assert TARGETS.sandbox.egress_proxy.port == 3128
    assert TARGETS.mcp_proxy.versions == ("2025-11-25",)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("egress-proxy.sandbox-science.internal", "10.0.0.5"),
        ("egress-proxy.sandbox-science.internal", "localhost"),
        ("port: 3128", "port: 6443"),
        ("binaries: [/**]", "binaries: [bin/sh]"),
        ('versions: ["2025-11-25"]', 'versions: ["2024-11-05"]'),
        ("binaries: [/opt/sandbox-science/bin/mcp-proxy]", "binaries: []"),
    ],
)
def test_invalid_targets_are_rejected(change: str, message: str) -> None:
    text = (ROOT / "examples" / "openshell" / "targets.yaml").read_text()
    assert change in text
    with pytest.raises(PolicyLoadError):
        parse_targets(text.replace(change, message, 1))


def test_sandbox_target_defaults_to_all_binaries() -> None:
    target = SandboxTarget.model_validate(
        {
            "egress_proxy": {"host": "egress.example.org", "port": 3128},
            "package_mirror": {"host": "mirror.example.org", "port": 443},
            "mcp_proxy": {"host": "mcp.example.org", "port": 8080},
        }
    )
    assert target.binaries == ("/**",)


def test_tcp_service_hosts_must_be_names() -> None:
    with pytest.raises(ValueError, match="DNS name"):
        SandboxTarget.model_validate(
            {
                "egress_proxy": {"host": "10.0.0.5", "port": 3128},
                "package_mirror": {"host": "mirror.example.org", "port": 443},
                "mcp_proxy": {"host": "mcp.example.org", "port": 8080},
            }
        )


def test_dotted_allow_patterns_are_dropped_when_deny_patterns_exist() -> None:
    compiled = compile_mcp(mcp_policy("{allow: ['x.*', 'a_*'], deny: ['*_secret']}"))
    endpoint = compiled.policy.network_policies["mcp-srv"].endpoints[0]
    tools = [rule.allow.tool for rule in endpoint.rules or () if rule.allow.method == "tools/call"]
    assert tools == ["a_*"]
    assert any("containing '.'" in finding.message for finding in compiled.findings)


def test_a_lone_star_admits_only_dot_free_names_when_deny_patterns_exist() -> None:
    compiled = compile_mcp(mcp_policy("{allow: ['*'], deny: ['admin_*']}"))
    endpoint = compiled.policy.network_policies["mcp-srv"].endpoints[0]
    tools = [rule.allow.tool for rule in endpoint.rules or () if rule.allow.method == "tools/call"]
    assert tools == ["*"]
    assert not openshell_admits(compiled.policy, "srv", "admin_.")
