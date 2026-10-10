# SPDX-License-Identifier: MIT
"""Regression tests for the 2026-10-10 adversarial review of the policy library.

Each test names the finding it pins. The review ran against commits 0977b35 and
1222d7c; every case below was more permissive than docs/POLICY.md intends, or
raised an exception other than the library's own.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from textwrap import dedent

import pytest

from sandbox_science.policy import (
    Action,
    HardFloor,
    HttpRequest,
    LoadedPolicy,
    McpOperation,
    PolicyLoadError,
    PoolCeiling,
    evaluate,
    evaluate_request,
    load_policy,
    parse_ceiling,
    parse_policy,
)
from sandbox_science.policy.hosts import floor_reason, is_public

EXAMPLE = load_policy(Path(__file__).resolve().parents[3] / "examples" / "policy.yaml")
OPEN = PoolCeiling.unrestricted()
PREFIX = "crypto_research_mcp-"
UPLOAD_PACK = "application/x-git-upload-pack-request"

HEADER = dedent(
    """\
    apiVersion: sandbox-science/v0alpha1
    kind: ProjectPolicy
    metadata: {name: test}
    """
)


def policy(body: str) -> LoadedPolicy:
    return parse_policy(HEADER + dedent(body))


def decide(
    method: str,
    url: str,
    loaded: LoadedPolicy = EXAMPLE,
    *,
    ceiling: PoolCeiling = OPEN,
    floor: HardFloor | None = None,
    **fields: object,
) -> Action:
    request = HttpRequest(method, url, **fields)  # type: ignore[arg-type]
    decision = evaluate_request(request, loaded, ceiling=ceiling, floor=floor or HardFloor())
    return decision.action


def tool(name: str, loaded: LoadedPolicy = EXAMPLE, **fields: object) -> Action:
    operation = McpOperation("crypto-research-mcp", "tools/call", name, **fields)  # type: ignore[arg-type]
    return evaluate(operation, loaded, ceiling=OPEN).action


# Finding 1: other spellings of an IP address.


@pytest.mark.parametrize(
    "url",
    [
        "http://[::ffff:10.0.0.5]/v1/policy",
        "http://[::ffff:a00:5]/v1/policy",
        "http://[64:ff9b::a00:5]/v1/policy",
        "http://[::a00:5]/v1/policy",
    ],
)
def test_admin_hosts_cannot_be_reached_through_ipv6_spellings(url: str) -> None:
    floor = HardFloor(admin_hosts=frozenset({"10.0.0.5"}))
    assert decide("POST", url, floor=floor) is Action.DENY


@pytest.mark.parametrize(
    "url", ["http://[::ffff:93.184.215.14]/", "http://[2606:4700:4700::1111%251]/"]
)
def test_ceiling_deny_hosts_cannot_be_reached_through_other_spellings(url: str) -> None:
    ceiling = parse_ceiling(
        'kind: PoolCeiling\nnetwork: {deny_hosts: ["93.184.215.14", "2606:4700:4700::1111"]}\n'
    )
    assert decide("GET", url, ceiling=ceiling) is Action.DENY


def test_exact_deny_rules_cannot_be_bypassed_through_ipv4_mapped_addresses() -> None:
    loaded = policy(
        """
        network:
          rules:
            - {name: web, operation: http, match: {hosts: ["*"], methods: [GET]}, action: allow}
            - {name: block, operation: http, match: {hosts: ["8.8.8.8"]}, action: deny}
        """
    )
    assert decide("GET", "http://8.8.8.8/", loaded) is Action.DENY
    assert decide("GET", "http://[::ffff:8.8.8.8]/", loaded) is Action.DENY


# Finding 2: POSTs whose path merely ends in /git-upload-pack.


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/api/v4/projects/123/repository/files/git-upload-pack",
        "https://codeberg.org/api/v1/repos/a/b/contents/git-upload-pack",
    ],
)
@pytest.mark.parametrize("content_type", [None, "application/json"])
def test_rest_writes_ending_in_git_upload_pack_are_not_git_fetches(
    url: str, content_type: str | None
) -> None:
    action = decide("POST", url, content_type=content_type, credentialed=True)
    assert action is Action.ESCALATE


def test_git_fetch_with_git_content_type_is_still_allowed() -> None:
    url = "https://github.com/regenscheid/sandbox-crypto.git/git-upload-pack"
    assert decide("POST", url, content_type=UPLOAD_PACK) is Action.ALLOW
    assert decide("POST", url, content_type=UPLOAD_PACK + "; charset=x") is Action.ALLOW


def test_git_rules_have_url_limits() -> None:
    url = "https://github.com/" + "a" * 20_000 + "/info/refs?service=git-upload-pack"
    assert decide("GET", url) is Action.DENY


# Finding 3: "*search*" matched the portal prefix of every tool.


@pytest.mark.parametrize(
    "name",
    [
        PREFIX + "github-mcp_delete_repository",
        PREFIX + "admin_reset_all",
        PREFIX + "paper-search-mcp_delete_everything",
    ],
)
def test_unknown_tools_ask_first(name: str) -> None:
    assert tool(name) is Action.ESCALATE


@pytest.mark.parametrize(
    "name",
    [
        PREFIX + "portal_enable_server",
        PREFIX + "portal_TOGGLE_servers",
        PREFIX + "portal_toggle_servers",
    ],
)
def test_portal_tools_other_than_listing_are_denied(name: str) -> None:
    assert tool(name) is Action.DENY
    assert tool(PREFIX + "portal_list_servers") is Action.ALLOW


@pytest.mark.parametrize(
    "name", ["firecrawl-mcp_firecrawl_search", "other_prefix-portal_list_servers"]
)
def test_tools_outside_the_declared_prefix_are_denied(name: str) -> None:
    assert tool(name) is Action.DENY


def test_worker_tools_narrow_after_the_prefix_is_removed() -> None:
    text = (Path(__file__).resolve().parents[3] / "examples" / "policy.yaml").read_text()
    narrowed = parse_policy(text.replace("worker_tools: inherit", 'worker_tools: ["*_search"]'))
    assert tool(PREFIX + "paper-search-mcp_arxiv_search", narrowed, origin="worker") is Action.ALLOW
    assert tool(PREFIX + "firecrawl-mcp_firecrawl_map", narrowed, origin="worker") is Action.DENY


# Finding 4: what a reviewer reads must be what is enforced.


@pytest.mark.parametrize(
    "hidden", ["\u2028", "\u2029", "\x85", "\r", "\u200b", "\u202e", "\u2066", "\ufeff", "\x0c"]
)
def test_hidden_line_breaks_and_invisible_characters_are_rejected(hidden: str) -> None:
    text = HEADER + f'defaults: {{action: escalate}}  # note{hidden}  x: "y"\n'
    with pytest.raises(PolicyLoadError, match="invisible or line-breaking"):
        parse_policy(text)


def test_a_leading_byte_order_mark_and_crlf_line_ends_are_accepted() -> None:
    parse_policy("\ufeff" + HEADER.replace("\n", "\r\n"))


@pytest.mark.parametrize(
    "fragment",
    [
        "mcp:\n  servers:\n    !!binary Y3J5cHRvLXJlc2VhcmNoLW1jcA==: {}\n",
        "defaults: {action: !!str allow}\n",
        "defaults: !mytag {action: allow}\n",
        "network: !!map [a]\n",
        "x: !!bool maybe\n",
        "x: !!timestamp foo\n",
        "x: !!int foo\n",
    ],
)
def test_explicit_yaml_tags_are_rejected(fragment: str) -> None:
    with pytest.raises(PolicyLoadError, match="tags are not allowed"):
        parse_policy(HEADER + fragment)


@pytest.mark.parametrize(
    "path", ["/v1/admin\u200b", "/v1/\u0430dmin", "/v1/admin\u00a0", "/v1/a b", "/v1/admin."]
)
def test_path_prefixes_are_printable_ascii(path: str) -> None:
    text = HEADER + (
        "network:\n  rules:\n"
        f'    - {{name: r, operation: http, match: {{hosts: [api.example.org], paths: ["{path}"]}},'
        " action: deny}\n"
    )
    with pytest.raises(PolicyLoadError):
        parse_policy(text)


# Finding 5: keys present without a value in a pool ceiling.


@pytest.mark.parametrize(
    "body",
    [
        "network:\n  allow_hosts:\n  # - pypi.org\n",
        "mcp_servers:\n# - crypto-research-mcp\n",
        "resources: {memory: }\n",
        "network:\n",
    ],
)
def test_ceiling_keys_without_values_are_rejected(body: str) -> None:
    with pytest.raises(PolicyLoadError, match="without a value"):
        parse_ceiling("kind: PoolCeiling\n" + body)


# Finding 6: paths that some servers decode or trim differently.


@pytest.mark.parametrize(
    "path",
    [
        "/v1/search/%252e%252e/admin",
        "/v1/%2561dmin",
        "/v1/admin.",
        "/v1/admin%20",
        "/v1/%20admin",
        "/v1/admin%3F",
        "/v1/admin%23x",
    ],
)
def test_ambiguous_path_encodings_are_denied(path: str) -> None:
    assert decide("GET", "https://api.example.org" + path) is Action.DENY


# Finding 7: ties must not drop limits or escalation safeguards.


def test_an_equally_specific_alert_rule_keeps_the_url_limit() -> None:
    loaded = policy(
        """
        network:
          rules:
            - name: web
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              limits: {max_url_bytes: 4096}
              action: allow
            - {name: watch, operation: http, match: {hosts: ["*"], methods: [GET]}, action: alert}
        """
    )
    assert decide("GET", "https://example.org/" + "a" * 50_000, loaded) is Action.DENY
    assert decide("GET", "https://example.org/short", loaded) is Action.ALERT


def test_tied_rules_with_the_same_action_apply_every_limit() -> None:
    loaded = policy(
        """
        network:
          rules:
            - name: unlimited
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              action: allow
            - name: limited
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              limits: {max_url_bytes: 100}
              action: allow
        """
    )
    assert decide("GET", "https://example.org/" + "a" * 200, loaded) is Action.DENY


def test_tied_escalations_take_the_strictest_terms() -> None:
    loaded = policy(
        """
        network:
          rules:
            - name: lenient
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              action: escalate
              escalation: {timeout: 1h, on_timeout: allow}
            - name: strict
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              action: escalate
              escalation: {timeout: 5m}
        """
    )
    request = HttpRequest("GET", "https://example.org/")
    decision = evaluate_request(request, loaded, ceiling=OPEN)
    assert decision.escalation is not None
    assert decision.escalation.timeout == timedelta(minutes=5)
    assert decision.escalation.on_timeout is Action.DENY


def test_an_exceeded_limit_never_times_out_to_an_alert() -> None:
    loaded = policy(
        """
        network:
          rules:
            - name: slow-read
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              limits: {max_url_bytes: 256}
              action: escalate
              escalation: {on_timeout: allow}
        """
    )
    short = evaluate_request(HttpRequest("GET", "https://example.org/"), loaded, ceiling=OPEN)
    long = evaluate_request(
        HttpRequest("GET", "https://example.org/" + "a" * 50_000), loaded, ceiling=OPEN
    )
    assert short.escalation is not None
    assert short.escalation.on_timeout is Action.ALERT
    assert long.escalation is not None
    assert long.escalation.on_timeout is Action.DENY


# Findings 8 and 9: address classes and the hard floor.


def test_deprecated_site_local_ipv6_is_not_public() -> None:
    assert not is_public("fec0::1")
    assert decide("GET", "http://[fec0::1]/") is Action.DENY


@pytest.mark.parametrize(
    "host",
    [
        "fd20:ce::254",
        "fd00:ec2::23",
        "192.0.0.192",
        "64:ff9b:1::a9fe:a9fe",
        "::ffff:0:a9fe:a9fe",
        "0.0.0.1",
        "localhost.localdomain",
        "ip6-localhost",
        "metadata.tencentyun.com",
        "host.docker.internal",
        "host.containers.internal",
    ],
)
def test_floor_covers_more_metadata_and_host_addresses(host: str) -> None:
    assert floor_reason(host) is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://[fd20:ce::254]/computeMetadata/v1/",
        "http://192.0.0.192/opc/v1/instance/",
        "http://localhost.localdomain/",
        "http://metadata.tencentyun.com/latest/meta-data/",
        "http://host.docker.internal:2375/containers/json",
    ],
)
def test_floor_denies_these_destinations_even_under_an_exact_allow(url: str) -> None:
    host = url.split("/")[2].split(":")[0].strip("[]") if "[" not in url else "fd20:ce::254"
    loaded = policy(
        f"""
        network:
          rules:
            - {{name: exact, operation: http, match: {{hosts: ["{host}"], ports: [80, 443, 2375]}},
               action: allow}}
        """
    )
    decision = evaluate_request(HttpRequest("GET", url), loaded, ceiling=OPEN)
    assert decision.action is Action.DENY
    assert decision.layer == "floor"


# Finding 10: hostile input must raise only the library's own errors.


@pytest.mark.parametrize(
    "fragment",
    [
        "x: 2025-13-45\n",  # an invalid date
        "x: " + "9" * 5000 + "\n",
        "ports: [0443]\n",  # YAML 1.1 octal
        "ports: [1:20]\n",  # YAML 1.1 base 60
        "count: 1_000\n",
        "worker_access: yes\n",
        "flag: on\n",
    ],
)
def test_ambiguous_scalars_are_rejected(fragment: str) -> None:
    with pytest.raises(PolicyLoadError):
        parse_policy(HEADER + fragment)


@pytest.mark.parametrize(
    "url", ["http://[::1", "http://[1.2.3.4]/", "http://evil[::1]/", "http://[]/", "http://[v1.x]/"]
)
def test_malformed_urls_are_denied_without_raising(url: str) -> None:
    decision = evaluate_request(HttpRequest("GET", url), EXAMPLE, ceiling=OPEN)
    assert decision.action is Action.DENY
    assert decision.layer == "classification"


# Finding 11: lowercase methods.


@pytest.mark.parametrize("method", ["get", "hEaD", "Post"])
def test_lowercase_methods_are_denied(method: str) -> None:
    assert decide(method, "https://example.org/") is Action.DENY


# Finding 12: MCP servers are reachable only through the MCP proxy.


def test_declared_mcp_endpoints_cannot_be_reached_as_plain_http() -> None:
    loaded = policy(
        """
        network:
          rules:
            - name: web
              operation: http
              match: {hosts: ["*"], methods: [GET, POST]}
              action: allow
        mcp:
          servers:
            tools:
              url: https://mcp.example.org/mcp
              revision: "1"
              auth: {mode: none}
        """
    )
    for method in ("GET", "POST"):
        decision = evaluate_request(
            HttpRequest(method, "https://mcp.example.org/mcp/x"), loaded, ceiling=OPEN
        )
        assert decision.action is Action.DENY
        assert decision.layer == "floor"
    assert decide("GET", "https://mcp.example.org/docs", loaded) is Action.ALLOW


# Suspected issues the review raised, closed in the same change.


def test_bodies_on_get_head_and_trace_are_denied() -> None:
    for method in ("GET", "HEAD", "TRACE"):
        assert decide(method, "https://example.org/", has_body=True) is Action.DENY


def test_protocol_upgrades_are_denied() -> None:
    assert decide("GET", "https://example.org/socket", upgrade="websocket") is Action.DENY


@pytest.mark.parametrize("field", ["memory", "workspace_disk"])
def test_zero_memory_or_disk_is_rejected(field: str) -> None:
    with pytest.raises(PolicyLoadError):
        parse_policy(HEADER + f"resources: {{{field}: 0}}\n")


def test_many_wildcards_match_in_bounded_time() -> None:
    loaded = policy(
        """
        mcp:
          servers:
            srv:
              url: https://mcp.example.org/x
              revision: "1"
              auth: {mode: none}
              tools: {allow: ["*a*a*a*a*a*a*a*a*b"]}
        """
    )
    operation = McpOperation("srv", "tools/call", "a" * 256)
    started = time.perf_counter()
    decision = evaluate(operation, loaded, ceiling=OPEN)
    assert time.perf_counter() - started < 0.5
    assert decision.layer == "default"
