# SPDX-License-Identifier: MIT
"""Specification tests for policy decisions (docs/POLICY.md).

These tests are derived from the specification, not from the implementation. Each test builds
the smallest policy that shows the rule under test. Sections follow the specification: loading,
hard floor, pool ceiling, rule matching and selection, operation classes, non-public
destinations, rule limits, git, MCP, packages, escalation timeout, monitors and the digest.
"""

import hashlib
import json
import re
import textwrap
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import pytest

from sandbox_science.policy import (
    Action,
    Decision,
    Ecosystem,
    EgressUsage,
    HardFloor,
    HttpRequest,
    LoadedPolicy,
    McpOperation,
    PackageOperation,
    PolicyError,
    PolicyLoadError,
    PoolCeiling,
    classify_http_request,
    effective_resources,
    evaluate,
    evaluate_monitors,
    evaluate_request,
    load_policy,
    parse_policy,
)

HEADER = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: test
"""

REPO = "https://git.example.org/team/repo.git"
FETCH_DISCOVERY = f"{REPO}/info/refs?service=git-upload-pack"
FETCH = f"{REPO}/git-upload-pack"
PUSH_DISCOVERY = f"{REPO}/info/refs?service=git-receive-pack"
PUSH = f"{REPO}/git-receive-pack"

ADMIN_HOST = "policy-admin.example.org"


def load(body: str = "") -> LoadedPolicy:
    """Parse the minimal policy header followed by a dedented YAML body."""
    return parse_policy(HEADER + textwrap.dedent(body))


GIT_CONTENT_TYPES = {
    "/git-upload-pack": "application/x-git-upload-pack-request",
    "/git-receive-pack": "application/x-git-receive-pack-request",
}


def client_request(method: str, url: str, *, credentialed: bool = False) -> HttpRequest:
    """A request as a real client sends it: git POSTs carry git's content type."""
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""
    content_type = next(
        (ctype for suffix, ctype in GIT_CONTENT_TYPES.items() if path.endswith(suffix)), None
    )
    return HttpRequest(
        method=method,
        url=url,
        credentialed=credentialed,
        content_type=content_type if method == "POST" else None,
        has_body=method in ("POST", "PUT", "PATCH"),
    )


def make_ceiling(data: dict[str, object]) -> PoolCeiling:
    return PoolCeiling.model_validate(data)


def admin_floor() -> HardFloor:
    return HardFloor(admin_hosts=frozenset({ADMIN_HOST}))


def decide_http(
    policy: LoadedPolicy,
    method: str,
    url: str,
    *,
    credentialed: bool = False,
    ceiling: PoolCeiling | None = None,
    floor: HardFloor | None = None,
) -> Decision:
    return evaluate_request(
        client_request(method, url, credentialed=credentialed),
        policy,
        ceiling=PoolCeiling.unrestricted() if ceiling is None else ceiling,
        floor=HardFloor() if floor is None else floor,
    )


def decide_mcp(
    policy: LoadedPolicy,
    method: str,
    tool: str | None = None,
    *,
    server: str = "search",
    origin: Literal["orchestrator", "worker"] = "orchestrator",
    ceiling: PoolCeiling | None = None,
    floor: HardFloor | None = None,
) -> Decision:
    return evaluate(
        McpOperation(server=server, method=method, tool=tool, origin=origin),
        policy,
        ceiling=PoolCeiling.unrestricted() if ceiling is None else ceiling,
        floor=HardFloor() if floor is None else floor,
    )


def call_tool(
    policy: LoadedPolicy, tool: str, *, origin: Literal["orchestrator", "worker"] = "orchestrator"
) -> Decision:
    return decide_mcp(policy, "tools/call", tool, origin=origin)


def decide_package(
    policy: LoadedPolicy,
    ecosystem: Ecosystem,
    name: str | None,
    channel: str | None = None,
    kind: Literal["index", "artifact"] = "artifact",
    digest: str | None = None,
) -> Decision:
    return evaluate(
        PackageOperation(ecosystem=ecosystem, name=name, channel=channel, kind=kind, digest=digest),
        policy,
        ceiling=PoolCeiling.unrestricted(),
    )


def escalation_terms(decision: Decision) -> tuple[timedelta, Action]:
    assert decision.action == Action.ESCALATE
    assert decision.escalation is not None
    return decision.escalation.timeout, decision.escalation.on_timeout


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --- Loading -------------------------------------------------------------------------------------


def test_minimal_policy_escalates_unmatched_operations_for_30_minutes() -> None:
    decision = decide_http(load(), "GET", "https://example.org/")
    assert isinstance(decision.action, Action)
    assert decision.action == Action.ESCALATE
    assert decision.layer == "default"
    assert decision.rule is None
    assert escalation_terms(decision) == (timedelta(minutes=30), Action.DENY)


def test_policy_load_error_is_a_policy_error() -> None:
    assert issubclass(PolicyLoadError, PolicyError)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("apiVersion: [unclosed\n", id="invalid-yaml"),
        pytest.param("- a\n- list\n", id="not-a-mapping"),
        pytest.param("", id="empty"),
        pytest.param(HEADER.replace("v0alpha1", "v9"), id="unknown-api-version"),
        pytest.param(HEADER.replace("ProjectPolicy", "PoolCeiling"), id="wrong-kind"),
        pytest.param(HEADER + "surprise: true\n", id="unknown-section"),
    ],
)
def test_invalid_policy_files_are_rejected(text: str) -> None:
    with pytest.raises(PolicyLoadError):
        parse_policy(text)


@pytest.mark.parametrize(
    ("operation", "match", "action"),
    [
        pytest.param("any", '{hosts: ["*"]}', "allow", id="unknown-operation"),
        pytest.param("http", '{hosts: ["*"]}', "permit", id="unknown-action"),
        pytest.param(
            "http", "{hosts: [example.org], path: [/admin]}", "allow", id="misspelled-path"
        ),
        pytest.param("git", '{hosts: ["*"], methods: [GET]}', "allow", id="methods-on-git"),
        pytest.param(
            "http", '{hosts: ["*"], git_service: upload-pack}', "allow", id="service-on-http"
        ),
        pytest.param("git", '{hosts: ["*"], git_service: clone}', "allow", id="unknown-service"),
    ],
)
def test_invalid_network_rules_are_rejected(operation: str, match: str, action: str) -> None:
    with pytest.raises(PolicyLoadError):
        load(
            f"""
            network:
              rules:
                - name: invalid
                  operation: {operation}
                  match: {match}
                  action: {action}
            """
        )


# --- Hard floor ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/",
        "https://LocalHost./",
        "http://localhost:8080/",
        "https://app.localhost/",
        "https://A.b.Localhost./",
        "https://127.0.0.1/",
        "https://127.8.9.10/",
        "http://127.0.0.1:8080/admin",
        "https://[::1]/",
        "https://0.0.0.0/",
        "https://[::]/",
        "https://169.254.10.20/",
        "https://[fe80::1]/",
        "https://kubernetes/",
        "https://kubernetes.default/",
        "https://kubernetes.default.svc/",
        "https://kubernetes.default.svc.cluster.local/",
        "https://Kubernetes.Default.Svc.Cluster.Local./api",
    ],
)
def test_floor_denies_loopback_link_local_unspecified_and_cluster_api(url: str) -> None:
    policy = load(
        """
        defaults: {action: allow}
        network:
          rules:
            - {name: anything, operation: http, match: {hosts: ["*"]}, action: allow}
        """
    )
    decision = decide_http(policy, "GET", url)
    assert decision.action == Action.DENY
    assert decision.layer == "floor"
    assert decision.reason


@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "metadata.google.internal", "[fd00:ec2::254]"],
)
def test_floor_denies_cloud_metadata_endpoints(host: str) -> None:
    policy = load(
        """
        defaults: {action: allow}
        network:
          rules:
            - {name: anything, operation: http, match: {hosts: ["*"]}, action: allow}
        """
    )
    decision = decide_http(policy, "GET", f"https://{host}/latest/meta-data/")
    assert decision.action == Action.DENY
    assert decision.layer == "floor"


@pytest.mark.parametrize(
    "url",
    [
        "https://[::ffff:127.0.0.1]/",
        "https://[::ffff:7f00:1]/",
        "https://[0:0:0:0:0:0:0:1]/",
        "https://[::ffff:169.254.169.254]/",
        "https://[::ffff:0.0.0.0]/",
        "https://[fe80::1%25eth0]/",
        "https://2130706433/",
        "https://0x7f.1/",
        "https://0x7f000001/",
        "https://0177.0.0.1/",
        "https://127.1/",
        "https://0/",
    ],
)
def test_floor_addresses_are_denied_however_they_are_written(url: str) -> None:
    # Non-canonical spellings may already fail classification; either way they never pass.
    policy = load(
        """
        defaults: {action: allow}
        network:
          rules:
            - {name: anything, operation: http, match: {hosts: ["*"]}, action: allow}
        """
    )
    decision = decide_http(policy, "GET", url)
    assert decision.action == Action.DENY
    assert decision.layer in {"floor", "classification"}


