# SPDX-License-Identifier: MIT
"""The OpenShell compiler: output shape, findings, and soundness against the library."""

from __future__ import annotations

import ipaddress
import itertools
import json
import random
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sandbox_science.policy import (
    Action,
    HardFloor,
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
    BASELINE_READ_ONLY,
    BASELINE_READ_WRITE,
    BASELINE_UNLISTED,
    HARD_LANDLOCK,
    MAX_FILESYSTEM_PATHS,
    SANDBOX_DEVICES_READ_ONLY,
    SANDBOX_DEVICES_READ_WRITE,
    AnyOf,
    Binary,
    Compiled,
    CompileError,
    Endpoint,
    FilesystemPolicy,
    LandlockPolicy,
    McpProxyTarget,
    OpenShellPolicy,
    OrchestratorTarget,
    SandboxFilesystem,
    SandboxTarget,
    ServiceEndpoint,
    Severity,
    admission_entry,
    admission_ranges,
    admission_refusal,
    admitted_ports,
    compile_mcp_proxy,
    compile_orchestrator,
    compile_sandbox,
    load_targets,
    mcp_server_ranges,
    parse_targets,
    public_address_ranges,
)

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = load_policy(ROOT / "examples" / "policy.yaml")
EXAMPLE_TEXT = (ROOT / "examples" / "policy.yaml").read_text()
TARGETS = load_targets(ROOT / "examples" / "openshell" / "targets.yaml")
FIXTURE = Path(__file__).parent / "fixtures" / "crypto-research-mcp-tools.json"
OPEN = PoolCeiling.unrestricted()
RANGES = admission_ranges(EXAMPLE, OPEN)
"""The public ranges less the floor's public metadata address, as compiled for EXAMPLE."""
HEADER = "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: test}\n"

# Fields of OpenShell's authored schema (v0.1.5) that the compiler may emit. OpenShell
# rejects unknown fields, so a typo here would break policy loads.
ALLOWED_KEYS = {
    "version",
    "filesystem_policy",
    "include_workdir",
    "read_only",
    "read_write",
    "landlock",
    "compatibility",
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
    "allow_encoded_slash",
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


def compile_mcp(policy: LoadedPolicy, *, admit_escalated: bool = False) -> Compiled:
    return compile_mcp_proxy(
        policy, TARGETS.mcp_proxy, ceiling=OPEN, admit_escalated=admit_escalated
    )


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


def library_allows_or_escalates_by_name(policy: LoadedPolicy, server: str, tool: str) -> bool:
    """Allowed, or escalated by an escalate entry (not by the default action)."""
    decision = evaluate(McpOperation(server, "tools/call", tool), policy, ceiling=OPEN)
    named = decision.action is Action.ESCALATE and ".tools.escalate[" in (decision.rule or "")
    return decision.action in (Action.ALLOW, Action.ALERT) or named


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
        "sandbox-science-package-cache",
        "sandbox-science-mcp-proxy",
        "sandbox-science-credentials",
    ]
    named = entries["sandbox-science-egress"].endpoints
    # The source-host and research-host rules name their hosts, so the sandbox can
    # resolve them; the '*' rules reach other hosts once the worker admits them on demand.
    hosts = [endpoint.host for endpoint in named]
    assert hosts == sorted(
        pattern.value
        for rule in EXAMPLE.policy.network.rules
        if rule.name in ("source-hosts", "research-hosts")
        for pattern in rule.match.hosts
    )
    assert {"github.com", "gitlab.inria.fr", "eprint.iacr.org", "api.openalex.org"} <= set(hosts)
    for endpoint in named:
        assert (endpoint.protocol, endpoint.enforcement, endpoint.access) == (
            "rest",
            "enforce",
            "full",
        )
        assert endpoint.allowed_ips == RANGES
        # github.com's HTTPS traffic belongs to its credential endpoint.
        assert endpoint.ports == ((80,) if endpoint.host == "github.com" else (80, 443))
    credentials = entries["sandbox-science-credentials"]
    assert credentials.binaries == entries["sandbox-science-egress"].binaries
    assert [endpoint.host for endpoint in credentials.endpoints] == ["api.github.com", "github.com"]
    for endpoint in credentials.endpoints:
        assert (endpoint.ports, endpoint.path, endpoint.protocol) == ((443,), "/**", "rest")
        assert (endpoint.enforcement, endpoint.access) == ("enforce", "full")
        assert endpoint.allowed_ips == RANGES
        assert endpoint.credential_binding is not None
        assert endpoint.credential_binding.provider == "github-readonly"
    (cache,) = entries["sandbox-science-package-cache"].endpoints
    assert (cache.host, cache.ports) == ("package-cache.sandbox-science.internal", (8090,))
    assert (cache.protocol, cache.enforcement, cache.access) == ("rest", "enforce", "read-only")
    assert cache.allow_encoded_slash is True
    assert cache.allowed_ips is None
    (proxy,) = entries["sandbox-science-mcp-proxy"].endpoints
    assert (proxy.host, proxy.ports) == ("mcp-proxy.sandbox-science.internal", (8080,))
    assert (proxy.protocol, proxy.enforcement, proxy.access) == ("rest", "enforce", "full")
    assert proxy.allowed_ips is None
    middlewares = compiled.policy.network_middlewares
    assert middlewares is not None
    middleware = middlewares["sandbox-science-policy"]
    assert middleware.middleware == "sandbox-science-policy"
    assert middleware.on_error == "fail_closed"
    assert middleware.endpoints.include == ("**",)
    # Nothing is excluded: OpenShell selects middleware by host alone.
    assert middleware.endpoints.exclude is None
    assert middleware.config == {
        "policy": {"name": "permissive-default", "digest": EXAMPLE.digest},
        "package_cache": {"host": "package-cache.sandbox-science.internal", "port": 8090},
        "mcp_proxy": {"host": "mcp-proxy.sandbox-science.internal", "port": 8080},
    }
    assert compiled.policy_digest == EXAMPLE.digest


def test_source_hosts_are_not_restricted_by_program() -> None:
    # D97's binary attribution is not compiled (docs/POLICY.md, "Code egress by binary"):
    # OpenShell's binaries only admit, match ancestors (every research process descends
    # from the executor's Python), and cannot tell pip from python. Every program may
    # reach the source hosts, as the middleware decides each request by the library.
    compiled = compile_research(EXAMPLE)
    entries = compiled.policy.network_policies
    egress = entries["sandbox-science-egress"]
    assert egress.binaries == (Binary(path="/**"),)
    source = {"github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "ftp.gnu.org"}
    assert source <= {endpoint.host for endpoint in egress.endpoints}
    assert all(entry.binaries == (Binary(path="/**"),) for entry in entries.values())


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


def test_lan_hosts_a_rule_names_get_endpoints_without_the_public_ranges() -> None:
    # The library lets a rule naming a host exactly reach it at a private address.
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: web, operation: http, match: {hosts: ['*'], methods: [GET]}, action: allow}\n"
        "    - {name: lan, operation: http, match: {hosts: [10.0.0.5, nas.local, "
        "93.184.215.14]}, action: allow}\n"
    )
    compiled = compile_research(policy)
    endpoints = compiled.policy.network_policies["sandbox-science-egress"].endpoints
    ranges = {endpoint.host: endpoint.allowed_ips for endpoint in endpoints}
    assert ranges == {
        "10.0.0.5": None,
        "93.184.215.14": RANGES,
        "nas.local": None,
    }
    assert not any(finding.subject.startswith("hosts.") for finding in compiled.findings)


