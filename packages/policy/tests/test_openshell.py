# SPDX-License-Identifier: MIT
"""The OpenShell compiler: output shape, findings, and soundness against the library."""

from __future__ import annotations

import ipaddress
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
from sandbox_science.policy.hosts import floor_reason, is_public
from sandbox_science.policy.openshell import (
    AnyOf,
    Compiled,
    CompileError,
    McpProxyTarget,
    OpenShellPolicy,
    SandboxTarget,
    ServiceEndpoint,
    Severity,
    compile_mcp_proxy,
    compile_sandbox,
    load_targets,
    parse_targets,
    public_address_ranges,
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
    "access",
    "allowed_ips",
    "network_middlewares",
    "middleware",
    "order",
    "on_error",
    "include",
    "exclude",
    "config",
}


def schema_keys(node: object, *, names_level: bool = False) -> set[str]:
    """Field names used anywhere in a document; entry names and middleware config are not."""
    if isinstance(node, dict):
        keys: set[str] = set()
        for key, value in node.items():
            if not names_level:
                keys.add(key)
            if key == "config":
                continue  # defined and validated by the middleware service
            keys |= schema_keys(
                value, names_level=key in ("network_policies", "network_middlewares")
            )
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


# Research sandbox: admission endpoints plus the policy middleware.


def compile_research(policy: LoadedPolicy, **ceiling: object) -> Compiled:
    pool = PoolCeiling.model_validate(ceiling) if ceiling else OPEN
    return compile_sandbox(policy, TARGETS.sandbox, ceiling=pool)


def network_policy(network: str, default: str = "escalate") -> LoadedPolicy:
    return parse_policy(HEADER + f"defaults: {{action: {default}}}\n" + network)


def test_example_sandbox_policy() -> None:
    compiled = compile_research(EXAMPLE)
    entries = compiled.policy.network_policies
    assert list(entries) == [
        "sandbox-science-egress",
        "sandbox-science-package-mirror",
        "sandbox-science-mcp-proxy",
    ]
    public, *named = entries["sandbox-science-egress"].endpoints
    assert public.host is None
    assert (public.protocol, public.enforcement, public.access) == ("rest", "enforce", "full")
    assert public.ports == (80, 443)
    assert public.allowed_ips == public_address_ranges()
    # The git-fetch rule names its hosts, so the sandbox can resolve them.
    assert [endpoint.host for endpoint in named] == ["codeberg.org", "github.com", "gitlab.com"]
    for endpoint in named:
        assert endpoint.model_dump(exclude={"host"}) == public.model_dump(exclude={"host"})
    for key in ("sandbox-science-package-mirror", "sandbox-science-mcp-proxy"):
        (service,) = entries[key].endpoints
        assert service.protocol == "tcp"
        assert service.allowed_ips is None
    middlewares = compiled.policy.network_middlewares
    assert middlewares is not None
    middleware = middlewares["sandbox-science-policy"]
    assert middleware.middleware == "sandbox-science-policy"
    assert middleware.on_error == "fail_closed"
    assert middleware.endpoints.include == ("**",)
    assert middleware.endpoints.exclude == (
        "mcp-proxy.sandbox-science.internal",
        "mirror.sandbox-science.internal",
    )
    assert middleware.config == {"policy": {"name": "permissive-default", "digest": EXAMPLE.digest}}
    assert compiled.policy_digest == EXAMPLE.digest


def test_public_ranges_hold_only_addresses_the_library_calls_public() -> None:
    rng = random.Random(7)  # noqa: S311 - test data, not secrets
    for block in public_address_ranges():
        network = ipaddress.ip_network(block)
        samples = [network.network_address, network.broadcast_address]
        samples += [
            network.network_address + rng.randrange(network.num_addresses) for _ in range(20)
        ]
        for address in samples:
            assert is_public(str(address)), (block, str(address))
            assert floor_reason(str(address)) is None, (block, str(address))


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "140.82.112.3", "2606:4700:4700::1111"])
def test_public_ranges_cover_ordinary_public_addresses(address: str) -> None:
    target = ipaddress.ip_address(address)
    assert any(target in ipaddress.ip_network(block) for block in public_address_ranges())