@pytest.mark.parametrize(
    "host", ["localhost", "127.0.0.1", "169.254.169.254", "kubernetes", "kubernetes.default.svc"]
)
def test_floor_beats_an_exact_allow_rule(host: str) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: exact
              operation: http
              match:
                hosts: ["{host}"]
              action: allow
        """
    )
    decision = decide_http(policy, "GET", f"https://{host}/")
    assert decision.action == Action.DENY
    assert decision.layer == "floor"


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost.example.org/",
        "https://kubernetes.example.org/",
        "https://kubernetes.default.svc.example.org/",
        "https://metadata.example.com/",
    ],
)
def test_floor_does_not_deny_public_names_that_merely_look_alike(url: str) -> None:
    policy = load(
        """
        network:
          rules:
            - {name: anything, operation: http, match: {hosts: ["*"]}, action: allow}
        """
    )
    assert decide_http(policy, "GET", url).action == Action.ALLOW


@pytest.mark.parametrize(
    "url",
    [
        f"https://{ADMIN_HOST}/v1/rules",
        "https://Policy-Admin.Example.ORG./",
        f"http://{ADMIN_HOST}:8080/",
    ],
)
def test_floor_denies_configured_admin_hosts_despite_an_exact_allow(url: str) -> None:
    policy = load(
        """
        defaults: {action: allow}
        network:
          rules:
            - name: admin-api
              operation: http
              match: {hosts: [policy-admin.example.org]}
              action: allow
        """
    )
    decision = decide_http(policy, "GET", url, floor=admin_floor())
    assert decision.action == Action.DENY
    assert decision.layer == "floor"


def test_admin_hosts_deny_only_themselves() -> None:
    policy = load(
        """
        defaults: {action: allow}
        network:
          rules:
            - name: admin-api
              operation: http
              match: {hosts: [policy-admin.example.org]}
              action: allow
        """
    )
    other_host = decide_http(policy, "GET", "https://api.example.org/", floor=admin_floor())
    without_admin_hosts = decide_http(policy, "GET", f"https://{ADMIN_HOST}/", floor=HardFloor())
    assert other_host.action == Action.ALLOW
    assert without_admin_hosts.action == Action.ALLOW


def test_floor_applies_to_git_operations() -> None:
    policy = load(
        """
        network:
          rules:
            - {name: local-git, operation: git, match: {hosts: [localhost]}, action: allow}
        """
    )
    url = "https://localhost/team/repo.git/info/refs?service=git-upload-pack"
    decision = decide_http(policy, "GET", url)
    assert decision.action == Action.DENY
    assert decision.layer == "floor"


@pytest.mark.parametrize(
    ("method", "tool"), [("initialize", None), ("tools/list", None), ("tools/call", "web_search")]
)
def test_floor_applies_to_declared_mcp_server_urls(method: str, tool: str | None) -> None:
    policy = load(
        """
        defaults: {action: allow}
        mcp:
          servers:
            search:
              url: https://policy-admin.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*"], escalate: [], deny: []}
        """
    )
    decision = decide_mcp(policy, method, tool, floor=admin_floor())
    assert decision.action == Action.DENY
    assert decision.layer == "floor"


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://169.254.169.254/mcp",
        "https://kubernetes.default.svc/mcp",
    ],
)
def test_mcp_servers_on_built_in_floor_hosts_are_never_reachable(url: str) -> None:
    text = f"""
        defaults:
          action: allow
        mcp:
          servers:
            search:
              url: {url}
              revision: "2025-11-25"
              auth:
                mode: none
              tools:
                allow: ["*"]
                escalate: []
                deny: []
        """
    try:
        policy = load(text)
    except PolicyLoadError:
        return  # Rejecting such a server when loading also keeps it unreachable.
    decision = decide_mcp(policy, "tools/call", "web_search")
    assert decision.action == Action.DENY
    assert decision.layer == "floor"


# --- Pool ceiling --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("deny_hosts", "url"),
    [
        (["example.com"], "https://example.com/"),
        (["example.com"], "https://EXAMPLE.com./"),
        (["*.example.com"], "https://a.example.com/"),
        (["*.example.com"], "https://a.b.example.com/"),
        (["*"], "https://example.com/"),
    ],
)
def test_ceiling_deny_hosts_beat_an_exact_project_allow(deny_hosts: list[str], url: str) -> None:
    policy = load(
        """
        network:
          rules:
            - name: exact
              operation: http
              match: {hosts: [example.com, a.example.com, a.b.example.com]}
              action: allow
        """
    )
    ceiling = make_ceiling({"network": {"deny_hosts": deny_hosts}})
    decision = decide_http(policy, "GET", url, ceiling=ceiling)
    assert decision.action == Action.DENY
    assert decision.layer == "ceiling"
    assert decision.reason


def test_ceiling_deny_hosts_leave_other_hosts_alone() -> None:
    policy = load(
        """
        network:
          rules:
            - {name: exact, operation: http, match: {hosts: [example.org]}, action: allow}
        """
    )
    ceiling = make_ceiling({"network": {"deny_hosts": ["*.example.com", "example.net"]}})
    assert (
        decide_http(policy, "GET", "https://example.org/", ceiling=ceiling).action == Action.ALLOW
    )


def test_omitting_allow_hosts_leaves_destinations_unrestricted() -> None:
    policy = load(
        """
        network:
          rules:
            - {name: exact, operation: http, match: {hosts: [example.org]}, action: allow}
        """
    )
    ceiling = make_ceiling({"network": {"deny_hosts": []}})
    assert (
        decide_http(policy, "GET", "https://example.org/", ceiling=ceiling).action == Action.ALLOW
    )


@pytest.mark.parametrize(
    ("allow_hosts", "url"),
    [
        (["example.org"], "https://example.com/"),
        (["example.org"], "https://a.example.org/"),
        (["*.example.org"], "https://example.org/"),
        (["*.example.org"], "https://a.example.com/"),
        ([], "https://example.org/"),
    ],
)
def test_ceiling_allow_hosts_deny_destinations_they_do_not_match(
    allow_hosts: list[str], url: str
) -> None:
    policy = load(
        """
        network:
          rules:
            - name: exact
              operation: http
              match: {hosts: [example.org, a.example.org, example.com, a.example.com]}
              action: allow
        """
    )
    ceiling = make_ceiling({"network": {"allow_hosts": allow_hosts}})
    decision = decide_http(policy, "GET", url, ceiling=ceiling)
    assert decision.action == Action.DENY
    assert decision.layer == "ceiling"


@pytest.mark.parametrize(
    ("allow_hosts", "url"),
    [
        (["example.org"], "https://example.org/"),
        (["example.org"], "https://Example.Org./"),
        (["*.example.org"], "https://a.example.org/"),
        (["*.example.org"], "https://a.b.example.org/"),
        (["*"], "https://example.com/"),
    ],
)
def test_ceiling_allow_hosts_admit_destinations_they_match(
    allow_hosts: list[str], url: str
) -> None:
    policy = load(
        """
        network:
          rules:
            - name: exact
              operation: http
              match: {hosts: [example.org, a.example.org, a.b.example.org, example.com]}
              action: allow
        """
    )
    ceiling = make_ceiling({"network": {"allow_hosts": allow_hosts}})
    assert decide_http(policy, "GET", url, ceiling=ceiling).action == Action.ALLOW


@pytest.mark.parametrize(
    ("allow_hosts", "host"),
    [(["*"], "db.internal"), (["*.internal"], "db.internal"), (["*"], "10.1.2.3")],
)
def test_ceiling_must_name_non_public_destinations_exactly(
    allow_hosts: list[str], host: str
) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: exact
              operation: http
              match:
                hosts: ["{host}"]
              action: allow
        """
    )
    ceiling = make_ceiling({"network": {"allow_hosts": allow_hosts}})
    decision = decide_http(policy, "GET", f"https://{host}/", ceiling=ceiling)
    assert decision.action == Action.DENY
    assert decision.layer == "ceiling"


@pytest.mark.parametrize("host", ["db.internal", "10.1.2.3"])
def test_ceiling_naming_a_non_public_destination_exactly_admits_it(host: str) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: exact
              operation: http
              match:
                hosts: ["{host}"]
              action: allow
        """
    )
    ceiling = make_ceiling({"network": {"allow_hosts": [host]}})
    assert decide_http(policy, "GET", f"https://{host}/", ceiling=ceiling).action == Action.ALLOW


def test_ceiling_denial_overrides_a_project_escalation() -> None:
    policy = load("defaults: {action: escalate}")
    ceiling = make_ceiling({"network": {"deny_hosts": ["example.com"]}})
    decision = decide_http(policy, "POST", "https://example.com/upload", ceiling=ceiling)
    assert decision.action == Action.DENY
    assert decision.layer == "ceiling"
    assert decision.escalation is None


def test_ceiling_applies_to_git_operations() -> None:
    policy = load(
        """
        network:
          rules:
            - name: fetch
              operation: git
              match: {hosts: [git.example.org], git_service: upload-pack}
              action: allow
        """
    )
    ceiling = make_ceiling({"network": {"deny_hosts": ["git.example.org"]}})
    decision = decide_http(policy, "POST", FETCH, ceiling=ceiling)
    assert decision.action == Action.DENY
    assert decision.layer == "ceiling"


def test_ceiling_cannot_widen_the_floor() -> None:
    policy = load(
        """
        network:
          rules:
            - {name: exact, operation: http, match: {hosts: [localhost]}, action: allow}
        """
    )
    ceiling = make_ceiling({"network": {"allow_hosts": ["localhost"]}})
    decision = decide_http(policy, "GET", "https://localhost/", ceiling=ceiling)
    assert decision.action == Action.DENY
    assert decision.layer == "floor"


def test_floor_is_applied_before_the_ceiling() -> None:
    ceiling = make_ceiling({"network": {"deny_hosts": [ADMIN_HOST]}})
    decision = decide_http(
        load(), "GET", f"https://{ADMIN_HOST}/", ceiling=ceiling, floor=admin_floor()
    )
    assert decision.action == Action.DENY
    assert decision.layer == "floor"


@pytest.mark.parametrize(("method", "tool"), [("initialize", None), ("tools/call", "lookup")])
def test_ceiling_limits_the_mcp_servers_a_pool_may_reach(method: str, tool: str | None) -> None:
    policy = load(
        """
        mcp:
          servers:
            search:
              url: https://mcp.example.org/search
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*"], escalate: [], deny: []}
            papers:
              url: https://mcp.example.org/papers
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*"], escalate: [], deny: []}
        """
    )
    ceiling = make_ceiling({"mcp_servers": ["search"]})
    unlisted = decide_mcp(policy, method, tool, server="papers", ceiling=ceiling)
    listed = decide_mcp(policy, method, tool, server="search", ceiling=ceiling)
    assert unlisted.action == Action.DENY
    assert unlisted.layer == "ceiling"
    assert listed.action == Action.ALLOW


def test_empty_ceiling_mcp_server_list_reaches_no_server() -> None:
    policy = load(
        """
        mcp:
          servers:
            search:
              url: https://mcp.example.org/search
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*"], escalate: [], deny: []}
        """
    )
    ceiling = make_ceiling({"mcp_servers": []})
    decision = decide_mcp(policy, "tools/call", "lookup", ceiling=ceiling)
    assert decision.action == Action.DENY
    assert decision.layer == "ceiling"


def test_resource_requests_above_a_cap_are_reduced_and_reported() -> None:
    policy = load("resources: {cpu: 16, pids: 4096}")
    ceiling = make_ceiling({"resources": {"cpu": 8, "pids": 8192}})
    effective, notices = effective_resources(policy.policy, ceiling)
    assert effective.cpu == 8
    assert effective.pids == 4096
    assert len(notices) == 1
    assert "cpu" in notices[0]


def test_resource_requests_within_the_ceiling_are_unchanged() -> None:
    policy = load("resources: {cpu: 16, pids: 4096}")
    effective, notices = effective_resources(policy.policy, PoolCeiling.unrestricted())
    assert effective.cpu == 16
    assert effective.pids == 4096
    assert notices == ()


# --- Rule matching: hosts, ports, paths, methods ------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "url", "matches"),
    [
        ("example.org", "https://example.org/", True),
        ("example.org", "https://EXAMPLE.ORG/", True),
        ("example.org", "https://example.org./", True),
        ("Example.ORG", "https://example.org/", True),
        ("example.org.", "https://example.org/", True),
        ("example.org", "https://a.example.org/", False),
        ("example.org", "https://badexample.org/", False),
        ("example.org", "https://example.org.example.com/", False),
        ("example.org", "https://example.org../", False),
        ("*.example.org", "https://a.example.org/", True),
        ("*.example.org", "https://a.b.example.org/", True),
        ("*.example.org", "https://A.Example.Org./", True),
        ("*.example.org", "https://example.org/", False),
        ("*.example.org", "https://aexample.org/", False),
        ("*.example.org", "https://a.example.org.example.com/", False),
        ("*", "https://example.org/", True),
        ("*", "https://a.b.example.com/", True),
        ("*", "https://8.8.8.8/", True),
        ("8.8.8.8", "https://8.8.8.8/", True),
        ("8.8.8.8", "https://8.8.4.4/", False),
    ],
)
def test_host_patterns(pattern: str, url: str, matches: bool) -> None:
    policy = load(
        f"""
        defaults:
          action: deny
        network:
          rules:
            - name: rule
              operation: http
              match:
                hosts: ["{pattern}"]
              action: allow
        """
    )
    decision = decide_http(policy, "GET", url)
    assert decision.action == (Action.ALLOW if matches else Action.DENY)


@pytest.mark.parametrize(
    ("url", "matches"),
    [
        ("https://example.org/", True),
        ("https://example.org:443/", True),
        ("http://example.org/", True),
        ("http://example.org:80/", True),
        ("https://example.org:8443/", False),
        ("http://example.org:8080/", False),
        ("http://example.org:443/", False),
        ("https://example.org:80/", False),
    ],
)
def test_rule_without_ports_matches_only_the_scheme_default_port(url: str, matches: bool) -> None:
    policy = load(
        """
        defaults: {action: deny}
        network:
          rules:
            - {name: default-port, operation: http, match: {hosts: [example.org]}, action: allow}
        """
    )
    assert decide_http(policy, "GET", url).action == (Action.ALLOW if matches else Action.DENY)


@pytest.mark.parametrize(
    ("url", "matches"),
    [
        ("https://example.org:8443/", True),
        ("http://example.org:8443/", True),
        ("https://example.org/", False),
        ("https://example.org:9443/", False),
    ],
)
def test_rule_with_ports_matches_only_the_listed_ports(url: str, matches: bool) -> None:
    policy = load(
        """
        defaults: {action: deny}
        network:
          rules:
            - name: alt-port
              operation: http
              match: {hosts: [example.org], ports: [8443]}
              action: allow
        """
    )
    assert decide_http(policy, "GET", url).action == (Action.ALLOW if matches else Action.DENY)


@pytest.mark.parametrize(
    ("path", "matches"),
    [
        ("/v1/search", True),
        ("/v1/search/x", True),
        ("/v1/search?q=lattice", True),
        ("/v1/%73earch", True),
        ("/docs", True),
        ("/docs/guide", True),
        ("/v1/searchx", False),
        ("/v1", False),
        ("/V1/search", False),
        ("/v2/v1/search", False),
        ("/docsx", False),
        ("/v1/other?next=/v1/search", False),
    ],
)
def test_path_prefixes_match_whole_decoded_segments(path: str, matches: bool) -> None:
    policy = load(
        """
        defaults: {action: deny}
        network:
          rules:
            - name: paths
              operation: http
              match: {hosts: [api.example.org], paths: [/v1/search, /docs]}
              action: allow
        """
    )
    decision = decide_http(policy, "GET", f"https://api.example.org{path}")
    assert decision.action == (Action.ALLOW if matches else Action.DENY)


@pytest.mark.parametrize(
    ("method", "matches"),
    [("GET", True), ("HEAD", True), ("get", False), ("POST", False), ("DELETE", False)],
)
def test_methods_must_be_listed(method: str, matches: bool) -> None:
    policy = load(
        """
        defaults: {action: deny}
        network:
          rules:
            - name: reads
              operation: http
              match: {hosts: ["*"], methods: [GET, HEAD]}
              action: allow
        """
    )
    decision = decide_http(policy, method, "https://example.org/")
    assert decision.action == (Action.ALLOW if matches else Action.DENY)


@pytest.mark.parametrize(
    ("method", "url", "matches"),
    [
        ("GET", FETCH_DISCOVERY, True),
        ("POST", FETCH, True),
        ("GET", PUSH_DISCOVERY, False),
        ("POST", PUSH, False),
    ],
)
def test_git_service_must_match(method: str, url: str, matches: bool) -> None:
    policy = load(
        """
        defaults: {action: deny}
        network:
          rules:
            - name: fetch
              operation: git
              match: {hosts: [git.example.org], git_service: upload-pack}
              action: allow
        """
    )
    decision = decide_http(policy, method, url)
    assert decision.action == (Action.ALLOW if matches else Action.DENY)


# --- Rule selection: specificity, ties and defaults ----------------------------------------------


@pytest.mark.parametrize(
    ("exact", "wildcard"),
    [(Action.ALLOW, Action.DENY), (Action.DENY, Action.ALLOW), (Action.ALERT, Action.ESCALATE)],
)
def test_exact_host_beats_wildcard_host(exact: Action, wildcard: Action) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: wildcard
              operation: http
              match:
                hosts: ["*.example.org"]
              action: {wildcard}
            - name: exact
              operation: http
              match:
                hosts: [a.example.org]
              action: {exact}
        """
    )
    decision = decide_http(policy, "GET", "https://a.example.org/")
    assert decision.action == exact
    assert decision.rule == "exact"
    assert decision.layer == "policy"