def test_named_public_hosts_get_endpoints_limited_to_public_ranges() -> None:
    # OpenShell's policy DNS answers only for hosts an endpoint names, so every host
    # a rule names gets one; '*' reaches other hosts through admission on demand.
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: web, operation: http, match: {hosts: ['*'], methods: [GET]}, action: allow}\n"
        "    - {name: api, operation: http, match: {hosts: [api.example.org, 93.184.215.14], "
        "methods: [POST]}, action: allow}\n"
        "    - {name: blocked, operation: http, match: {hosts: [bad.example.org]}, action: deny}\n"
    )
    compiled = compile_research(policy)
    named = compiled.policy.network_policies["sandbox-science-egress"].endpoints
    assert [endpoint.host for endpoint in named] == ["93.184.215.14", "api.example.org"]
    for endpoint in named:
        assert endpoint.allowed_ips == RANGES
        assert (endpoint.ports, endpoint.protocol, endpoint.enforcement, endpoint.access) == (
            (80, 443),
            "rest",
            "enforce",
            "full",
        )
    warnings = [f.message for f in compiled.findings if f.severity is Severity.WARNING]
    assert any("admitted them on demand" in message for message in warnings)


def test_wildcard_rules_become_openshell_subdomain_wildcards() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: arxiv, operation: http, match: {hosts: ['*.arxiv.org', '*.org', "
        "'*.corp.internal']}, action: allow}\n",
        default="deny",
    )
    compiled = compile_research(policy)
    named = compiled.policy.network_policies["sandbox-science-egress"].endpoints
    # The library's *.suffix matches any depth below the suffix: OpenShell's **.
    assert [endpoint.host for endpoint in named] == ["**.arxiv.org"]
    assert named[0].allowed_ips == RANGES
    tld = [f for f in compiled.findings if f.subject == "hosts.*.org"]
    assert len(tld) == 1
    assert tld[0].severity is Severity.WARNING
    assert "three labels" in tld[0].message
    # Wildcards never grant non-public names, so none is admitted or reported.
    assert not any("corp.internal" in f.subject for f in compiled.findings)
    # Defaults deny and no rule is for '*': nothing waits for admission on demand.
    assert not any("on demand" in f.message for f in compiled.findings)


READ_ANYWHERE = (
    "network:\n  rules:\n"
    "    - {name: web, operation: http, match: {hosts: ['*'], methods: [GET]}, action: allow}\n"
)


def test_hosts_admitted_on_demand_get_endpoints_like_named_hosts() -> None:
    compiled = compile_sandbox(
        network_policy(READ_ANYWHERE, default="deny"),
        TARGETS.sandbox,
        ceiling=OPEN,
        admitted=["papers.example.net", "API.Example.org", "papers.example.net"],
    )
    assert "sandbox-science-egress" not in compiled.policy.network_policies
    admitted = compiled.policy.network_policies["sandbox-science-admitted"].endpoints
    assert [endpoint.host for endpoint in admitted] == ["api.example.org", "papers.example.net"]
    for endpoint in admitted:
        # The ranges keep names that resolve to private addresses out.
        assert endpoint.allowed_ips == RANGES
        assert (endpoint.ports, endpoint.protocol, endpoint.enforcement, endpoint.access) == (
            (80, 443),
            "rest",
            "enforce",
            "full",
        )
    assert any("2 hosts admitted on demand" in finding.message for finding in compiled.findings)


READ_ANY_PORT = (
    "network:\n  rules:\n"
    "    - {name: web, operation: http, match: {hosts: ['*'], methods: [GET], ports: any}, "
    "action: allow}\n"
    "    - {name: api, operation: http, match: {hosts: [api.example.org], ports: any}, "
    "action: allow}\n"
)


def test_other_ports_are_admitted_on_demand_under_any_port_rules() -> None:
    # D95: GET on any port is a read. OpenShell endpoints list their ports, so the
    # compiled policy admits the defaults and the worker adds other ports one by one.
    policy = network_policy(READ_ANY_PORT, default="deny")
    assert admitted_ports(policy) == (80, 443)
    assert admission_entry(policy, "Papers.Example.net", 443) == "papers.example.net"
    assert admission_entry(policy, "papers.example.net", 8080) == "papers.example.net:8080"
    assert admission_entry(policy, "2606:4700::1", 8080) == "[2606:4700::1]:8080"
    for host, port in [("papers.example.net", 8080), ("api.example.org", 8443)]:
        assert admission_refusal(policy, TARGETS.sandbox, host, port, ceiling=OPEN) is None
    for port in (6443, 10250):
        refusal = admission_refusal(policy, TARGETS.sandbox, "x.example.net", port, ceiling=OPEN)
        assert f"port {port} is not admitted" in (refusal or "")
    compiled = compile_sandbox(
        policy,
        TARGETS.sandbox,
        ceiling=OPEN,
        admitted=[
            "papers.example.net",
            "papers.example.net:8080",
            "api.example.org:8443",  # a named host, on another port
            "data.example.net:9000",  # another port only
            "[2606:4700::1]:8080",
            "x.example.net:6443",  # a control-plane port
            "printer.local:8080",  # not public
            "pypi.org:8080",  # a registry
            "bad:port:entry",
        ],
    )
    assert compiled.admitted == (
        "[2606:4700::1]:8080",
        "api.example.org:8443",
        "data.example.net:9000",
        "papers.example.net",
        "papers.example.net:8080",
    )
    named = {
        e.host: e for e in compiled.policy.network_policies["sandbox-science-egress"].endpoints
    }
    assert named["api.example.org"].ports == (80, 443, 8443)
    admitted = {
        e.host: e for e in compiled.policy.network_policies["sandbox-science-admitted"].endpoints
    }
    assert admitted["papers.example.net"].ports == (80, 443, 8080)
    assert admitted["data.example.net"].ports == (9000,)
    assert admitted["2606:4700::1"].ports == (8080,)
    assert all(
        endpoint.allowed_ips == admission_ranges(policy, OPEN) for endpoint in admitted.values()
    )
    assert compiled.admits("papers.example.net", 8080)
    assert compiled.admits("data.example.net", 9000)
    assert not compiled.admits("data.example.net", 443)
    assert not compiled.admits("papers.example.net", 8081)
    refused = [f.subject for f in compiled.findings if "not admitted on demand" in f.message]
    assert sorted(refused) == [
        "hosts.bad:port:entry",
        "hosts.printer.local:8080",
        "hosts.pypi.org:8080",
        "hosts.x.example.net:6443",
    ]


def test_other_ports_are_refused_without_an_any_port_rule() -> None:
    policy = network_policy(READ_ANYWHERE, default="deny")
    refusal = admission_refusal(policy, TARGETS.sandbox, "x.example.net", 8080, ceiling=OPEN)
    assert "port 8080 is not admitted" in (refusal or "")
    compiled = compile_sandbox(
        policy, TARGETS.sandbox, ceiling=OPEN, admitted=["x.example.net:8080"]
    )
    assert compiled.admitted == ()
    assert "sandbox-science-admitted" not in compiled.policy.network_policies


def test_a_closed_any_port_policy_admits_other_ports_of_named_hosts_only() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: api, operation: http, match: {hosts: [api.example.org], ports: any}, "
        "action: allow}\n",
        default="deny",
    )
    assert admission_refusal(policy, TARGETS.sandbox, "api.example.org", 8443, ceiling=OPEN) is None
    unnamed = admission_refusal(policy, TARGETS.sandbox, "x.example.org", 8443, ceiling=OPEN)
    assert "allows no request" in (unnamed or "")
    assert "reaches only the hosts it names" in (
        admission_refusal(policy, TARGETS.sandbox, "x.example.org", 443, ceiling=OPEN) or ""
    )
    compiled = compile_sandbox(
        policy,
        TARGETS.sandbox,
        ceiling=OPEN,
        admitted=["api.example.org:8443", "x.example.org:8443"],
    )
    assert compiled.admitted == ("api.example.org:8443",)
    (endpoint,) = compiled.policy.network_policies["sandbox-science-egress"].endpoints
    assert (endpoint.host, endpoint.ports, endpoint.allowed_ips) == (
        "api.example.org",
        (80, 443, 8443),
        None,
    )