def test_a_closed_policy_admits_only_the_hosts_it_names() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: api, operation: http, match: {hosts: [api.example.org], ports: [8443]}, "
        "action: allow}\n"
        "    - {name: lan, operation: http, match: {hosts: [10.0.0.5]}, action: escalate}\n"
        "    - {name: blocked, operation: http, match: {hosts: [bad.example.org]}, action: deny}\n",
        default="deny",
    )
    endpoints = compile_research(policy).policy.network_policies["sandbox-science-egress"].endpoints
    assert [endpoint.host for endpoint in endpoints] == ["10.0.0.5", "api.example.org"]
    assert all(endpoint.allowed_ips is None for endpoint in endpoints)
    assert all(endpoint.ports == (80, 443, 8443) for endpoint in endpoints)


def test_a_policy_admitting_nothing_has_no_egress_endpoints() -> None:
    compiled = compile_research(network_policy("", default="deny"))
    assert "sandbox-science-egress" not in compiled.policy.network_policies
    assert compiled.policy.network_middlewares is not None


def test_lan_addresses_join_the_public_endpoint_and_lan_names_are_refused() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: web, operation: http, match: {hosts: ['*'], methods: [GET]}, action: allow}\n"
        "    - {name: lan, operation: http, match: {hosts: [10.0.0.5, nas.local]}, "
        "action: allow}\n"
    )
    compiled = compile_research(policy)
    (public,) = compiled.policy.network_policies["sandbox-science-egress"].endpoints
    assert public.allowed_ips is not None
    assert public.allowed_ips[-1] == "10.0.0.5"
    refused = [finding for finding in compiled.findings if finding.subject == "hosts.nas.local"]
    assert refused
    assert refused[0].severity is Severity.WARNING


def test_named_public_hosts_get_endpoints_beside_the_host_less_one() -> None:
    # OpenShell's policy DNS answers only for hosts an endpoint names, so a
    # sandbox could not resolve these names through the host-less endpoint.
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: web, operation: http, match: {hosts: ['*'], methods: [GET]}, action: allow}\n"
        "    - {name: api, operation: http, match: {hosts: [api.example.org, 93.184.215.14], "
        "methods: [POST]}, action: allow}\n"
        "    - {name: blocked, operation: http, match: {hosts: [bad.example.org]}, action: deny}\n"
    )
    compiled = compile_research(policy)
    public, *named = compiled.policy.network_policies["sandbox-science-egress"].endpoints
    assert public.host is None
    assert [endpoint.host for endpoint in named] == ["api.example.org"]
    (api,) = named
    assert api.allowed_ips == public.allowed_ips
    assert (api.ports, api.protocol, api.enforcement, api.access) == (
        public.ports,
        "rest",
        "enforce",
        "full",
    )
    warnings = [f.message for f in compiled.findings if f.severity is Severity.WARNING]
    assert any("reach only the hosts this policy names" in message for message in warnings)