@pytest.mark.parametrize(
    ("wildcard", "star"), [(Action.ALLOW, Action.DENY), (Action.DENY, Action.ALLOW)]
)
def test_wildcard_host_beats_star(wildcard: Action, star: Action) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: star
              operation: http
              match:
                hosts: ["*"]
              action: {star}
            - name: wildcard
              operation: http
              match:
                hosts: ["*.example.org"]
              action: {wildcard}
        """
    )
    decision = decide_http(policy, "GET", "https://a.example.org/")
    assert decision.action == wildcard
    assert decision.rule == "wildcard"


@pytest.mark.parametrize(
    ("longer", "shorter"), [(Action.ALLOW, Action.DENY), (Action.DENY, Action.ALLOW)]
)
def test_more_labels_after_the_wildcard_is_more_specific(longer: Action, shorter: Action) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: shorter
              operation: http
              match:
                hosts: ["*.example.org"]
              action: {shorter}
            - name: longer
              operation: http
              match:
                hosts: ["*.b.example.org"]
              action: {longer}
        """
    )
    deeper = decide_http(policy, "GET", "https://a.b.example.org/")
    assert deeper.action == longer
    assert deeper.rule == "longer"
    assert decide_http(policy, "GET", "https://c.example.org/").rule == "shorter"


def test_exact_host_outranks_a_wildcard_rule_with_path_and_methods() -> None:
    policy = load(
        """
        network:
          rules:
            - name: broad
              operation: http
              match: {hosts: ["*"], paths: [/v1/search], methods: [POST]}
              action: allow
            - name: exact
              operation: http
              match: {hosts: [api.example.org]}
              action: escalate
        """
    )
    decision = decide_http(policy, "POST", "https://api.example.org/v1/search")
    assert decision.action == Action.ESCALATE
    assert decision.rule == "exact"


def test_wildcard_labels_outrank_path_and_methods() -> None:
    policy = load(
        """
        network:
          rules:
            - name: shorter-with-path
              operation: http
              match: {hosts: ["*.example.org"], paths: [/x/y], methods: [GET]}
              action: deny
            - name: longer
              operation: http
              match: {hosts: ["*.b.example.org"]}
              action: allow
        """
    )
    decision = decide_http(policy, "GET", "https://a.b.example.org/x/y")
    assert decision.action == Action.ALLOW
    assert decision.rule == "longer"


def test_longer_path_prefix_wins() -> None:
    policy = load(
        """
        network:
          rules:
            - name: v1
              operation: http
              match: {hosts: [api.example.org], paths: [/v1]}
              action: deny
            - name: search
              operation: http
              match: {hosts: [api.example.org], paths: [/v1/search]}
              action: allow
        """
    )
    longer = decide_http(policy, "GET", "https://api.example.org/v1/search/x")
    assert longer.action == Action.ALLOW
    assert longer.rule == "search"
    assert decide_http(policy, "GET", "https://api.example.org/v1/other").action == Action.DENY


def test_path_specificity_counts_the_prefix_that_matched() -> None:
    policy = load(
        """
        network:
          rules:
            - name: mixed
              operation: http
              match: {hosts: [api.example.org], paths: [/v1, /v2/deep/nested/path]}
              action: deny
            - name: search
              operation: http
              match: {hosts: [api.example.org], paths: [/v1/search]}
              action: allow
        """
    )
    decision = decide_http(policy, "GET", "https://api.example.org/v1/search")
    assert decision.action == Action.ALLOW
    assert decision.rule == "search"


def test_path_outranks_methods() -> None:
    policy = load(
        """
        network:
          rules:
            - name: posts
              operation: http
              match: {hosts: [api.example.org], methods: [POST]}
              action: escalate
            - name: search
              operation: http
              match: {hosts: [api.example.org], paths: [/v1/search]}
              action: allow
        """
    )
    decision = decide_http(policy, "POST", "https://api.example.org/v1/search")
    assert decision.action == Action.ALLOW
    assert decision.rule == "search"


def test_naming_methods_is_more_specific_than_not() -> None:
    policy = load(
        """
        network:
          rules:
            - name: any-method
              operation: http
              match: {hosts: [api.example.org], paths: [/v1]}
              action: deny
            - name: post
              operation: http
              match: {hosts: [api.example.org], paths: [/v1], methods: [POST]}
              action: allow
        """
    )
    post = decide_http(policy, "POST", "https://api.example.org/v1/x")
    assert post.action == Action.ALLOW
    assert post.rule == "post"
    assert decide_http(policy, "GET", "https://api.example.org/v1/x").action == Action.DENY


@pytest.mark.parametrize(
    ("few", "many", "expected_rule"),
    [(Action.ALLOW, Action.DENY, "many"), (Action.DENY, Action.ALLOW, "few")],
)
def test_the_number_of_methods_does_not_affect_specificity(
    few: Action, many: Action, expected_rule: str
) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: few
              operation: http
              match: {{hosts: [example.org], methods: [GET]}}
              action: {few}
            - name: many
              operation: http
              match: {{hosts: [example.org], methods: [GET, HEAD, POST]}}
              action: {many}
        """
    )
    decision = decide_http(policy, "GET", "https://example.org/")
    assert decision.action == Action.DENY
    assert decision.rule == expected_rule


def test_naming_a_git_service_is_more_specific_than_not() -> None:
    policy = load(
        """
        network:
          rules:
            - {name: any-service, operation: git, match: {hosts: ["*"]}, action: deny}
            - name: fetch
              operation: git
              match: {hosts: ["*"], git_service: upload-pack}
              action: allow
        """
    )
    fetch = decide_http(policy, "POST", FETCH)
    assert fetch.action == Action.ALLOW
    assert fetch.rule == "fetch"
    assert decide_http(policy, "POST", PUSH).action == Action.DENY


@pytest.mark.parametrize(
    ("ported", "portless", "expected_rule"),
    [(Action.DENY, Action.ALLOW, "ported"), (Action.ALLOW, Action.DENY, "portless")],
)
def test_ports_do_not_make_a_rule_more_specific(
    ported: Action, portless: Action, expected_rule: str
) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: portless
              operation: http
              match: {{hosts: [example.org]}}
              action: {portless}
            - name: ported
              operation: http
              match: {{hosts: [example.org], ports: [443]}}
              action: {ported}
        """
    )
    decision = decide_http(policy, "GET", "https://example.org/")
    assert decision.action == Action.DENY
    assert decision.rule == expected_rule