def test_hosts_the_policy_cannot_reach_are_not_admitted_on_demand() -> None:
    policy = network_policy(
        READ_ANYWHERE
        + "    - {name: blocked, operation: http, match: {hosts: [blocked.example.org]}, "
        "action: deny}\n",
        default="deny",
    )
    refused = [
        "blocked.example.org",
        "pypi.org",
        "metadata.goog",
        "printer.local",
        "203.0.113.7",
        TARGETS.sandbox.package_cache.host,
        "not a host",
    ]
    compiled = compile_sandbox(policy, TARGETS.sandbox, ceiling=OPEN, admitted=refused)
    assert "sandbox-science-admitted" not in compiled.policy.network_policies
    reported = [f for f in compiled.findings if "not admitted on demand" in f.message]
    assert len(reported) == len(refused)
    assert all(finding.severity is Severity.WARNING for finding in reported)


def test_hosts_the_policy_names_are_not_admitted_twice() -> None:
    policy = network_policy(
        READ_ANYWHERE
        + "    - {name: api, operation: http, match: {hosts: [api.example.org, '*.arxiv.org']}, "
        "action: allow}\n",
        default="deny",
    )
    compiled = compile_sandbox(
        policy, TARGETS.sandbox, ceiling=OPEN, admitted=["api.example.org", "export.arxiv.org"]
    )
    assert "sandbox-science-admitted" not in compiled.policy.network_policies


def test_a_closed_policy_admits_nothing_on_demand() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: api, operation: http, match: {hosts: [api.example.org]}, action: allow}\n",
        default="deny",
    )
    compiled = compile_sandbox(policy, TARGETS.sandbox, ceiling=OPEN, admitted=["x.example.org"])
    assert "sandbox-science-admitted" not in compiled.policy.network_policies
    assert any(
        f.subject == "hosts.x.example.org" and "reaches only the hosts it names" in f.message
        for f in compiled.findings
    )


def test_the_example_policy_admits_unnamed_public_hosts_on_demand() -> None:
    def refusal(host: str, port: int = 443) -> str | None:
        return admission_refusal(EXAMPLE, TARGETS.sandbox, host, port, ceiling=OPEN)

    assert refusal("example.org") is None
    assert refusal("example.org", 80) is None
    assert refusal("EXAMPLE.ORG.") is None
    # D95: GET on any port is a read, so other ports are admitted one by one.
    assert refusal("example.org", 8443) is None
    assert "port 6443 is not admitted" in (refusal("example.org", 6443) or "")
    assert "package registry" in (refusal("pypi.org") or "")
    assert refusal("github.com") is None
    compiled = compile_research(EXAMPLE)
    assert compiled.names("github.com")
    assert compiled.names("GitHub.com.")
    assert compiled.names("eprint.iacr.org")  # pre-admitted
    assert not compiled.names("example.org")
    # Addresses are admitted on demand too, when public.
    assert refusal("93.184.215.14") is None
    assert "not public" in (refusal("203.0.113.7") or "")
    assert "not public" in (refusal("printer.local") or "")
    assert "not a host name" in (refusal("bad host") or "")
    assert refusal("metadata.goog") is not None


def test_admission_ranges_leave_out_addresses_the_library_always_denies() -> None:
    # The middleware sees names only, so a name the sandbox picks (such as
    # 168-63-129-16.sslip.io) must not reach these through allowed_ips.
    policy = parse_policy(
        HEADER + "mcp:\n  servers:\n    tools:\n      url: https://52.1.2.3/mcp\n"
        "      revision: '2025-11-25'\n      auth: {mode: none}\n      tools: {allow: [search]}\n"
    )
    ceiling = PoolCeiling.model_validate({"network": {"deny_hosts": ["1.2.3.4"]}})
    floor = HardFloor(admin_hosts=frozenset({"34.120.1.2", "admin.example.org"}))
    ranges = [ipaddress.ip_network(block) for block in admission_ranges(policy, ceiling, floor)]

    def covered(address: str) -> bool:
        return any(ipaddress.ip_address(address) in network for network in ranges)

    for denied in ("168.63.129.16", "1.2.3.4", "34.120.1.2", "52.1.2.3"):
        assert not covered(denied), denied
    for ordinary in ("8.8.8.8", "1.2.3.5", "168.63.129.17", "2606:4700:4700::1111"):
        assert covered(ordinary), ordinary
    assert not covered("10.0.0.5")


def test_compiled_names_are_exactly_its_endpoints() -> None:
    policy = network_policy(
        READ_ANYWHERE
        + "    - {name: named, operation: http, match: {hosts: [api.example.org, '*.arxiv.org', "
        "'*.pypi.org', '*.org']}, action: allow}\n",
        default="deny",
    )
    compiled = compile_sandbox(policy, TARGETS.sandbox, ceiling=OPEN, admitted=["x.example.net"])
    assert compiled.names("api.example.org")
    assert compiled.names("export.arxiv.org")
    assert compiled.names("x.example.net")
    assert compiled.admitted == ("x.example.net",)
    assert compiled.names(TARGETS.sandbox.package_cache.host)
    # Wildcards the compiler cannot emit name nothing (review L3).
    assert not compiled.names("docs.pypi.org")
    assert not compiled.names("example.org")
    assert not compiled.names("bad host")


def test_a_service_under_a_wildcard_on_an_admitted_port_is_a_compile_error() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: lab, operation: http, match: {hosts: ['*.example.org']}, action: allow}\n",
        default="deny",
    )
    target = TARGETS.sandbox.model_copy(
        update={
            "package_cache": ServiceEndpoint.model_validate(
                {"host": "cache.lab.example.org", "port": 443}
            )
        }
    )
    with pytest.raises(CompileError, match="under the wildcard"):
        compile_sandbox(policy, target, ceiling=OPEN)


def test_a_closed_policy_refuses_every_admission() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: api, operation: http, match: {hosts: [api.example.org]}, action: allow}\n",
        default="deny",
    )
    refusal = admission_refusal(policy, TARGETS.sandbox, "x.example.org", 443, ceiling=OPEN)
    assert refusal is not None
    assert "reaches only the hosts it names" in refusal


def test_admission_follows_the_most_specific_rule() -> None:
    policy = network_policy(
        READ_ANYWHERE
        + "    - {name: blocked, operation: http, match: {hosts: [blocked.example.org]}, "
        "action: deny}\n",
        default="deny",
    )
    refusal = admission_refusal(policy, TARGETS.sandbox, "blocked.example.org", 443, ceiling=OPEN)
    assert refusal is not None
    assert "allows no request to blocked.example.org" in refusal
    assert admission_refusal(policy, TARGETS.sandbox, "ok.example.org", 443, ceiling=OPEN) is None