def test_wildcard_rules_become_openshell_subdomain_wildcards() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: arxiv, operation: http, match: {hosts: ['*.arxiv.org', '*.org', "
        "'*.corp.internal']}, action: allow}\n",
        default="deny",
    )
    compiled = compile_research(policy)
    public, *named = compiled.policy.network_policies["sandbox-science-egress"].endpoints
    assert public.host is None
    # The library's *.suffix matches any depth below the suffix: OpenShell's **.
    assert [endpoint.host for endpoint in named] == ["**.arxiv.org"]
    assert named[0].allowed_ips == public.allowed_ips
    tld = [f for f in compiled.findings if f.subject == "hosts.*.org"]
    assert len(tld) == 1
    assert tld[0].severity is Severity.WARNING
    assert "three labels" in tld[0].message
    # Wildcards never grant non-public names, so none is admitted or reported.
    assert not any("corp.internal" in f.subject for f in compiled.findings)
    # Defaults deny and no rule is for '*': nothing is limited to named hosts.
    assert not any("reach only the hosts" in f.message for f in compiled.findings)


def test_floor_hosts_are_not_named_beside_the_host_less_endpoint() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: web, operation: http, match: {hosts: ['*', metadata.goog, "
        "blocked.example.org]}, action: allow}\n"
    )
    compiled = compile_research(policy, network={"deny_hosts": ["blocked.example.org"]})
    (public,) = compiled.policy.network_policies["sandbox-science-egress"].endpoints
    assert public.host is None


def test_floor_and_ceiling_hosts_are_not_admitted() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: api, operation: http, match: {hosts: [api.example.org, metadata.goog, "
        "blocked.example.org]}, action: allow}\n",
        default="deny",
    )
    compiled = compile_research(policy, network={"deny_hosts": ["blocked.example.org"]})
    endpoints = compiled.policy.network_policies["sandbox-science-egress"].endpoints
    assert [endpoint.host for endpoint in endpoints] == ["api.example.org"]
    subjects = {
        finding.subject for finding in compiled.findings if "not admitted" in finding.message
    }
    assert subjects == {"hosts.metadata.goog", "hosts.blocked.example.org"}


def test_control_plane_ports_are_reported_and_left_out() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: k8s, operation: http, match: {hosts: ['*'], ports: [6443, 9443]}, "
        "action: allow}\n"
    )
    compiled = compile_research(policy)
    (public,) = compiled.policy.network_policies["sandbox-science-egress"].endpoints
    assert public.ports == (80, 443, 9443)
    assert any("control-plane" in finding.message for finding in compiled.findings)


def test_a_service_on_an_admitted_port_is_a_compile_error() -> None:
    target = TARGETS.sandbox.model_copy(
        update={
            "package_mirror": ServiceEndpoint.model_validate(
                {"host": "mirror.internal", "port": 443}
            )
        }
    )
    with pytest.raises(CompileError, match="port 443"):
        compile_sandbox(EXAMPLE, target, ceiling=OPEN)


def test_without_worker_access_no_endpoint_admits_the_mcp_proxy() -> None:
    text = (ROOT / "examples" / "policy.yaml").read_text()
    policy = parse_policy(text.replace("worker_access: true", "worker_access: false"))
    compiled = compile_research(policy)
    assert "sandbox-science-mcp-proxy" not in compiled.policy.network_policies
    middlewares = compiled.policy.network_middlewares
    assert middlewares is not None
    assert middlewares["sandbox-science-policy"].endpoints.exclude == (
        "mirror.sandbox-science.internal",
    )
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
    assert endpoint.mcp is not None
    assert endpoint.mcp.versions == ("2025-11-25",)
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
        "{allow: ['*', 'b_get', 'a_*'], deny: [reset]}",
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
    names |= {"portal_list", "b_get", "q", "reset", "admin_x", "x_secret_y", "a_b", ""}
    for bare in sorted(names):
        tool = prefix + bare
        if not tool:
            continue
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


def test_a_lone_star_allow_becomes_openshells_any_value_glob_alone() -> None:
    compiled = compile_mcp(mcp_policy("{allow: ['*', b_get, 'a_*'], deny: [reset]}"))
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
        finding.subject
        for finding in compiled.findings
        if finding.message.startswith("not compiled:")
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
    assert endpoint.mcp.versions == ("2025-11-25",)  # the server's own revision


# Output shape.


@pytest.mark.parametrize("which", ["sandbox", "mcp"])
def test_output_uses_only_known_openshell_fields_and_no_nulls(which: str) -> None:
    compiled = compile_research(EXAMPLE) if which == "sandbox" else compile_mcp(EXAMPLE)
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
        "research-sandbox.yaml": compile_research(EXAMPLE).policy.to_yaml(),
        "mcp-proxy.yaml": compile_mcp(EXAMPLE).policy.to_yaml(),
    }
    for name, text in expected.items():
        current = (ROOT / "examples" / "openshell" / name).read_text()
        assert current == header + text, (
            f"examples/openshell/{name} is stale; run scripts/render_openshell_examples.py"
        )


# Targets.


def test_example_targets_parse() -> None:
    assert TARGETS.sandbox.middleware == "sandbox-science-policy"
    assert TARGETS.sandbox.package_mirror.port == 8443
    assert TARGETS.mcp_proxy.versions == ("2025-11-25",)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("mirror.sandbox-science.internal", "10.0.0.5"),
        ("mirror.sandbox-science.internal", "localhost"),
        ("port: 8443", "port: 6443"),
        ("middleware: sandbox-science-policy", "middleware: Not A Name"),
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
            "middleware": "sandbox-science-policy",
            "package_mirror": {"host": "mirror.example.org", "port": 443},
            "mcp_proxy": {"host": "mcp.example.org", "port": 8080},
        }
    )
    assert target.binaries == ("/**",)