@pytest.mark.parametrize(
    ("first", "second", "winner"),
    [
        (Action.ALLOW, Action.ALERT, Action.ALERT),
        (Action.ALERT, Action.ALLOW, Action.ALERT),
        (Action.ALERT, Action.ESCALATE, Action.ESCALATE),
        (Action.ESCALATE, Action.ALLOW, Action.ESCALATE),
        (Action.ESCALATE, Action.DENY, Action.DENY),
        (Action.DENY, Action.ALLOW, Action.DENY),
    ],
)
def test_ties_go_to_the_most_restrictive_action(
    first: Action, second: Action, winner: Action
) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: first
              operation: http
              match: {{hosts: [example.org], methods: [GET]}}
              action: {first}
            - name: second
              operation: http
              match: {{hosts: [example.org], methods: [GET]}}
              action: {second}
        """
    )
    decision = decide_http(policy, "GET", "https://example.org/")
    assert decision.action == winner
    assert decision.rule == ("first" if first == winner else "second")


def test_narrow_post_allow_overrides_a_broad_write_escalation() -> None:
    policy = load(
        """
        network:
          rules:
            - name: external-writes
              operation: http
              match: {hosts: ["*"], methods: [POST, PUT, PATCH, DELETE]}
              action: escalate
            - name: search-api
              operation: http
              match: {hosts: [api.example.org], paths: [/v1/search], methods: [POST]}
              action: allow
        """
    )
    assert decide_http(policy, "POST", "https://api.example.org/v1/search").action == Action.ALLOW
    assert decide_http(policy, "POST", "https://api.example.org/v1/upload").action == (
        Action.ESCALATE
    )
    assert decide_http(policy, "POST", "https://example.com/v1/search").action == Action.ESCALATE


def test_narrow_deny_overrides_a_broad_allow() -> None:
    policy = load(
        """
        network:
          rules:
            - name: reads
              operation: http
              match: {hosts: ["*"], methods: [GET, HEAD]}
              action: allow
            - name: admin
              operation: http
              match: {hosts: [example.org], paths: [/admin]}
              action: deny
        """
    )
    blocked = decide_http(policy, "GET", "https://example.org/admin/users")
    assert blocked.action == Action.DENY
    assert blocked.rule == "admin"
    assert decide_http(policy, "GET", "https://example.org/docs").action == Action.ALLOW


@pytest.mark.parametrize("action", list(Action))
def test_unmatched_operations_take_defaults_action(action: Action) -> None:
    policy = load(
        f"""
        defaults:
          action: {action}
        network:
          rules:
            - name: elsewhere
              operation: http
              match:
                hosts: [example.com]
              action: allow
        """
    )
    decision = decide_http(policy, "GET", "https://example.org/")
    assert decision.action == action
    assert decision.layer == "default"
    assert decision.rule is None


# --- Operation classes ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url"), [("GET", FETCH_DISCOVERY), ("POST", FETCH), ("POST", PUSH)]
)
def test_http_rules_never_see_git_operations(method: str, url: str) -> None:
    policy = load(
        """
        defaults: {action: deny}
        network:
          rules:
            - name: all-http
              operation: http
              match: {hosts: ["*"], methods: [GET, POST]}
              action: allow
        """
    )
    decision = decide_http(policy, method, url)
    assert decision.action == Action.DENY
    assert decision.layer == "default"


@pytest.mark.parametrize(
    ("method", "url"), [("GET", f"{REPO}/info/refs"), ("POST", "https://git.example.org/api")]
)
def test_git_rules_never_see_http_operations(method: str, url: str) -> None:
    policy = load(
        """
        defaults: {action: deny}
        network:
          rules:
            - {name: all-git, operation: git, match: {hosts: ["*"]}, action: allow}
        """
    )
    decision = decide_http(policy, method, url)
    assert decision.action == Action.DENY
    assert decision.layer == "default"


def test_git_fetch_post_is_not_caught_by_an_http_post_rule() -> None:
    policy = load(
        """
        network:
          rules:
            - name: http-posts
              operation: http
              match: {hosts: ["*"], methods: [POST]}
              action: escalate
            - name: fetch
              operation: git
              match: {hosts: [git.example.org], git_service: upload-pack}
              action: allow
        """
    )
    fetch = decide_http(policy, "POST", FETCH)
    assert fetch.action == Action.ALLOW
    assert fetch.rule == "fetch"
    assert decide_http(policy, "POST", "https://git.example.org/api").rule == "http-posts"


def test_push_discovery_is_governed_by_push_rules_not_http_reads() -> None:
    policy = load(
        """
        network:
          rules:
            - {name: reads, operation: http, match: {hosts: ["*"], methods: [GET]}, action: allow}
            - name: push
              operation: git
              match: {hosts: ["*"], git_service: receive-pack}
              action: escalate
        """
    )
    decision = decide_http(policy, "GET", PUSH_DISCOVERY)
    assert decision.action == Action.ESCALATE
    assert decision.rule == "push"


def test_dumb_http_git_reads_are_governed_by_http_rules() -> None:
    policy = load(
        """
        network:
          rules:
            - {name: reads, operation: http, match: {hosts: ["*"], methods: [GET]}, action: allow}
            - {name: no-git, operation: git, match: {hosts: ["*"]}, action: deny}
        """
    )
    decision = decide_http(policy, "GET", f"{REPO}/info/refs")
    assert decision.action == Action.ALLOW
    assert decision.rule == "reads"


def test_http_rules_never_see_mcp_operations() -> None:
    policy = load(
        """
        defaults: {action: deny}
        network:
          rules:
            - name: block-mcp-host
              operation: http
              match: {hosts: [mcp.example.org]}
              action: deny
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*search*"], escalate: [], deny: []}
        """
    )
    assert call_tool(policy, "web_search").action == Action.ALLOW
    assert decide_mcp(policy, "tools/list").action == Action.ALLOW


def test_http_rules_never_see_package_operations() -> None:
    policy = load(
        """
        packages:
          pypi: {action: allow}
        network:
          rules:
            - {name: block-everything, operation: http, match: {hosts: ["*"]}, action: deny}
        """
    )
    assert decide_package(policy, "pypi", "requests").action == Action.ALLOW


@pytest.mark.parametrize(
    ("method", "url", "rule"), [("GET", "https://example.org/", "reads"), ("POST", FETCH, "fetch")]
)
def test_evaluate_accepts_classified_http_and_git_operations(
    method: str, url: str, rule: str
) -> None:
    policy = load(
        """
        network:
          rules:
            - name: reads
              operation: http
              match: {hosts: [example.org], methods: [GET]}
              action: allow
            - name: fetch
              operation: git
              match: {hosts: [git.example.org], git_service: upload-pack}
              action: allow
        """
    )
    operation = classify_http_request(client_request(method, url))
    decision = evaluate(operation, policy, ceiling=PoolCeiling.unrestricted())
    assert decision.action == Action.ALLOW
    assert decision.rule == rule


# --- Non-public destinations ---------------------------------------------------------------------

NON_PUBLIC_HOSTS = [
    pytest.param("10.1.2.3", id="private-10"),
    pytest.param("172.16.5.4", id="private-172"),
    pytest.param("192.168.1.10", id="private-192"),
    pytest.param("100.64.0.1", id="shared-cgnat"),
    pytest.param("224.0.0.251", id="multicast"),
    pytest.param("239.255.255.250", id="multicast-admin-scope"),
    pytest.param("240.0.0.1", id="reserved"),
    pytest.param("255.255.255.255", id="broadcast"),
    pytest.param("198.18.0.1", id="benchmarking"),
    pytest.param("192.0.2.10", id="documentation-1"),
    pytest.param("198.51.100.10", id="documentation-2"),
    pytest.param("203.0.113.10", id="documentation-3"),
    pytest.param("[fd12:3456::1]", id="v6-unique-local"),
    pytest.param("[2001:db8::10]", id="v6-documentation"),
    pytest.param("[ff02::fb]", id="v6-multicast"),
    pytest.param("[::ffff:10.1.2.3]", id="v4-mapped-private"),
    pytest.param("[::ffff:cb00:710a]", id="v4-mapped-documentation-hex"),
    pytest.param("[64:ff9b::a01:203]", id="nat64-embedded-private"),
    pytest.param("db.local", id="local"),
    pytest.param("api.internal", id="internal"),
    pytest.param("printer.home.arpa", id="home-arpa"),
    pytest.param("nas.lan", id="lan"),
    pytest.param("router.home", id="home"),
    pytest.param("wiki.corp", id="corp"),
    pytest.param("api.default.svc", id="svc"),
    pytest.param("site.test", id="test"),
    pytest.param("www.example", id="example"),
    pytest.param("host.invalid", id="invalid"),
    pytest.param("A.B.Internal.", id="internal-mixed-case-trailing-dot"),
]


@pytest.mark.parametrize("action", [Action.ALLOW, Action.ALERT])
@pytest.mark.parametrize("host", NON_PUBLIC_HOSTS)
def test_star_rules_never_grant_non_public_destinations(host: str, action: Action) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: anywhere
              operation: http
              match:
                hosts: ["*"]
              action: {action}
        """
    )
    assert decide_http(policy, "GET", f"https://{host}/").action == Action.DENY


@pytest.mark.parametrize(
    ("pattern", "host"),
    [("*.internal", "api.internal"), ("*.home.arpa", "printer.home.arpa"), ("*.lan", "nas.lan")],
)
def test_suffix_wildcards_never_grant_non_public_names(pattern: str, host: str) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: suffix
              operation: http
              match:
                hosts: ["{pattern}"]
              action: allow
        """
    )
    assert decide_http(policy, "GET", f"https://{host}/").action == Action.DENY


@pytest.mark.parametrize("action", [Action.ALLOW, Action.ALERT])
@pytest.mark.parametrize("host", ["10.1.2.3", "203.0.113.10", "db.internal", "[fd12:3456::1]"])
def test_defaults_never_grant_non_public_destinations(host: str, action: Action) -> None:
    policy = load(
        f"""
        defaults:
          action: {action}
        """
    )
    assert decide_http(policy, "GET", f"https://{host}/").action == Action.DENY


@pytest.mark.parametrize("action", [Action.ESCALATE, Action.DENY])
def test_escalate_and_deny_outcomes_are_unchanged_for_non_public_destinations(
    action: Action,
) -> None:
    by_wildcard = load(
        f"""
        network:
          rules:
            - name: anywhere
              operation: http
              match:
                hosts: ["*"]
              action: {action}
        """
    )
    by_default = load(
        f"""
        defaults:
          action: {action}
        """
    )
    assert decide_http(by_wildcard, "GET", "https://10.1.2.3/").action == action
    assert decide_http(by_default, "GET", "https://db.internal/").action == action


@pytest.mark.parametrize(
    ("host", "action"),
    [
        ("10.1.2.3", Action.ALLOW),
        ("203.0.113.10", Action.ALERT),
        ("db.internal", Action.ALLOW),
        ("printer.home.arpa", Action.ALERT),
    ],
)
def test_exact_rules_can_grant_non_public_destinations(host: str, action: Action) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: exact
              operation: http
              match:
                hosts: ["{host}"]
              action: {action}
        """
    )
    decision = decide_http(policy, "GET", f"https://{host}/")
    assert decision.action == action
    assert decision.rule == "exact"


def test_a_rule_naming_the_host_exactly_beside_a_wildcard_grants_it() -> None:
    policy = load(
        """
        network:
          rules:
            - {name: mixed, operation: http, match: {hosts: ["*", db.internal]}, action: allow}
        """
    )
    assert decide_http(policy, "GET", "https://db.internal/").action == Action.ALLOW
    assert decide_http(policy, "GET", "https://cache.internal/").action == Action.DENY