def test_floor_hosts_are_not_named_in_an_open_policy() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: web, operation: http, match: {hosts: ['*', metadata.goog, "
        "blocked.example.org]}, action: allow}\n"
    )
    compiled = compile_research(policy, network={"deny_hosts": ["blocked.example.org"]})
    assert "sandbox-science-egress" not in compiled.policy.network_policies
    subjects = {f.subject for f in compiled.findings if "not admitted" in f.message}
    assert subjects == {"hosts.metadata.goog", "hosts.blocked.example.org"}


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
    compiled = compile_sandbox(policy, TARGETS.sandbox, ceiling=OPEN, admitted=["k8s.example.org"])
    (endpoint,) = compiled.policy.network_policies["sandbox-science-admitted"].endpoints
    assert endpoint.ports == (80, 443, 9443)
    assert any("control-plane" in finding.message for finding in compiled.findings)


def test_a_service_may_use_a_port_egress_endpoints_admit() -> None:
    # No endpoint admits every host any more, so nothing overlaps the service's.
    target = TARGETS.sandbox.model_copy(
        update={
            "package_cache": ServiceEndpoint.model_validate({"host": "cache.internal", "port": 443})
        }
    )
    compiled = compile_sandbox(EXAMPLE, target, ceiling=OPEN)
    (cache,) = compiled.policy.network_policies["sandbox-science-package-cache"].endpoints
    assert (cache.host, cache.ports) == ("cache.internal", (443,))
    assert openshell_ambiguities(compiled.policy) == []


def single_host_target() -> SandboxTarget:
    return TARGETS.sandbox.model_copy(
        update={
            "package_cache": ServiceEndpoint.model_validate(
                {"host": "host.openshell.internal", "port": 8090}
            ),
            "mcp_proxy": ServiceEndpoint.model_validate(
                {"host": "host.openshell.internal", "port": 8080}
            ),
        }
    )


def test_single_host_services_stay_behind_the_middleware() -> None:
    # Both services on the gateway host (the VM driver on babbage). Excluding the
    # host would take every port on it out of the middleware.
    compiled = compile_sandbox(EXAMPLE, single_host_target(), ceiling=OPEN)
    middlewares = compiled.policy.network_middlewares
    assert middlewares is not None
    middleware = middlewares["sandbox-science-policy"]
    assert middleware.endpoints.include == ("**",)
    assert middleware.endpoints.exclude is None
    assert middleware.config["package_cache"] == {"host": "host.openshell.internal", "port": 8090}
    assert middleware.config["mcp_proxy"] == {"host": "host.openshell.internal", "port": 8080}
    entries = compiled.policy.network_policies
    for key in ("sandbox-science-package-cache", "sandbox-science-mcp-proxy"):
        (endpoint,) = entries[key].endpoints
        assert endpoint.protocol == "rest"


def test_services_sharing_a_port_are_a_compile_error() -> None:
    target = single_host_target().model_copy(
        update={
            "mcp_proxy": ServiceEndpoint.model_validate(
                {"host": "host.openshell.internal", "port": 8090}
            )
        }
    )
    with pytest.raises(CompileError, match="different ports"):
        compile_sandbox(EXAMPLE, target, ceiling=OPEN)


RESERVED_RULES = """
network:
  rules:
    - name: names
      operation: http
      match:
        hosts:
          - pypi.org
          - registry.npmjs.com
          - mcp.example.org
          - package-cache.sandbox-science.internal
          - mcp-proxy.sandbox-science.internal
          - ok.example.org
      action: allow
    - {name: crates, operation: http, match: {hosts: ["*.crates.io"]}, action: allow}
mcp:
  servers:
    tools:
      url: https://mcp.example.org/mcp
      revision: "2025-11-25"
      auth: {mode: none}
      tools: {allow: [x]}
"""


@pytest.mark.parametrize("default", ["deny", "escalate"])
def test_reserved_hosts_named_in_rules_are_not_admitted(default: str) -> None:
    policy = network_policy(RESERVED_RULES, default=default)
    compiled = compile_research(policy)
    hosts = {
        endpoint.host
        for entry in compiled.policy.network_policies.values()
        for endpoint in entry.endpoints
    }
    assert "ok.example.org" in hosts
    for reserved in ("pypi.org", "registry.npmjs.com", "mcp.example.org", "**.crates.io"):
        assert reserved not in hosts
    refused = {f.subject for f in compiled.findings if "not admitted" in f.message}
    assert {"hosts.pypi.org", "hosts.registry.npmjs.com", "hosts.mcp.example.org"} <= refused
    assert "hosts.*.crates.io" in refused
    for service in ("package-cache", "mcp-proxy"):
        assert f"hosts.{service}.sandbox-science.internal" in refused


def test_the_gateway_host_alias_is_accepted_only_as_a_service_host() -> None:
    # A single-host deployment (the VM driver) runs its services on the gateway host.
    service = ServiceEndpoint.model_validate({"host": "host.openshell.internal", "port": 8090})
    assert service.host == "host.openshell.internal"
    with pytest.raises(ValidationError):
        ServiceEndpoint.model_validate({"host": "host.docker.internal", "port": 8090})
    # Project rules cannot reach the gateway host through it.
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: host, operation: http, match: {hosts: [host.openshell.internal]}, "
        "action: allow}\n",
        default="deny",
    )
    compiled = compile_research(policy)
    assert "sandbox-science-egress" not in compiled.policy.network_policies
    assert any(f.subject == "hosts.host.openshell.internal" for f in compiled.findings)


def test_without_worker_access_no_endpoint_admits_the_mcp_proxy() -> None:
    text = (ROOT / "examples" / "policy.yaml").read_text()
    policy = parse_policy(text.replace("worker_access: true", "worker_access: false"))
    compiled = compile_research(policy)
    assert "sandbox-science-mcp-proxy" not in compiled.policy.network_policies
    middlewares = compiled.policy.network_middlewares
    assert middlewares is not None
    assert middlewares["sandbox-science-policy"].endpoints.exclude is None
    assert any(finding.subject == "mcp.proxy.worker_access" for finding in compiled.findings)


EXAMPLE_OPAM = "  opam:                     # OCaml: EasyCrypt, Jasmin\n    action: allow\n"


def example_with_opam(action: str) -> LoadedPolicy:
    text = (ROOT / "examples" / "policy.yaml").read_text()
    assert EXAMPLE_OPAM in text
    return parse_policy(text.replace(EXAMPLE_OPAM, EXAMPLE_OPAM.replace("allow", action)))


def opam_findings(compiled: Compiled) -> list[str]:
    return [
        finding.message
        for finding in compiled.findings
        if finding.subject == "packages.opam" and finding.severity is Severity.WARNING
    ]


@pytest.mark.parametrize("action", ["escalate", "deny", "alert"])
def test_an_opam_action_the_network_rules_undo_is_reported(action: str) -> None:
    # opam downloads an archive from the package's own URL whenever the cache does not
    # deliver it, and the example's source-downloads rule allows GitHub.
    (message,) = opam_findings(compile_research(example_with_opam(action)))
    assert message.startswith(f"packages.opam is {action}, which does not hold")
    assert "rule source-hosts allows that (GET https://github.com/" in message


def test_an_allowed_opam_is_not_reported() -> None:
    assert opam_findings(compile_research(EXAMPLE)) == []


def test_an_unset_opam_is_reported_with_the_default_it_takes() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: reads, operation: http, match: {hosts: ['*'], methods: [GET]}, "
        "action: allow}\n"
    )
    (message,) = opam_findings(compile_research(policy))
    assert message.startswith("packages.opam is unset, so opam archives take defaults.action")
    assert "rule reads allows that" in message


@pytest.mark.parametrize(
    "network",
    [
        pytest.param("", id="nothing-allowed"),
        pytest.param(
            "network:\n  rules:\n"
            "    - {name: api, operation: http, match: {hosts: [api.example.org], "
            "methods: [GET]}, action: allow}\n",
            id="unrelated-host",
        ),
        pytest.param(
            "network:\n  rules:\n"
            "    - {name: reads, operation: http, match: {hosts: ['*'], methods: [GET]}, "
            "action: escalate}\n",
            id="downloads-escalate",
        ),
    ],
)
def test_an_opam_action_holds_when_the_rules_stop_the_downloads(network: str) -> None:
    for default in ("deny", "escalate"):
        policy = network_policy(network + "packages:\n  opam: {action: escalate}\n", default)
        assert opam_findings(compile_research(policy)) == []