def test_tcp_service_hosts_must_be_names() -> None:
    with pytest.raises(ValueError, match="DNS name"):
        SandboxTarget.model_validate(
            {
                "middleware": "sandbox-science-policy",
                "package_mirror": {"host": "10.0.0.5", "port": 443},
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


# Compiler review findings.


def test_a_server_at_the_host_root_gets_an_exact_root_selector() -> None:
    policy = parse_policy(
        HEADER
        + "mcp:\n  servers:\n"
        + "    docs: {url: 'https://mcp.example.org/', revision: '1', auth: {mode: none},"
        + " tools: {allow: [delete_repository]}}\n"
        + "    github: {url: 'https://mcp.example.org/github', revision: '1', auth: {mode: none},"
        + " tools: {allow: [get_file]}}\n"
    )
    entries = compile_mcp(policy).policy.network_policies
    assert entries["mcp-docs"].endpoints[0].path == "/"
    assert entries["mcp-github"].endpoints[0].path == "/github"


def test_servers_sharing_an_endpoint_are_a_compile_error() -> None:
    policy = parse_policy(
        HEADER
        + "mcp:\n  servers:\n"
        + "    a: {url: 'https://mcp.example.org/x', revision: '1', auth: {mode: none}}\n"
        + "    b: {url: 'https://mcp.example.org:443/x', revision: '1', auth: {mode: none}}\n"
    )
    with pytest.raises(CompileError, match="share the endpoint"):
        compile_mcp(policy)


def test_servers_on_one_host_with_different_credentials_are_reported() -> None:
    policy = parse_policy(
        HEADER
        + "mcp:\n  servers:\n"
        + "    a: {url: 'https://mcp.example.org/a', revision: '1',"
        + " auth: {mode: bearer, provider: one}}\n"
        + "    b: {url: 'https://mcp.example.org/b', revision: '1',"
        + " auth: {mode: bearer, provider: two}}\n"
    )
    findings = compile_mcp(policy).findings
    assert any("different credentials" in finding.message for finding in findings)


def test_a_prefixed_star_allow_denies_the_bare_prefix() -> None:
    compiled = compile_mcp(mcp_policy("{allow: ['*']}", prefix="portal-"))
    assert not openshell_admits(compiled.policy, "srv", "portal-")
    assert openshell_admits(compiled.policy, "srv", "portal-list")
    assert not library_allows(mcp_policy("{allow: ['*']}", prefix="portal-"), "srv", "portal-")


def test_the_stricter_report_follows_openshells_matching() -> None:
    compiled = compile_mcp(mcp_policy("{allow: ['x.y', 'a_*'], deny: ['x*']}"))
    assert not any(
        "x.y is allowed by exact name" in finding.message for finding in compiled.findings
    )
    assert openshell_admits(compiled.policy, "srv", "x.y")


def test_worker_tools_narrowing_is_reported_as_looser_at_the_fence() -> None:
    text = (ROOT / "examples" / "policy.yaml").read_text()
    policy = parse_policy(
        text.replace("worker_tools: inherit", "worker_tools: [paper-search-mcp_arxiv_search]")
    )
    findings = [
        finding
        for finding in compile_mcp(policy).findings
        if finding.subject == "mcp.proxy.worker_tools"
    ]
    assert findings
    assert findings[0].severity is Severity.WARNING


def test_literal_headers_need_no_credential_binding() -> None:
    policy = parse_policy(
        HEADER
        + "mcp:\n  servers:\n    srv:\n      url: https://mcp.example.org/mcp\n"
        + "      revision: '1'\n      auth: {mode: headers, headers: {X-Account: research}}\n"
    )
    compiled = compile_mcp(policy)
    assert compiled.policy.network_policies["mcp-srv"].endpoints[0].credential_binding is None
    assert not [
        finding for finding in compiled.findings if finding.subject == "mcp.servers.srv.auth"
    ]


@pytest.mark.parametrize(
    ("tools", "prefix"),
    [
        ("{allow: [repos/read_file]}", ""),
        ("{allow: [read_file]}", "github/"),
        ("{allow: [" + "a" * 120 + "]}", "p" * 20),
    ],
)
def test_names_openshell_refuses_are_reported(tools: str, prefix: str) -> None:
    findings = compile_mcp(mcp_policy(tools, prefix=prefix)).findings
    assert any("does not fit OpenShell's tool names" in finding.message for finding in findings)


@pytest.mark.parametrize(
    ("revision", "versions", "severity"),
    [("2025-06-18", ("2025-06-18",), Severity.WARNING), ("v1.4", ("2025-11-25",), Severity.INFO)],
)
def test_a_server_revision_limits_its_mcp_versions(
    revision: str, versions: tuple[str, ...], severity: Severity
) -> None:
    policy = parse_policy(
        HEADER
        + "mcp:\n  servers:\n    srv:\n      url: https://mcp.example.org/mcp\n"
        + f"      revision: '{revision}'\n      auth: {{mode: none}}\n"
    )
    compiled = compile_mcp(policy)
    endpoint = compiled.policy.network_policies["mcp-srv"].endpoints[0]
    assert endpoint.mcp is not None
    assert endpoint.mcp.versions == versions
    revision_findings = [f for f in compiled.findings if f.subject == "mcp.servers.srv.revision"]
    assert [finding.severity for finding in revision_findings] == [severity]


@pytest.mark.parametrize("name", ["1e3", "0o17", "true", "null", "1.5", "-2", ".inf", "0x1F"])
def test_strings_a_yaml_12_parser_could_misread_are_quoted(name: str) -> None:
    compiled = compile_mcp(mcp_policy(f"{{allow: ['{name}']}}"))
    text = compiled.policy.to_yaml()
    assert f'"{name}"' in text
    assert yaml.safe_load(text)["network_policies"]["mcp-srv"]["endpoints"][0]["rules"][-1][
        "allow"
    ]["tool"]["any"] == [name]


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example.org/x/**",
        "https://mcp.example.org/mcp*",
        "https://mcp.example.org//mcp",
        "https://mcp.example.org/a|b",
        "https://mcp.example.org/m[a-z]p",
        "https://mcp.example.org:0/mcp",
    ],
)
def test_mcp_urls_must_be_plain(url: str) -> None:
    with pytest.raises(PolicyLoadError, match="MCP server URL"):
        parse_policy(
            HEADER
            + "mcp:\n  servers:\n"
            + f"    srv: {{url: '{url}', revision: '1', auth: {{mode: none}}}}\n"
        )