def test_a_converted_wildcard_allow_does_not_fall_back_to_a_broader_rule() -> None:
    policy = load(
        """
        network:
          rules:
            - {name: broad, operation: http, match: {hosts: ["*"]}, action: escalate}
            - {name: suffix, operation: http, match: {hosts: ["*.internal"]}, action: allow}
        """
    )
    assert decide_http(policy, "GET", "https://db.internal/").action == Action.DENY


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/",
        "https://8.8.8.8/",
        "https://1.1.1.1/",
        "https://[2001:4860:4860::8888]/",
        "https://local.example.org/",
        "https://internal.example.com/",
        "https://test.example.org/",
    ],
)
def test_wildcards_still_grant_public_destinations(url: str) -> None:
    policy = load(
        """
        network:
          rules:
            - {name: anywhere, operation: http, match: {hosts: ["*"]}, action: allow}
        """
    )
    assert decide_http(policy, "GET", url).action == Action.ALLOW


def test_wildcard_git_rules_never_grant_non_public_destinations() -> None:
    url = "https://git.internal/team/repo.git/info/refs?service=git-upload-pack"
    by_wildcard = load(
        """
        network:
          rules:
            - name: fetch
              operation: git
              match: {hosts: ["*"], git_service: upload-pack}
              action: allow
        """
    )
    by_name = load(
        """
        network:
          rules:
            - name: fetch
              operation: git
              match: {hosts: [git.internal], git_service: upload-pack}
              action: allow
        """
    )
    assert decide_http(by_wildcard, "GET", url).action == Action.DENY
    assert decide_http(by_name, "GET", url).action == Action.ALLOW


# --- Rule limits ---------------------------------------------------------------------------------


def test_url_at_the_limit_is_allowed_and_one_byte_more_is_denied() -> None:
    policy = load(
        """
        network:
          rules:
            - name: reads
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              limits: {max_url_bytes: 64}
              action: allow
        """
    )
    base = "https://example.org/search?q="
    at_limit = base + "a" * (64 - len(base))
    over = decide_http(policy, "GET", at_limit + "a")
    assert decide_http(policy, "GET", at_limit).action == Action.ALLOW
    assert over.action == Action.DENY
    assert over.rule == "reads"


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (Action.ALLOW, Action.DENY),
        (Action.ALERT, Action.DENY),
        (Action.ESCALATE, Action.ESCALATE),
        (Action.DENY, Action.DENY),
    ],
)
def test_url_limit_denies_only_allow_and_alert_outcomes(action: Action, expected: Action) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: limited
              operation: http
              match:
                hosts: ["*"]
              limits:
                max_url_bytes: 64
              action: {action}
        """
    )
    decision = decide_http(policy, "GET", "https://example.org/?q=" + "a" * 100)
    assert decision.action == expected


def test_url_limit_denial_does_not_fall_through_to_another_rule() -> None:
    policy = load(
        """
        defaults: {action: allow}
        network:
          rules:
            - name: limited-reads
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              limits: {max_url_bytes: 64}
              action: allow
            - {name: any-method, operation: http, match: {hosts: ["*"]}, action: alert}
        """
    )
    decision = decide_http(policy, "GET", "https://example.org/?q=" + "a" * 100)
    assert decision.action == Action.DENY
    assert decision.rule == "limited-reads"


def test_url_limit_of_a_rule_that_does_not_win_does_not_apply() -> None:
    policy = load(
        """
        network:
          rules:
            - name: limited
              operation: http
              match: {hosts: ["*"]}
              limits: {max_url_bytes: 64}
              action: allow
            - {name: api, operation: http, match: {hosts: [api.example.org]}, action: allow}
        """
    )
    decision = decide_http(policy, "GET", "https://api.example.org/?q=" + "a" * 100)
    assert decision.action == Action.ALLOW
    assert decision.rule == "api"


# --- Git -----------------------------------------------------------------------------------------


def test_git_fetch_is_allowed_only_from_listed_hosts() -> None:
    policy = load(
        """
        network:
          rules:
            - name: git-fetch
              operation: git
              match: {hosts: [git.example.org, codeberg.org], git_service: upload-pack}
              action: allow
        """
    )
    other_host = "https://gitlab.example.com/team/repo.git/git-upload-pack"
    assert decide_http(policy, "POST", FETCH).action == Action.ALLOW
    assert decide_http(policy, "POST", other_host).action == Action.ESCALATE


def test_git_push_escalates_while_fetch_is_allowed() -> None:
    policy = load(
        """
        network:
          rules:
            - name: git-fetch
              operation: git
              match: {hosts: [git.example.org], git_service: upload-pack}
              action: allow
            - name: git-push
              operation: git
              match: {hosts: ["*"], git_service: receive-pack}
              action: escalate
        """
    )
    assert decide_http(policy, "GET", FETCH_DISCOVERY).action == Action.ALLOW
    assert decide_http(policy, "POST", FETCH).action == Action.ALLOW
    assert decide_http(policy, "GET", PUSH_DISCOVERY).action == Action.ESCALATE
    assert decide_http(policy, "POST", PUSH).action == Action.ESCALATE


def test_git_rule_paths_match_repository_prefixes() -> None:
    policy = load(
        """
        defaults: {action: deny}
        network:
          rules:
            - name: team-repos
              operation: git
              match: {hosts: [git.example.org], paths: [/team], git_service: upload-pack}
              action: allow
        """
    )
    other_repo = "https://git.example.org/other/repo.git/git-upload-pack"
    assert decide_http(policy, "POST", FETCH).action == Action.ALLOW
    assert decide_http(policy, "POST", other_repo).action == Action.DENY


# --- MCP -----------------------------------------------------------------------------------------

PROTOCOL_METHODS = [
    "initialize",
    "ping",
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
    "notifications/roots/list_changed",
    "tools/list",
    "resources/list",
    "resources/templates/list",
    "resources/read",
    "resources/subscribe",
    "resources/unsubscribe",
    "prompts/list",
    "prompts/get",
    "completion/complete",
    "logging/setLevel",
]

OTHER_METHODS = [
    "tasks/get",
    "tasks/list",
    "tasks/result",
    "tasks/cancel",
    "notifications/tasks/status",
    "sampling/createMessage",
    "elicitation/create",
    "roots/list",
    "notifications/message",
    "notifications/tools/list_changed",
    "resources/write",
    "x-vendor/rebuild-index",
    "Tools/List",
    "tools/list ",
]


def test_calls_to_undeclared_servers_are_denied() -> None:
    policy = load(
        """
        defaults: {action: allow}
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*"], escalate: [], deny: []}
        """
    )
    assert decide_mcp(policy, "initialize", server="unknown").action == Action.DENY
    assert decide_mcp(policy, "tools/call", "web_search", server="unknown").action == Action.DENY
    assert decide_mcp(policy, "tools/call", "web_search", server="search").action == Action.ALLOW


def test_mcp_server_urls_must_use_https() -> None:
    text = """
        defaults: {action: allow}
        mcp:
          servers:
            search:
              url: http://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*"], escalate: [], deny: []}
        """
    try:
        policy = load(text)
    except PolicyLoadError:
        return  # Rejecting the server when loading is the expected outcome.
    assert call_tool(policy, "web_search").action == Action.DENY


@pytest.mark.parametrize("method", PROTOCOL_METHODS)
def test_protocol_and_discovery_methods_are_allowed(method: str) -> None:
    policy = load(
        """
        defaults: {action: deny}
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: [], escalate: [], deny: ["*"]}
        """
    )
    assert decide_mcp(policy, method).action == Action.ALLOW


@pytest.mark.parametrize("method", OTHER_METHODS)
def test_all_other_methods_are_denied(method: str) -> None:
    policy = load(
        """
        defaults: {action: allow}
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*"], escalate: [], deny: []}
        """
    )
    assert decide_mcp(policy, method).action == Action.DENY


@pytest.mark.parametrize(
    ("exact_list", "pattern_list", "expected"),
    [
        ("allow", "deny", Action.ALLOW),
        ("allow", "escalate", Action.ALLOW),
        ("escalate", "deny", Action.ESCALATE),
        ("deny", "allow", Action.DENY),
    ],
)
def test_exact_tool_name_takes_precedence_over_patterns(
    exact_list: str, pattern_list: str, expected: Action
) -> None:
    lists: dict[str, list[str]] = {"allow": [], "escalate": [], "deny": []}
    lists[exact_list].append("search_feedback")
    lists[pattern_list].append("*feedback*")
    policy = load(
        f"""
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {{mode: none}}
              tools: {json.dumps(lists)}
        """
    )
    decision = call_tool(policy, "search_feedback")
    assert decision.action == expected
    assert decision.rule is not None
    assert exact_list in decision.rule
    assert "search_feedback" in decision.rule


@pytest.mark.parametrize(
    ("tools", "tool", "expected"),
    [
        (
            '{allow: ["*search*"], escalate: ["*feedback*"], deny: []}',
            "search_feedback",
            "escalate",
        ),
        ('{allow: ["*search*"], escalate: [], deny: ["*feedback*"]}', "search_feedback", "deny"),
        ('{allow: [], escalate: ["*search*"], deny: ["*feedback*"]}', "search_feedback", "deny"),
        ('{allow: ["*search*", "search_*"], escalate: [], deny: []}', "search_feedback", "allow"),
        ('{allow: ["*"], escalate: ["*feedback*"], deny: ["admin_*"]}', "admin_feedback", "deny"),
    ],
)
def test_most_restrictive_matching_pattern_list_wins(tools: str, tool: str, expected: str) -> None:
    policy = load(
        f"""
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {{mode: none}}
              tools: {tools}
        """
    )
    assert call_tool(policy, tool).action == Action(expected)


def test_tool_decisions_name_the_list_and_pattern() -> None:
    policy = load(
        """
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*search*"], escalate: [], deny: ["*reset*"]}
        """
    )
    decision = call_tool(policy, "index_reset")
    assert decision.action == Action.DENY
    assert decision.layer == "policy"
    assert decision.rule is not None
    assert "deny" in decision.rule
    assert "*reset*" in decision.rule


@pytest.mark.parametrize("action", list(Action))
def test_unmatched_tools_take_defaults_action(action: Action) -> None:
    policy = load(
        f"""
        defaults:
          action: {action}
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth:
                mode: none
              tools:
                allow: ["*search*"]
                escalate: []
                deny: []
        """
    )
    decision = call_tool(policy, "crawl_site")
    assert decision.action == action
    assert decision.layer == "default"


@pytest.mark.parametrize(
    ("pattern", "tool", "matches"),
    [
        ("search", "search", True),
        ("search", "search_docs", False),
        ("search", "web_search", False),
        ("*search", "web_search", True),
        ("*search", "search_docs", False),
        ("search*", "search_docs", True),
        ("search*", "search", True),
        ("search*", "web_search", False),
        ("*search*", "web_search_docs", True),
        ("a*b*c", "a_x_b_y_c", True),
        ("a*b*c", "a_x_c", False),
        ("*", "anything", True),
        ("get.data", "get.data", True),
        ("get.data", "get_data", False),
    ],
)
def test_tool_patterns_match_the_whole_name_with_star_as_the_only_wildcard(
    pattern: str, tool: str, matches: bool
) -> None:
    policy = load(
        f"""
        defaults:
          action: deny
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth:
                mode: none
              tools:
                allow: ["{pattern}"]
                escalate: []
                deny: []
        """
    )
    assert call_tool(policy, tool).action == (Action.ALLOW if matches else Action.DENY)


@pytest.mark.parametrize(
    ("pattern", "tool"),
    [("get_?", "get_x"), ("[gs]et_data", "get_data"), ("get_(data)", "get_data")],
)
def test_glob_and_regex_metacharacters_are_not_wildcards(pattern: str, tool: str) -> None:
    text = f"""
        defaults:
          action: deny
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth:
                mode: none
              tools:
                allow: ["{pattern}"]
                escalate: []
                deny: []
        """
    try:
        policy = load(text)
    except PolicyLoadError:
        return  # Rejecting the pattern outright is also safe.
    assert call_tool(policy, tool).action == Action.DENY


@pytest.mark.parametrize(
    "tools",
    [
        '{allow: ["*search*"], escalate: [], deny: ["*search*"]}',
        "{allow: [web_search], escalate: [web_search], deny: []}",
        '{allow: [], escalate: ["*crawl*"], deny: ["*crawl*"]}',
    ],
)
def test_the_same_tool_pattern_in_two_lists_is_rejected(tools: str) -> None:
    with pytest.raises(PolicyLoadError):
        load(
            f"""
            mcp:
              servers:
                search:
                  url: https://mcp.example.org/mcp
                  revision: "2025-11-25"
                  auth: {{mode: none}}
                  tools: {tools}
            """
        )


@pytest.mark.parametrize(
    ("method", "tool"), [("initialize", None), ("tools/list", None), ("tools/call", "web_search")]
)
def test_worker_calls_are_denied_by_default(method: str, tool: str | None) -> None:
    policy = load(
        """
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*"], escalate: [], deny: []}
        """
    )
    assert decide_mcp(policy, method, tool, origin="worker").action == Action.DENY
    assert decide_mcp(policy, method, tool, origin="orchestrator").action == Action.ALLOW


def test_worker_calls_are_denied_when_worker_access_is_false() -> None:
    policy = load(
        """
        mcp:
          proxy: {worker_access: false}
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*"], escalate: [], deny: []}
        """
    )
    assert call_tool(policy, "web_search", origin="worker").action == Action.DENY
    assert decide_mcp(policy, "tools/list", origin="worker").action == Action.DENY


def test_worker_access_inherits_the_server_tool_rules() -> None:
    policy = load(
        """
        mcp:
          proxy: {worker_access: true, worker_tools: inherit}
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*search*"], escalate: ["*crawl*"], deny: ["*reset*"]}
        """
    )
    assert call_tool(policy, "web_search", origin="worker").action == Action.ALLOW
    assert call_tool(policy, "crawl_site", origin="worker").action == Action.ESCALATE
    assert call_tool(policy, "index_reset", origin="worker").action == Action.DENY
    assert decide_mcp(policy, "tools/list", origin="worker").action == Action.ALLOW


def test_worker_tools_list_narrows_worker_tool_calls() -> None:
    policy = load(
        """
        mcp:
          proxy: {worker_access: true, worker_tools: ["*search*", fetch]}
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*"], escalate: [], deny: []}
        """
    )
    assert call_tool(policy, "web_search", origin="worker").action == Action.ALLOW
    assert call_tool(policy, "fetch_page", origin="worker").action == Action.DENY
    assert call_tool(policy, "read_page", origin="worker").action == Action.DENY
    assert call_tool(policy, "read_page", origin="orchestrator").action == Action.ALLOW
    assert decide_mcp(policy, "tools/list", origin="worker").action == Action.ALLOW


def test_worker_tools_never_widen_the_server_tool_rules() -> None:
    policy = load(
        """
        defaults: {action: escalate}
        mcp:
          proxy: {worker_access: true, worker_tools: ["*"]}
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*search*"], escalate: ["*crawl*"], deny: ["*reset*"]}
        """
    )
    assert call_tool(policy, "index_reset", origin="worker").action == Action.DENY
    assert call_tool(policy, "crawl_site", origin="worker").action == Action.ESCALATE
    assert call_tool(policy, "summarize", origin="worker").action == Action.ESCALATE


# --- Packages ------------------------------------------------------------------------------------


def test_conda_packages_from_listed_channels_take_the_conda_action() -> None:
    policy = load(
        """
        defaults: {action: deny}
        packages:
          conda: {channels: [conda-forge], action: allow}
        """
    )
    assert decide_package(policy, "conda", "numpy", "conda-forge").action == Action.ALLOW


def test_conda_packages_from_unlisted_channels_are_not_granted() -> None:
    policy = load(
        """
        defaults: {action: deny}
        packages:
          conda: {channels: [conda-forge], action: allow}
        """
    )
    assert decide_package(policy, "conda", "numpy", "bioconda").action == Action.DENY


@pytest.mark.parametrize("action", [Action.ALLOW, Action.ESCALATE, Action.DENY])
def test_pypi_packages_take_the_pypi_action(action: Action) -> None:
    policy = load(
        f"""
        packages:
          pypi:
            action: {action}
          system_packages: deny
        """
    )
    assert decide_package(policy, "pypi", "requests").action == action


def test_pypi_settings_do_not_apply_to_conda() -> None:
    policy = load(
        """
        defaults: {action: deny}
        packages:
          conda: {channels: [conda-forge], action: deny}
          pypi: {action: allow}
        """
    )
    assert decide_package(policy, "conda", "numpy", "conda-forge").action == Action.DENY
    assert decide_package(policy, "pypi", "numpy").action == Action.ALLOW


MIXED_PACKAGES = """
defaults: {action: escalate}
packages:
  conda: {channels: [conda-forge], action: allow}
  pypi: {action: allow}
  cargo: {action: escalate}
  npm: {action: deny}