def test_the_pool_ceiling_counts_when_opam_downloads_are_checked() -> None:
    # A pool that reaches only the registries stops opam's own downloads as well.
    compiled = compile_research(
        example_with_opam("escalate"), network={"allow_hosts": ["opam.ocaml.org"]}
    )
    assert opam_findings(compiled) == []


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
    assert len(names.any) == 67
    # The server lists bare names, and the fence admits them as listed (lab 39).
    assert "e-print-mcp_search_eprint" in names.any
    assert not any(name.startswith("crypto_research_mcp-") for name in names.any)
    assert [binary.path for binary in entry.binaries] == ["/opt/sandbox-science/bin/python*"]
    # Inside OpenShell the proxy sees only synthetic addresses, so the fence applies the
    # floor and the ceiling to where a name leads (lab 13).
    assert endpoint.allowed_ips == mcp_server_ranges(OPEN)
    assert compiled.policy.filesystem_policy == FilesystemPolicy(
        include_workdir=True,
        read_only=(*BASELINE_READ_ONLY, "/opt/sandbox-science"),
        read_write=BASELINE_READ_WRITE,
    )
    assert compiled.policy.landlock == HARD_LANDLOCK


def test_mcp_server_names_reach_only_public_addresses_the_floor_and_ceiling_allow() -> None:
    floor = HardFloor(frozenset({"10.0.0.2", "203.0.113.7", "gateway.lab.internal"}))
    ceiling = PoolCeiling.model_validate({"network": {"deny_hosts": ["198.51.100.9"]}})
    policy = parse_policy(
        HEADER
        + "mcp:\n  servers:\n"
        + "    named: {url: 'https://mcp.example.org/x', revision: '1', auth: {mode: none}}\n"
        + "    literal: {url: 'https://192.0.2.10/x', revision: '1', auth: {mode: none}}\n"
    )
    compiled = compile_mcp_proxy(policy, TARGETS.mcp_proxy, ceiling=ceiling, floor=floor)
    ranges = compiled.policy.network_policies["mcp-named"].endpoints[0].allowed_ips
    assert ranges == mcp_server_ranges(ceiling, floor)
    networks = [ipaddress.ip_network(network) for network in ranges or ()]

    def admitted(address: str) -> bool:
        return any(ipaddress.ip_address(address) in network for network in networks)

    assert admitted("8.8.8.8")
    assert admitted("2606:4700::1111")
    for denied in ("169.254.169.254", "10.0.0.2", "203.0.113.7", "198.51.100.9", "172.17.0.1"):
        assert not admitted(denied), denied
    # An address literal is its own limit.
    literal = compiled.policy.network_policies["mcp-literal"].endpoints[0]
    assert literal.allowed_ips is None


def test_the_example_fence_matches_the_library_for_every_known_tool() -> None:
    compiled = compile_mcp(EXAMPLE)
    tools = [entry["name"] for entry in json.loads(FIXTURE.read_text())["tools"]]
    made_up = [
        "github-mcp_delete_repository",
        "e-print-mcp_get_or_create_note",
        # A portal's prefixed name, which the server does not list (lab 39).
        "crypto_research_mcp-firecrawl-mcp_firecrawl_search",
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
@pytest.mark.parametrize("admit_escalated", [False, True])
def test_compiled_tool_rules_are_never_looser_than_the_library(
    tools: str, prefix: str, default: str, admit_escalated: bool
) -> None:
    """Admitting escalated tools admits nothing but them beyond what is allowed, and
    denies nothing the fence admits without it."""
    policy = mcp_policy(tools, prefix=prefix, default=default)
    compiled = compile_mcp(policy, admit_escalated=admit_escalated)
    held = compile_mcp(policy)
    admitted = library_allows_or_escalates_by_name if admit_escalated else library_allows
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
            assert admitted(policy, "srv", tool), tool
        if openshell_admits(held.policy, "srv", tool):
            assert openshell_admits(compiled.policy, "srv", tool), tool


def test_escalated_tools_are_admitted_at_the_fence_only_on_request() -> None:
    policy = mcp_policy("{allow: ['*_search'], escalate: [crawl, '*_feedback'], deny: ['admin_*']}")
    held = compile_mcp(policy)
    admitting = compile_mcp(policy, admit_escalated=True)
    for tool, before, after in (
        ("paper_search", True, True),
        ("crawl", False, True),
        ("paper_feedback", False, True),
        ("admin_feedback", False, False),
        ("admin_search", False, False),
        ("other", False, False),  # escalated by the default action only: still held
    ):
        assert openshell_admits(held.policy, "srv", tool) is before, tool
        assert openshell_admits(admitting.policy, "srv", tool) is after, tool
    assert any("escalate entries match are admitted" in f.message for f in admitting.findings)


def test_admitting_escalated_tools_never_denies_a_tool_allowed_by_name() -> None:
    policy = mcp_policy(
        "{allow: [portal_list, paper_search], escalate: [crawl, '*_feedback'], deny: ['portal_*']}"
    )
    held = compile_mcp(policy)
    admitting = compile_mcp(policy, admit_escalated=True)
    for tool, before, after in (
        ("portal_list", True, True),  # admitting '*_feedback' would deny it with 'portal_*'
        ("paper_search", True, True),
        ("crawl", False, True),
        ("paper_feedback", False, False),  # so the escalated pattern stays held
        ("portal_feedback", False, False),
        ("portal_reset", False, False),
    ):
        assert openshell_admits(held.policy, "srv", tool) is before, tool
        assert openshell_admits(admitting.policy, "srv", tool) is after, tool
    (warning,) = [f for f in admitting.findings if "are not admitted" in f.message]
    assert "*_feedback" in warning.message
    assert "portal_list" in warning.message


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


# Provider credentials.

PROVIDERS = (
    "credentials:\n  providers:\n"
    "    - {name: api-token, profile: token, hosts: [api.example.org, files.example.org]}\n"
)


def endpoints_for(compiled: Compiled, host: str) -> list[tuple[str, Endpoint]]:
    return [
        (name, endpoint)
        for name, entry in compiled.policy.network_policies.items()
        for endpoint in entry.endpoints
        if endpoint.host == host
    ]


def test_credentials_are_bound_to_their_hosts_on_https_only() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: web, operation: http, match: {hosts: ['*'], methods: [GET]}, action: allow}\n"
        "    - {name: api, operation: http, match: {hosts: [api.example.org], ports: [8443]}, "
        "action: allow}\n" + PROVIDERS
    )
    compiled = compile_research(policy)
    public = compiled.policy.network_policies["sandbox-science-egress"].endpoints[0]
    # The rule names api.example.org: its other ports stay with the egress endpoints.
    assert [(name, e.ports, e.path) for name, e in endpoints_for(compiled, "api.example.org")] == [
        ("sandbox-science-egress", (80, 8443), None),
        ("sandbox-science-credentials", (443,), "/**"),
    ]
    # files.example.org is named only for its credential.
    ((name, files),) = endpoints_for(compiled, "files.example.org")
    assert name == "sandbox-science-credentials"
    for _, endpoint in [*endpoints_for(compiled, "api.example.org"), (name, files)]:
        assert endpoint.allowed_ips == public.allowed_ips
    bindings = {
        endpoint.host: endpoint.credential_binding
        for endpoint in compiled.policy.network_policies["sandbox-science-credentials"].endpoints
    }
    assert {host: binding and binding.provider for host, binding in bindings.items()} == {
        "api.example.org": "api-token",
        "files.example.org": "api-token",
    }
    info = [f for f in compiled.findings if f.subject == "credentials.providers"]
    assert len(info) == 1
    assert "profiles without endpoints" in info[0].message


def test_a_closed_policy_binds_credentials_without_address_ranges() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: api, operation: http, match: {hosts: [api.example.org], ports: [8443]}, "
        "action: allow}\n" + PROVIDERS,
        default="deny",
    )
    compiled = compile_research(policy)
    assert [(e.ports, e.allowed_ips) for _, e in endpoints_for(compiled, "api.example.org")] == [
        ((80, 8443), None),
        ((443,), None),
    ]
    # No rule names files.example.org, and a closed policy admits nothing else.
    assert endpoints_for(compiled, "files.example.org") == []
    (refused,) = [f for f in compiled.findings if f.subject == "credentials.providers.api-token"]
    assert "no rule that could allow a request names files.example.org" in refused.message


def test_a_closed_policy_does_not_bind_hosts_its_rules_deny() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: blocked, operation: http, match: {hosts: [api.example.org]}, action: deny}\n"
        + PROVIDERS,
        default="deny",
    )
    compiled = compile_research(policy)
    assert "sandbox-science-credentials" not in compiled.policy.network_policies
    assert endpoints_for(compiled, "api.example.org") == []


def test_a_policy_without_providers_binds_nothing() -> None:
    compiled = compile_research(network_policy(""))
    assert "sandbox-science-credentials" not in compiled.policy.network_policies
    assert not any(
        endpoint.credential_binding
        for entry in compiled.policy.network_policies.values()
        for endpoint in entry.endpoints
    )


def test_credentials_are_not_bound_where_the_library_forbids() -> None:
    policy = network_policy(
        "network:\n  rules:\n"
        "    - {name: web, operation: http, match: {hosts: ['*'], methods: [GET]}, action: allow}\n"
        "mcp:\n  servers:\n    srv:\n      url: https://mcp.example.org/mcp\n"
        "      revision: '1'\n      auth: {mode: none}\n"
        "credentials:\n  providers:\n"
        "    - name: everywhere\n      profile: token\n"
        "      hosts: [api.example.org, metadata.goog, pypi.org, mcp.example.org, "
        "blocked.example.org, gitlab.corp.internal, package-cache.sandbox-science.internal]\n"
    )
    compiled = compile_research(policy, network={"deny_hosts": ["blocked.example.org"]})
    endpoints = compiled.policy.network_policies["sandbox-science-credentials"].endpoints
    assert [endpoint.host for endpoint in endpoints] == ["api.example.org"]
    refused = {
        f.message.split(":")[0].removeprefix("credential not bound to ")
        for f in compiled.findings
        if f.subject == "credentials.providers.everywhere"
    }
    assert refused == {
        "metadata.goog",
        "pypi.org",
        "mcp.example.org",
        "blocked.example.org",
        "gitlab.corp.internal",
        "package-cache.sandbox-science.internal",
    }
    assert all(
        f.severity is Severity.WARNING and f.message.endswith("(stricter)")
        for f in compiled.findings
        if f.subject == "credentials.providers.everywhere"
    )


# OpenShell v0.1.5 refuses a policy whose endpoints are ambiguous
# (openshell-policy/src/ambiguity.rs): endpoints that overlap on host and port
# must agree on their connection settings, and where neither has the more
# specific path, on how requests are handled, including the credential binding.
# This is a port of that check for the fields the compiler emits.


def _host_tokens(pattern: str) -> list[str | bool]:
    """A host pattern's tokens: a character, or a star (True when it crosses '.')."""
    tokens: list[str | bool] = []
    index = 0
    while index < len(pattern):
        if pattern[index] != "*":
            tokens.append(pattern[index])
            index += 1
            continue
        start = index
        while index < len(pattern) and pattern[index] == "*":
            index += 1
        tokens.append(index - start >= 2)
    return tokens


def _shares_character(left: str | bool, right: str | bool) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, bool) and isinstance(right, bool):
        return True
    character = left if isinstance(left, str) else right
    crosses = right if isinstance(left, str) else left
    return bool(crosses) or character != "."


def openshell_hosts_overlap(left: str | None, right: str | None) -> bool:
    """Whether some host name matches both patterns; no host matches every name."""
    if not left or not right:
        return True
    a, b = _host_tokens(left.lower()), _host_tokens(right.lower())
    queue = [(0, 0)]
    seen: set[tuple[int, int]] = set()
    while queue:
        i, j = queue.pop()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if i == len(a) and j == len(b):
            return True
        if i < len(a) and isinstance(a[i], bool):
            queue.append((i + 1, j))
        if j < len(b) and isinstance(b[j], bool):
            queue.append((i, j + 1))
        if i < len(a) and j < len(b) and _shares_character(a[i], b[j]):
            queue.append(
                (i if isinstance(a[i], bool) else i + 1, j if isinstance(b[j], bool) else j + 1)
            )
    return False


def _path_specificity(path: str | None) -> int:
    return len(path.replace("*", "")) if path else 0


def _paths_overlap(left: str | None, right: str | None) -> bool:
    if not left or not right or left in ("**", "/**") or right in ("**", "/**"):
        return True
    assert not set("*?[") & set(left + right), "the port handles literal paths only"
    return left == right


def _contributes_metadata(endpoint: Endpoint) -> bool:
    return (
        endpoint.protocol != "tcp"
        or bool(endpoint.allowed_ips)
        or endpoint.credential_binding is not None
    )


def openshell_ambiguities(policy: OpenShellPolicy) -> list[str]:
    """The conflicts OpenShell would report for this policy, as readable strings."""
    endpoints = [
        (f"{name}[{index}]", endpoint)
        for name, entry in policy.network_policies.items()
        for index, endpoint in enumerate(entry.endpoints)
    ]
    found: list[str] = []
    for (left_name, left), (right_name, right) in itertools.combinations(endpoints, 2):
        if not set(left.ports) & set(right.ports) or not openshell_hosts_overlap(
            left.host, right.host
        ):
            continue
        pairs: dict[str, tuple[object, object]] = {
            "transparent_tcp_eligible": (left.protocol == "tcp", right.protocol == "tcp"),
            "allowed_ips": (
                sorted({ip.strip().lower() for ip in left.allowed_ips or ()}),
                sorted({ip.strip().lower() for ip in right.allowed_ips or ()}),
            ),
        }
        if (
            _contributes_metadata(left)
            and _contributes_metadata(right)
            and _paths_overlap(left.path, right.path)
            and _path_specificity(left.path) == _path_specificity(right.path)
        ):
            pairs |= {
                "protocol": (left.protocol, right.protocol),
                "enforcement": (left.enforcement or "audit", right.enforcement or "audit"),
                "allow_encoded_slash": (
                    bool(left.allow_encoded_slash),
                    bool(right.allow_encoded_slash),
                ),
                "credential_binding.provider": (
                    left.credential_binding.provider if left.credential_binding else "",
                    right.credential_binding.provider if right.credential_binding else "",
                ),
            }
            if left.protocol == right.protocol == "mcp":
                pairs |= {
                    "json_rpc_max_body_bytes": (
                        (left.mcp and left.mcp.max_body_bytes) or 65536,
                        (right.mcp and right.mcp.max_body_bytes) or 65536,
                    ),
                    "mcp.strict_tool_names": (
                        left.mcp.strict_tool_names if left.mcp else True,
                        right.mcp.strict_tool_names if right.mcp else True,
                    ),
                }
        conflicts = sorted(field for field, (a, b) in pairs.items() if a != b)
        if conflicts:
            found.append(f"{left_name} {left.host} vs {right_name} {right.host}: {conflicts}")
    return found