"""


@pytest.mark.parametrize(
    ("ecosystem", "name", "channel", "artifact", "index"),
    [
        pytest.param("pypi", "numpy", None, Action.ALLOW, Action.ALLOW, id="pypi-allow"),
        pytest.param("cargo", "serde", None, Action.ESCALATE, Action.ALLOW, id="cargo-escalate"),
        pytest.param("npm", "lodash", None, Action.DENY, Action.DENY, id="npm-deny"),
        # Not configured: defaults.action (escalate) for files. A Go lookup also reads
        # the module's origin, which no rule allows here, so it escalates too.
        pytest.param("go", "golang.org/x/mod", None, Action.ESCALATE, Action.ESCALATE, id="go"),
        pytest.param("conda", "numpy", "conda-forge", Action.ALLOW, Action.ALLOW, id="conda-forge"),
        pytest.param("conda", "numpy", "bioconda", Action.DENY, Action.DENY, id="conda-unlisted"),
    ],
)
def test_package_files_take_the_ecosystem_action_and_lookups_need_only_no_deny(
    ecosystem: Literal["conda", "pypi", "cargo", "go", "npm"],
    name: str,
    channel: str | None,
    artifact: Action,
    index: Action,
) -> None:
    policy = load(MIXED_PACKAGES)
    assert decide_package(policy, ecosystem, name, channel).action == artifact
    assert decide_package(policy, ecosystem, name, channel, kind="index").action == index


def test_registry_wide_index_data_follows_the_ecosystem() -> None:
    policy = load(MIXED_PACKAGES)
    assert decide_package(policy, "conda", None, "conda-forge", kind="index").action == Action.ALLOW
    assert decide_package(policy, "conda", None, "bioconda", kind="index").action == Action.DENY
    assert decide_package(policy, "go", None, kind="index").action == Action.ALLOW
    assert decide_package(policy, "npm", None, kind="index").action == Action.DENY


def test_unconfigured_ecosystems_are_closed_under_a_deny_default() -> None:
    policy = load("defaults: {action: deny}\n")
    for ecosystem in ("pypi", "cargo", "go", "npm"):
        assert decide_package(policy, ecosystem, None, kind="index").action == Action.DENY
    assert decide_package(policy, "cargo", "serde").action == Action.DENY


@pytest.mark.parametrize(
    "url",
    [
        "https://registry.npmjs.org/lodash",
        "https://registry.yarnpkg.com/lodash",
        "https://index.crates.io/se/rd/serde",
        "https://static.crates.io/crates/serde/serde-1.0.0.crate",
        "https://crates.io/api/v1/crates",
        "https://proxy.golang.org/golang.org/x/mod/@v/list",
        "https://sum.golang.org/latest",
        "https://files.pythonhosted.org/packages/x/y.whl",
        "https://opam.ocaml.org/index.tar.gz",
        "https://opam.ocaml.org/cache/sha256/aa/" + "a" * 64,
        "https://hackage.haskell.org/timestamp.json",
        "https://hackage.haskell.org/01-index.tar.gz",
        "https://hackage.haskell.org/package/split-0.2.5.tar.gz",
    ],
)
def test_package_registries_are_reachable_only_through_the_cache(url: str) -> None:
    policy = load(
        """
        network:
          rules:
            - {name: reads, operation: http, match: {hosts: ["*"], methods: [GET]}, action: allow}
        """
    )
    decision = decide_http(policy, "GET", url)
    assert decision.action == Action.DENY
    assert decision.layer == "floor"


GO_READS = """
packages:
  go: {action: allow}
network:
  rules:
    - {name: reads, operation: http, match: {hosts: ["*"], methods: [GET]}, action: allow}
    - {name: no-evil, operation: http, match: {hosts: [evil.example.org]}, action: deny}
    - {name: ask, operation: http, match: {hosts: [ask.example.org]}, action: escalate}
"""


@pytest.mark.parametrize(
    ("module", "kind", "action"),
    [
        pytest.param("golang.org/x/mod", "index", Action.ALLOW, id="public-origin"),
        pytest.param("golang.org/x/mod", "artifact", Action.ALLOW, id="public-origin-zip"),
        pytest.param("evil.example.org/x", "index", Action.DENY, id="denied-origin"),
        pytest.param("ask.example.org/x", "artifact", Action.ESCALATE, id="escalated-origin"),
        pytest.param("metadata.google.internal/x", "index", Action.DENY, id="floor-origin"),
        pytest.param("10.0.0.5/x", "index", Action.DENY, id="private-origin"),
        pytest.param("registry.npmjs.org/x", "index", Action.DENY, id="registry-origin"),
    ],
)
def test_go_modules_are_also_reads_of_their_origin(
    module: str, kind: Literal["index", "artifact"], action: Action
) -> None:
    # The Go module proxy fetches a module it lacks from its origin, so a lookup for
    # a made-up module would otherwise send data to a host the rules forbid.
    policy = load(GO_READS)
    assert decide_package(policy, "go", module, kind=kind).action == action


def test_the_pool_ceiling_applies_to_package_registries() -> None:
    policy = load(MIXED_PACKAGES)
    closed = PoolCeiling.model_validate({"network": {"allow_hosts": []}})
    decision = evaluate(PackageOperation("pypi", "numpy"), policy, ceiling=closed)
    assert (decision.action, decision.layer) == (Action.DENY, "ceiling")
    no_npm = PoolCeiling.model_validate({"network": {"deny_hosts": ["registry.npmjs.org"]}})
    npm_index = evaluate(PackageOperation("npm", None, kind="index"), policy, ceiling=no_npm)
    assert npm_index.layer == "ceiling"
    numpy = evaluate(PackageOperation("pypi", "numpy"), policy, ceiling=no_npm)
    assert numpy.action == Action.ALLOW


def test_unconfigured_conda_reaches_only_conda_forge() -> None:
    policy = load("defaults: {action: allow}\n")
    assert decide_package(policy, "conda", "numpy", "conda-forge").action == Action.ALLOW
    assert decide_package(policy, "conda", "numpy", "bioconda").action == Action.DENY
    assert decide_package(policy, "conda", None, "some-channel", kind="index").action == Action.DENY


@pytest.mark.parametrize(
    "host",
    [
        "registry.npmjs.com",
        "replicate.npmjs.com",
        "unpkg.com",
        "cdn.jsdelivr.net",
        "npm.pkg.github.com",
        "test.pypi.org",
        "test-files.pythonhosted.org",
        "conda-static.anaconda.org",
        "api.anaconda.org",
        "anaconda.org",
        "fast.prefix.dev",
        "prefix.dev",
        "repo.continuum.io",
        "anything.crates.io",
        "goproxy.io",
        "sum.golang.google.cn",
        "opam-4.ocaml.org",
        "opam-5.ocaml.org",
        "staging.opam.ocaml.org",
        "hackage-origin.haskell.org",
        "hackage-content.haskell.org",
        "hackage.fpcomplete.com",
        "hackage-mirror.objects-us-east-1.dream.io",
        "hackage-mirror.objects-website-us-east-1.dream.io",
        "casa.stackage.org",
    ],
)
def test_registry_aliases_and_mirrors_are_on_the_floor(host: str) -> None:
    policy = load(
        """
        network:
          rules:
            - {name: reads, operation: http, match: {hosts: ["*"], methods: [GET]}, action: allow}
        """
    )
    decision = decide_http(policy, "GET", f"https://{host}/x")
    assert (decision.action, decision.layer) == (Action.DENY, "floor")


OPAM_HACKAGE = """
defaults: {action: deny}
packages:
  opam: {action: escalate}
  hackage: {action: allow}