@pytest.mark.parametrize(
    ("left", "right", "overlap"),
    [
        (None, "github.com", True),
        ("github.com", "github.com", True),
        ("api.github.com", "github.com", False),
        ("**.github.com", "api.github.com", True),
        ("**.github.com", "github.com", False),
        ("**.github.com", "**.api.github.com", True),
        ("**.arxiv.org", "**.github.com", False),
    ],
)
def test_the_host_overlap_port_matches_openshell(
    left: str | None, right: str | None, overlap: bool
) -> None:
    assert openshell_hosts_overlap(left, right) is overlap
    assert openshell_hosts_overlap(right, left) is overlap


def test_the_ambiguity_port_reproduces_the_lab_refusal() -> None:
    # Lab 2, variant A1: a provider profile's endpoint beside the compiled ones.
    compiled = compile_research(EXAMPLE)
    rules = dict(compiled.policy.network_policies)
    rules["_provider_gh"] = rules["sandbox-science-egress"].model_copy(
        update={
            "endpoints": (
                Endpoint(
                    host="api.github.com", ports=(443,), protocol="rest", enforcement="enforce"
                ),
            )
        }
    )
    composed = compiled.policy.model_copy(update={"network_policies": rules})
    assert any("allowed_ips" in conflict for conflict in openshell_ambiguities(composed))
    # Without its path selector, a credential endpoint would compete with a wildcard's.
    compiled = compile_research(
        parse_policy(
            EXAMPLE_TEXT.replace(
                "    - name: read-only-web\n",
                "    - name: github-wildcard\n      operation: http\n"
                "      match: { hosts: ['*.github.com'], methods: [GET] }\n      action: allow\n"
                "    - name: read-only-web\n",
                1,
            )
        )
    )
    credentials = compiled.policy.network_policies["sandbox-science-credentials"]
    unpathed = tuple(e.model_copy(update={"path": None}) for e in credentials.endpoints)
    rules = dict(compiled.policy.network_policies)
    rules["sandbox-science-credentials"] = credentials.model_copy(update={"endpoints": unpathed})
    composed = compiled.policy.model_copy(update={"network_policies": rules})
    assert any(
        "credential_binding.provider" in conflict for conflict in openshell_ambiguities(composed)
    )


def test_compiled_policies_are_not_ambiguous() -> None:
    assert openshell_ambiguities(compile_research(EXAMPLE).policy) == []
    assert openshell_ambiguities(compile_mcp(EXAMPLE).policy) == []


def test_random_policies_compile_unambiguously_and_bind_each_host_once() -> None:
    rng = random.Random(20261010)  # noqa: S311 - test data, not secrets
    names = ["api.example.org", "example.org", "files.example.org", "github.com", "a.b.example.org"]
    patterns = [*names, "*.example.org", "*.b.example.org", "*", "93.184.215.14", "10.0.0.5"]
    for _ in range(300):
        rules = [
            (
                rng.sample(patterns, rng.randint(1, 3)),
                rng.random() < 0.3,
                rng.choice(["allow", "escalate", "deny"]),
            )
            for _ in range(rng.randint(0, 4))
        ]
        default = rng.choice(["deny", "escalate"])
        bound = rng.sample(names, rng.randint(0, 3))
        text = "".join(
            f"    - {{name: r{index}, operation: http, match: {{hosts: "
            f"[{', '.join(repr(host) for host in hosts)}]{', ports: [8443]' if ports else ''}}}, "
            f"action: {action}}}\n"
            for index, (hosts, ports, action) in enumerate(rules)
        )
        text = "network:\n  rules:\n" + text if text else ""
        if bound:
            text += "credentials:\n  providers:\n" + "".join(
                f"    - {{name: p{index}, profile: token, hosts: [{host}]}}\n"
                for index, host in enumerate(bound)
            )
        compiled = compile_research(network_policy(text, default=default))
        assert openshell_ambiguities(compiled.policy) == [], text
        # A closed policy binds only hosts that a rule which could allow a request names.
        reachable = [hosts for hosts, _, action in rules if action != "deny"]
        open_internet = default != "deny" or any("*" in h for hosts in reachable for h in hosts)
        named = {host for hosts in reachable for host in hosts if "*" not in host}
        for host in bound:
            on_https = [
                (name, endpoint)
                for name, endpoint in endpoints_for(compiled, host)
                if 443 in endpoint.ports
            ]
            if open_internet or host in named:
                assert len(on_https) == 1, text
                ((name, endpoint),) = on_https
                assert name == "sandbox-science-credentials"
                assert endpoint.credential_binding is not None
            else:
                assert endpoints_for(compiled, host) == [], text


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
    assert TARGETS.sandbox.package_cache.port == 8090
    assert TARGETS.mcp_proxy.versions == ("2025-11-25",)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("package-cache.sandbox-science.internal", "10.0.0.5"),
        ("package-cache.sandbox-science.internal", "localhost"),
        ("port: 8090", "port: 6443"),
        ("middleware: sandbox-science-policy", "middleware: Not A Name"),
        ("binaries: [/**]", "binaries: [bin/sh]"),
        ('versions: ["2025-11-25"]', 'versions: ["2024-11-05"]'),
        ("binaries: [/opt/sandbox-science/bin/python*]", "binaries: []"),
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
            "package_cache": {"host": "cache.example.org", "port": 443},
            "mcp_proxy": {"host": "mcp.example.org", "port": 8080},
        }
    )
    assert target.binaries == ("/**",)


# Filesystem section.


def test_a_research_sandbox_may_open_the_devices_research_code_needs() -> None:
    # Lab 5: without these, Landlock refused pseudo-terminals (Sage's pexpect interfaces)
    # and /dev/shm (every multiprocessing lock).
    filesystem = compile_research(EXAMPLE).policy.filesystem_policy
    assert filesystem is not None
    assert filesystem.include_workdir
    assert {"/dev/ptmx", "/dev/pts", "/dev/shm"} <= set(filesystem.read_write)
    assert filesystem.read_write == (*BASELINE_READ_WRITE, *SANDBOX_DEVICES_READ_WRITE)
    assert filesystem.read_only == (*BASELINE_READ_ONLY, *SANDBOX_DEVICES_READ_ONLY)


@pytest.mark.parametrize("which", ["research", "orchestrator", "mcp-proxy"])
def test_every_compiled_sandbox_lists_the_baseline_and_requires_landlock(which: str) -> None:
    # OpenShell's supervisor adds its baseline only where the path exists on its own host.
    # For the Mac's VM driver that is macOS, which has no /lib or /proc, so a sandbox there
    # could not read /proc until the compiler listed the baseline (Mac test, 2026-10-21).
    # With best_effort, OpenShell would run a sandbox whose rules Landlock cannot apply.
    orchestrator = TARGETS.orchestrator
    assert orchestrator is not None
    policy = {
        "research": lambda: compile_research(EXAMPLE).policy,
        "orchestrator": lambda: compile_orchestrator(orchestrator),
        "mcp-proxy": lambda: compile_mcp_proxy(EXAMPLE, TARGETS.mcp_proxy, ceiling=OPEN).policy,
    }[which]()
    filesystem = policy.filesystem_policy
    assert filesystem is not None
    assert set(BASELINE_READ_ONLY) <= set(filesystem.read_only)
    assert set(BASELINE_READ_WRITE) <= set(filesystem.read_write)
    assert {"/lib", "/proc"} <= set(filesystem.read_only)
    assert policy.landlock == LandlockPolicy(compatibility="hard_requirement")
    assert yaml.safe_load(policy.to_yaml())["landlock"] == {"compatibility": "hard_requirement"}


def test_the_filesystem_section_comes_first_and_revisions_keep_the_live_one() -> None:
    policy = compile_research(EXAMPLE).policy
    assert list(yaml.safe_load(policy.to_yaml())) == [
        "version",
        "filesystem_policy",
        "landlock",
        "network_policies",
        "network_middlewares",
    ]
    # OpenShell refuses a revision that drops paths it added at startup (lab, 2026-10-11)
    # or changes the Landlock section (validate_static_fields_unchanged).
    live = FilesystemPolicy(read_only=("/dev/random", "/usr"), read_write=("/dev/shm", "/tmp"))
    revision = policy.with_static(live, HARD_LANDLOCK)
    assert revision.filesystem_policy == live
    assert revision.landlock == HARD_LANDLOCK
    assert revision.network_policies == policy.network_policies
    assert policy.filesystem_policy != live  # the original is unchanged
    # A sandbox created before compiled policies had a Landlock section keeps none.
    legacy = policy.with_static(live, None)
    assert "landlock" not in yaml.safe_load(legacy.to_yaml())


def test_the_image_paths_are_added_and_writable_wins() -> None:
    target = TARGETS.sandbox.model_copy(
        update={
            "filesystem": SandboxFilesystem(
                read_only=("/opt/conda", "/dev/random", "/data"),
                read_write=("/data", "/dev/shm"),
            )
        }
    )
    filesystem = compile_sandbox(EXAMPLE, target, ceiling=OPEN).policy.filesystem_policy
    assert filesystem is not None
    assert filesystem.read_only == (*BASELINE_READ_ONLY, "/dev/random", "/opt/conda")
    assert filesystem.read_write == (*BASELINE_READ_WRITE, *SANDBOX_DEVICES_READ_WRITE, "/data")


@pytest.mark.parametrize(
    "filesystem",
    [
        {"read_only": ["opt/conda"]},
        {"read_only": ["/opt/../etc"]},
        {"read_only": ["/opt//conda"]},
        {"read_only": ["/opt/conda/"]},
        {"read_only": ["/opt/con da"]},
        {"read_write": ["/"]},
        {"read_write": ["/" + "a" * 4096]},
        {"writable": ["/data"]},
    ],
)
def test_invalid_image_paths_are_rejected(filesystem: dict[str, list[str]]) -> None:
    with pytest.raises(ValidationError):
        SandboxFilesystem.model_validate(filesystem)


def test_too_many_paths_are_a_compile_error() -> None:
    target = TARGETS.sandbox.model_copy(
        update={"filesystem": SandboxFilesystem(read_only=tuple(f"/opt/p{i}" for i in range(250)))}
    )
    with pytest.raises(CompileError, match="256 filesystem paths"):
        compile_sandbox(EXAMPLE, target, ceiling=OPEN)


def test_the_path_limit_counts_the_listed_baseline_once() -> None:
    fixed = (
        len(BASELINE_READ_ONLY)
        + len(BASELINE_READ_WRITE)
        + len(SANDBOX_DEVICES_READ_ONLY)
        + len(SANDBOX_DEVICES_READ_WRITE)
        + BASELINE_UNLISTED
    )

    def target(count: int) -> SandboxTarget:
        paths = tuple(f"/opt/p{i}" for i in range(count))
        return TARGETS.sandbox.model_copy(
            update={"filesystem": SandboxFilesystem(read_only=(*paths, "/usr", "/proc"))}
        )

    compile_sandbox(EXAMPLE, target(MAX_FILESYSTEM_PATHS - fixed), ceiling=OPEN)
    with pytest.raises(CompileError, match="256 filesystem paths"):
        compile_sandbox(EXAMPLE, target(MAX_FILESYSTEM_PATHS - fixed + 1), ceiling=OPEN)


def test_example_targets_name_no_image_paths() -> None:
    assert TARGETS.sandbox.filesystem == SandboxFilesystem()


# The orchestrator's sandbox.


def test_the_orchestrator_reaches_only_the_platform_services() -> None:
    assert TARGETS.orchestrator is not None
    policy = compile_orchestrator(TARGETS.orchestrator)
    assert list(policy.network_policies) == [
        "sandbox-science-worker-api",
        "sandbox-science-model-gateway",
        "sandbox-science-mcp-proxy",
    ]
    endpoints = [
        (endpoint.host, endpoint.ports, endpoint.protocol, endpoint.enforcement, endpoint.access)
        for entry in policy.network_policies.values()
        for endpoint in entry.endpoints
    ]
    assert endpoints == [
        ("worker.sandbox-science.internal", (8100,), "rest", "enforce", "full"),
        ("litellm.sandbox-science.internal", (4000,), "rest", "enforce", "full"),
        ("mcp-proxy.sandbox-science.internal", (8080,), "rest", "enforce", "full"),
    ]
    for entry in policy.network_policies.values():
        assert entry.binaries == (Binary(path="/opt/sandbox-science/bin/python*"),)
    assert policy.network_middlewares is None
    # The harness runs from the image's prefix, which Landlock must let it read (lab 9).
    assert policy.filesystem_policy == FilesystemPolicy(
        include_workdir=True,
        read_only=(*BASELINE_READ_ONLY, "/opt/sandbox-science"),
        read_write=BASELINE_READ_WRITE,
    )
    assert policy.landlock == HARD_LANDLOCK
    document = yaml.safe_load(policy.to_yaml())
    assert schema_keys(document) <= ALLOWED_KEYS
    assert None not in leaf_values(document)


def test_the_model_gateway_and_mcp_proxy_are_optional_and_cannot_share_the_worker_api() -> None:
    target = OrchestratorTarget(
        worker_api=ServiceEndpoint(host="host.openshell.internal", port=8100),
        binaries=("/usr/local/bin/python3*",),
    )
    assert list(compile_orchestrator(target).network_policies) == ["sandbox-science-worker-api"]
    for service in ("model_gateway", "mcp_proxy", "state_service"):
        with pytest.raises(CompileError, match=r"share host\.openshell\.internal:8100"):
            compile_orchestrator(target.model_copy(update={service: target.worker_api}))


@pytest.mark.parametrize(
    "service",
    [
        {"host": "169.254.169.254", "port": 80},
        {"host": "metadata.google.internal", "port": 80},
        {"host": "worker.sandbox-science.internal", "port": 6443},
    ],
)
def test_the_orchestrators_services_obey_the_service_rules(service: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OrchestratorTarget.model_validate({"worker_api": service, "binaries": ["/usr/bin/python3"]})


def test_tcp_service_hosts_must_be_names() -> None:
    with pytest.raises(ValueError, match="DNS name"):
        SandboxTarget.model_validate(
            {
                "middleware": "sandbox-science-policy",
                "package_cache": {"host": "10.0.0.5", "port": 443},
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


def test_scoped_state_service_has_only_its_declared_endpoint() -> None:
    target = OrchestratorTarget(
        worker_api=ServiceEndpoint(host="host.openshell.internal", port=8100),
        state_service=ServiceEndpoint(host="state.sandbox-science.internal", port=8202),
        binaries=("/opt/sandbox-science/bin/python*",),
    )
    compiled = compile_orchestrator(target)
    assert list(compiled.network_policies) == [
        "sandbox-science-worker-api",
        "sandbox-science-state-service",
    ]
    endpoint = compiled.network_policies["sandbox-science-state-service"].endpoints[0]
    assert (endpoint.host, endpoint.ports, endpoint.protocol, endpoint.access) == (
        "state.sandbox-science.internal",
        (8202,),
        "rest",
        "full",
    )
    with pytest.raises(ValidationError):
        OrchestratorTarget.model_validate(
            {
                "worker_api": {"host": "worker.example.org", "port": 8100},
                "state_service": {"host": "169.254.169.254", "port": 80},
                "binaries": ["/opt/sandbox-science/bin/python*"],
            }
        )