"""
OPAM_ARCHIVE = "md5:ca770e5806032a96131b670f6e07f146"  # ocamlfind 1.9.8


@pytest.mark.parametrize(
    ("ecosystem", "name", "digest", "artifact", "index"),
    [
        pytest.param("opam", None, OPAM_ARCHIVE, Action.ESCALATE, Action.ALLOW, id="opam-archive"),
        pytest.param("hackage", "cryptol", None, Action.ALLOW, Action.ALLOW, id="hackage"),
    ],
)
def test_opam_and_hackage_take_their_actions(
    ecosystem: Ecosystem, name: str | None, digest: str | None, artifact: Action, index: Action
) -> None:
    policy = load(OPAM_HACKAGE)
    file = decide_package(policy, ecosystem, name, digest=digest)
    assert (file.action, file.rule) == (artifact, f"packages.{ecosystem}")
    assert decide_package(policy, ecosystem, None, kind="index").action == index


def test_an_opam_archive_is_decided_by_ecosystem_and_says_so() -> None:
    # opam fetches archives by digest, so the decision names the digest, not a package.
    policy = load(OPAM_HACKAGE)
    decision = decide_package(policy, "opam", None, digest=OPAM_ARCHIVE)
    assert decision.reason == (
        f"opam archive {OPAM_ARCHIVE} (fetched by digest, so it names no package)"
    )
    assert decide_package(policy, "hackage", "cryptol").reason == "Hackage package cryptol"
    assert decide_package(policy, "hackage", None, kind="index").reason == (
        "Hackage index (index lookup)"
    )


@pytest.mark.parametrize("ecosystem", ["opam", "hackage"])
def test_unconfigured_opam_and_hackage_take_the_default(ecosystem: Ecosystem) -> None:
    name, digest = (None, OPAM_ARCHIVE) if ecosystem == "opam" else ("split", None)
    denied = load("defaults: {action: deny}\n")
    assert decide_package(denied, ecosystem, name, digest=digest).action == Action.DENY
    assert decide_package(denied, ecosystem, None, kind="index").action == Action.DENY
    asking = load("defaults: {action: escalate}\n")
    assert decide_package(asking, ecosystem, name, digest=digest).action == Action.ESCALATE
    assert decide_package(asking, ecosystem, None, kind="index").action == Action.ALLOW


def test_the_pool_ceiling_applies_to_opam_and_hackage() -> None:
    policy = load(OPAM_HACKAGE)
    no_opam = PoolCeiling.model_validate({"network": {"deny_hosts": ["opam.ocaml.org"]}})
    archive = evaluate(PackageOperation("opam", None, digest=OPAM_ARCHIVE), policy, ceiling=no_opam)
    assert (archive.action, archive.layer) == (Action.DENY, "ceiling")
    sdist = evaluate(PackageOperation("hackage", "split"), policy, ceiling=no_opam)
    assert sdist.action == Action.ALLOW
    only_hackage = PoolCeiling.model_validate({"network": {"allow_hosts": ["hackage.haskell.org"]}})
    allowed = evaluate(PackageOperation("hackage", "split"), policy, ceiling=only_hackage)
    assert allowed.action == Action.ALLOW
    index = evaluate(PackageOperation("opam", None, kind="index"), policy, ceiling=only_hackage)
    assert index.layer == "ceiling"


READS_EVERYWHERE = """
network:
  rules:
    - {name: reads, operation: http, match: {hosts: ["*"], methods: [GET]}, action: allow}
    - name: fetch
      operation: git
      match: {hosts: ["*"], git_service: upload-pack}
      action: allow
"""


@pytest.mark.parametrize(
    "url",
    [
        "http://objects-us-east-1.dream.io/hackage-mirror/timestamp.json",
        "http://objects-us-east-1.dream.io/hackage-mirror/package/split-0.2.5.tar.gz",
        "https://objects-us-east-1.dream.io/hackage-mirror",
        "https://objects-us-east-1.dream.io/hackage-mirror/",
        "http://OBJECTS-US-EAST-1.dream.io/hackage-mirror/root.json",
        "http://objects-us-east-1.dream.io:80/hackage-mirror/root.json",
    ],
)
def test_a_mirror_on_a_shared_host_is_on_the_floor(url: str) -> None:
    # Hackage's signed mirror list names a bucket on a shared object store, which
    # cabal falls back to when the cache fails.
    decision = decide_http(load(READS_EVERYWHERE), "GET", url)
    assert (decision.action, decision.layer) == (Action.DENY, "floor")


@pytest.mark.parametrize(
    "url",
    [
        "http://objects-us-east-1.dream.io/other-bucket/data.csv",
        "http://objects-us-east-1.dream.io/hackage-mirrors/x",
        "http://objects-us-east-1.dream.io/",
    ],
)
def test_the_rest_of_a_shared_host_stays_with_the_rules(url: str) -> None:
    assert decide_http(load(READS_EVERYWHERE), "GET", url).action == Action.ALLOW


def test_a_mirror_path_is_on_the_floor_for_git_fetches_too() -> None:
    base = "https://objects-us-east-1.dream.io/hackage-mirror/repo"
    for method, url in (
        ("GET", f"{base}/info/refs?service=git-upload-pack"),
        ("POST", f"{base}/git-upload-pack"),
    ):
        decision = decide_http(load(READS_EVERYWHERE), method, url)
        assert (decision.action, decision.layer) == (Action.DENY, "floor")


# --- Escalation timeout --------------------------------------------------------------------------


def test_escalations_use_the_policy_wide_timeout_and_deny_on_timeout() -> None:
    policy = load(
        """
        escalation: {timeout: 10m}
        network:
          rules:
            - name: writes
              operation: http
              match: {hosts: ["*"], methods: [POST]}
              action: escalate
        """
    )
    by_rule = decide_http(policy, "POST", "https://example.org/upload")
    by_default = decide_http(policy, "PUT", "https://example.org/upload")
    assert escalation_terms(by_rule) == (timedelta(minutes=10), Action.DENY)
    assert escalation_terms(by_default) == (timedelta(minutes=10), Action.DENY)


def test_policy_wide_on_timeout_deny_is_accepted() -> None:
    policy = load("escalation: {timeout: 5m, on_timeout: deny}")
    decision = decide_http(policy, "POST", "https://example.org/upload")
    assert escalation_terms(decision) == (timedelta(minutes=5), Action.DENY)


def test_policy_wide_on_timeout_allow_is_rejected() -> None:
    with pytest.raises(PolicyLoadError):
        load("escalation: {timeout: 30m, on_timeout: allow}")


def test_a_rule_can_override_the_timeout() -> None:
    policy = load(
        """
        escalation: {timeout: 10m}
        network:
          rules:
            - name: slow
              operation: http
              match: {hosts: [example.org]}
              action: escalate
              escalation: {timeout: 2h}
            - {name: other, operation: http, match: {hosts: [example.com]}, action: escalate}
        """
    )
    slow = decide_http(policy, "POST", "https://example.org/")
    other = decide_http(policy, "POST", "https://example.com/")
    assert escalation_terms(slow) == (timedelta(hours=2), Action.DENY)
    assert escalation_terms(other) == (timedelta(minutes=10), Action.DENY)


@pytest.mark.parametrize(
    ("methods", "method"),
    [("[GET, HEAD]", "GET"), ("[GET, HEAD]", "HEAD"), ("[GET]", "GET"), ("[HEAD]", "HEAD")],
)
def test_read_only_http_rules_may_proceed_as_alert_on_timeout(methods: str, method: str) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: reads
              operation: http
              match:
                hosts: ["*"]
                methods: {methods}
              action: escalate
              escalation:
                on_timeout: allow
        """
    )
    decision = decide_http(policy, method, "https://example.org/")
    assert escalation_terms(decision) == (timedelta(minutes=30), Action.ALERT)


def test_git_fetch_rules_may_proceed_as_alert_on_timeout() -> None:
    policy = load(
        """
        network:
          rules:
            - name: fetch
              operation: git
              match: {hosts: ["*"], git_service: upload-pack}
              action: escalate
              escalation: {timeout: 5m, on_timeout: allow}
        """
    )
    assert escalation_terms(decide_http(policy, "POST", FETCH)) == (
        timedelta(minutes=5),
        Action.ALERT,
    )


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        pytest.param("http", '{hosts: ["*"]}', id="http-any-method"),
        pytest.param("http", '{hosts: ["*"], methods: [GET, POST]}', id="http-get-and-post"),
        pytest.param("http", '{hosts: ["*"], methods: [POST]}', id="http-post"),
        pytest.param("http", '{hosts: ["*"], methods: [GET, OPTIONS]}', id="http-options"),
        pytest.param("git", '{hosts: ["*"], git_service: receive-pack}', id="git-push"),
        pytest.param("git", '{hosts: ["*"]}', id="git-any-service"),
    ],
)
def test_on_timeout_allow_is_rejected_for_rules_that_can_write(operation: str, match: str) -> None:
    with pytest.raises(PolicyLoadError):
        load(
            f"""
            network:
              rules:
                - name: writes
                  operation: {operation}
                  match: {match}
                  action: escalate
                  escalation:
                    on_timeout: allow
            """
        )


@pytest.mark.parametrize(("method", "url"), [("GET", "https://example.org/"), ("POST", FETCH)])
def test_credentialed_requests_are_denied_on_timeout_even_under_read_only_rules(
    method: str, url: str
) -> None:
    policy = load(
        """
        network:
          rules:
            - name: reads
              operation: http
              match: {hosts: ["*"], methods: [GET, HEAD]}
              action: escalate
              escalation: {on_timeout: allow}
            - name: fetch
              operation: git
              match: {hosts: ["*"], git_service: upload-pack}
              action: escalate
              escalation: {on_timeout: allow}
        """
    )
    anonymous = decide_http(policy, method, url)
    credentialed = decide_http(policy, method, url, credentialed=True)
    assert escalation_terms(anonymous) == (timedelta(minutes=30), Action.ALERT)
    assert escalation_terms(credentialed) == (timedelta(minutes=30), Action.DENY)


@pytest.mark.parametrize(("method", "url"), [("GET", "https://example.org/"), ("POST", FETCH)])
def test_a_rule_can_let_credentialed_requests_proceed_on_timeout(method: str, url: str) -> None:
    policy = load(
        """
        network:
          rules:
            - name: reads
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              action: escalate
              escalation: {on_timeout: allow, allow_credentialed: true}
            - name: fetch
              operation: git
              match: {hosts: ["*"], git_service: upload-pack}
              action: escalate
              escalation: {on_timeout: allow, allow_credentialed: true}
        """
    )
    credentialed = decide_http(policy, method, url, credentialed=True)
    assert escalation_terms(credentialed) == (timedelta(minutes=30), Action.ALERT)


def test_allow_credentialed_does_not_lift_url_limits() -> None:
    policy = load(
        """
        network:
          rules:
            - name: reads
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              limits: {max_url_bytes: 64}
              action: escalate
              escalation: {on_timeout: allow, allow_credentialed: true}
        """
    )
    long_url = "https://example.org/?" + "q" * 100
    # An escalate rule whose limit is exceeded still asks, but never proceeds unanswered.
    decision = decide_http(policy, "GET", long_url, credentialed=True)
    assert escalation_terms(decision) == (timedelta(minutes=30), Action.DENY)


def test_credentialed_requests_proceed_only_if_every_tied_rule_allows_it() -> None:
    policy = load(
        """
        network:
          rules:
            - name: with
              operation: http
              match: {hosts: [example.org], methods: [GET]}
              action: escalate
              escalation: {on_timeout: allow, allow_credentialed: true}
            - name: without
              operation: http
              match: {hosts: [example.org], methods: [GET, HEAD]}
              action: escalate
              escalation: {on_timeout: allow}
        """
    )
    anonymous = decide_http(policy, "GET", "https://example.org/")
    credentialed = decide_http(policy, "GET", "https://example.org/", credentialed=True)
    assert escalation_terms(anonymous)[1] is Action.ALERT
    assert escalation_terms(credentialed)[1] is Action.DENY


@pytest.mark.parametrize(
    "escalation",
    ["{allow_credentialed: true}", "{on_timeout: deny, allow_credentialed: true}"],
)
def test_allow_credentialed_needs_on_timeout_allow(escalation: str) -> None:
    with pytest.raises(PolicyLoadError, match="allow_credentialed"):
        load(
            f"""
            network:
              rules:
                - name: reads
                  operation: http
                  match: {{hosts: ["*"], methods: [GET]}}
                  action: escalate
                  escalation: {escalation}
            """
        )


def test_allow_credentialed_cannot_widen_a_rule_that_writes() -> None:
    with pytest.raises(PolicyLoadError, match="read-only"):
        load(
            """
            network:
              rules:
                - name: writes
                  operation: http
                  match: {hosts: ["*"], methods: [GET, POST]}
                  action: escalate
                  escalation: {on_timeout: allow, allow_credentialed: true}
            """
        )


def test_write_rules_may_override_the_timeout_with_on_timeout_deny() -> None:
    policy = load(
        """
        network:
          rules:
            - name: writes
              operation: http
              match: {hosts: ["*"], methods: [POST]}
              action: escalate
              escalation: {timeout: 5m, on_timeout: deny}
        """
    )
    decision = decide_http(policy, "POST", "https://example.org/upload")
    assert escalation_terms(decision) == (timedelta(minutes=5), Action.DENY)


@pytest.mark.parametrize("action", [Action.ALLOW, Action.ALERT, Action.DENY])
def test_escalation_overrides_are_rejected_on_non_escalate_rules(action: Action) -> None:
    with pytest.raises(PolicyLoadError):
        load(
            f"""
            network:
              rules:
                - name: reads
                  operation: http
                  match:
                    hosts: ["*"]
                    methods: [GET]
                  action: {action}
                  escalation:
                    timeout: 5m
            """
        )


@pytest.mark.parametrize("action", [Action.ALLOW, Action.ALERT, Action.DENY])
def test_escalation_terms_are_present_only_for_escalate(action: Action) -> None:
    policy = load(
        f"""
        network:
          rules:
            - name: rule
              operation: http
              match:
                hosts: [example.org]
              action: {action}
        """
    )
    assert decide_http(policy, "GET", "https://example.org/").escalation is None


def test_mcp_tool_escalations_use_the_policy_wide_setting() -> None:
    policy = load(
        """
        escalation: {timeout: 15m}
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*search*"], escalate: ["*crawl*"], deny: []}
        """
    )
    decision = call_tool(policy, "crawl_site")
    assert escalation_terms(decision) == (timedelta(minutes=15), Action.DENY)


def test_package_escalations_use_the_policy_wide_setting() -> None:
    policy = load(
        """
        escalation: {timeout: 20m}
        packages:
          pypi: {action: escalate}
        """
    )
    decision = decide_package(policy, "pypi", "requests")
    assert escalation_terms(decision) == (timedelta(minutes=20), Action.DENY)


# --- Monitors ------------------------------------------------------------------------------------


def test_monitor_alerts_only_above_its_threshold() -> None:
    policy = load(
        """
        monitors:
          - name: large-egress
            when: {egress_bytes_per_hour_over: 500Mi}
            action: alert
        """
    )
    threshold = 500 * 1024 * 1024
    assert evaluate_monitors(policy, EgressUsage(egress_bytes_last_hour=0)) == ()
    assert evaluate_monitors(policy, EgressUsage(egress_bytes_last_hour=threshold)) == ()
    alerts = evaluate_monitors(policy, EgressUsage(egress_bytes_last_hour=threshold + 1))
    assert [alert.rule for alert in alerts] == ["large-egress"]
    assert alerts[0].reason


def test_monitors_are_additive() -> None:
    policy = load(
        """
        monitors:
          - {name: some-egress, when: {egress_bytes_per_hour_over: 1Ki}, action: alert}
          - {name: lots-of-egress, when: {egress_bytes_per_hour_over: 1Mi}, action: alert}
        """
    )
    some = evaluate_monitors(policy, EgressUsage(egress_bytes_last_hour=2 * 1024))
    lots = evaluate_monitors(policy, EgressUsage(egress_bytes_last_hour=2 * 1024 * 1024))
    assert [alert.rule for alert in some] == ["some-egress"]
    assert sorted(alert.rule for alert in lots) == ["lots-of-egress", "some-egress"]


def test_policy_without_monitors_raises_no_alerts() -> None:
    assert evaluate_monitors(load(), EgressUsage(egress_bytes_last_hour=1 << 40)) == ()


@pytest.mark.parametrize(("method", "expected"), [("GET", Action.ALLOW), ("POST", Action.ESCALATE)])
def test_monitors_never_change_access_decisions(method: str, expected: Action) -> None:
    policy = load(
        """
        network:
          rules:
            - {name: reads, operation: http, match: {hosts: ["*"], methods: [GET]}, action: allow}
        monitors:
          - name: any-egress
            when: {egress_bytes_per_hour_over: 1Ki}
            action: alert
        """
    )
    decision = decide_http(policy, method, "https://example.org/")
    assert decision.action == expected
    assert decision.rule != "any-egress"
    assert evaluate_monitors(policy, EgressUsage(egress_bytes_last_hour=1 << 30))


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        pytest.param("any", "{egress_bytes_per_hour_over: 500Mi}", id="operation-any"),
        pytest.param(
            "http", '{hosts: ["*"], egress_bytes_per_hour_over: 500Mi}', id="threshold-condition"
        ),
    ],
)
def test_thresholds_are_rejected_in_network_rules(operation: str, match: str) -> None:
    with pytest.raises(PolicyLoadError):
        load(
            f"""
            network:
              rules:
                - name: large-egress
                  operation: {operation}
                  match: {match}
                  action: alert
            """
        )


@pytest.mark.parametrize("action", [Action.ALLOW, Action.ESCALATE, Action.DENY])
def test_monitors_can_only_alert(action: Action) -> None:
    with pytest.raises(PolicyLoadError):
        load(
            f"""
            monitors:
              - name: large-egress
                when:
                  egress_bytes_per_hour_over: 500Mi
                action: {action}
            """
        )


# --- Policy digest -------------------------------------------------------------------------------


def test_digest_is_the_sha256_of_the_exact_input_bytes() -> None:
    text = HEADER + "defaults: {action: deny}\n"
    digest = parse_policy(text).digest
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    assert digest == sha256_digest(text.encode("utf-8"))


def test_bytes_input_is_digested_without_normalization() -> None:
    data = (HEADER + "defaults: {action: deny}\n").replace("\n", "\r\n").encode()
    assert parse_policy(data).digest == sha256_digest(data)


def test_str_input_is_digested_as_utf8() -> None:
    text = HEADER + "# R\u00e9sum\u00e9 \u2013 caf\u00e9\n"
    assert parse_policy(text).digest == sha256_digest(text.encode("utf-8"))


def test_digest_is_stable() -> None:
    text = HEADER + "defaults: {action: deny}\n"
    assert parse_policy(text).digest == parse_policy(text).digest
    assert parse_policy(text).digest == parse_policy(text.encode("utf-8")).digest


def test_digest_differs_for_different_content() -> None:
    text = HEADER + "defaults: {action: deny}\n"
    assert parse_policy(text).digest != parse_policy(text + "# same rules, new comment\n").digest
    assert parse_policy(text).digest != parse_policy(HEADER + "defaults: {action: allow}\n").digest


def test_load_policy_digests_the_file_bytes(tmp_path: Path) -> None:
    data = (HEADER + "defaults: {action: deny}\n").encode()
    path = tmp_path / "policy.yaml"
    path.write_bytes(data)
    loaded = load_policy(path)
    assert loaded.digest == sha256_digest(data)
    assert "policy.yaml" in str(loaded.source)


def test_parse_policy_records_the_given_source() -> None:
    assert parse_policy(HEADER, source="inline-test").source == "inline-test"


def test_decisions_record_the_policy_digest() -> None:
    policy = load(
        """
        defaults: {action: deny}
        packages:
          pypi: {action: allow}
        network:
          rules:
            - {name: reads, operation: http, match: {hosts: [example.org]}, action: allow}
        mcp:
          servers:
            search:
              url: https://mcp.example.org/mcp
              revision: "2025-11-25"
              auth: {mode: none}
              tools: {allow: ["*search*"], escalate: [], deny: []}
        """
    )
    decisions = {
        "rule": decide_http(policy, "GET", "https://example.org/"),
        "default": decide_http(policy, "GET", "https://example.com/"),
        "mcp": call_tool(policy, "web_search"),
        "package": decide_package(policy, "pypi", "requests"),
    }
    assert {name: d.policy_digest for name, d in decisions.items()} == dict.fromkeys(
        decisions, policy.digest
    )
