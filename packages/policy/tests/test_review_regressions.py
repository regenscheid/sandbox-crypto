# SPDX-License-Identifier: MIT
"""Regression tests for the 2026-10-10 adversarial reviews of the policy library.

Each test names the finding it pins. The first review ran against commits
0977b35 and 1222d7c, the second against 515b5ea; every case below was more
permissive than docs/POLICY.md intends, or raised an exception other than the
library's own.
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

EXAMPLE_TEXT = (Path(__file__).resolve().parents[3] / "examples" / "policy.yaml").read_text()
EXAMPLE = load_policy(Path(__file__).resolve().parents[3] / "examples" / "policy.yaml")
OPEN = PoolCeiling.unrestricted()
# crypto-research-mcp lists bare tool names; a portal with a prefix is tested on a copy.
PREFIX = "crypto_research_mcp-"
PREFIXED_TEXT = EXAMPLE_TEXT.replace(
    "      tools:\n", f"      tool_prefix: {PREFIX}\n      tools:\n", 1
)
PREFIXED = parse_policy(PREFIXED_TEXT)
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
    request = HttpRequest(method, url, **fields)  # pyright: ignore
    decision = evaluate_request(request, loaded, ceiling=ceiling, floor=floor or HardFloor())
    return decision.action


def tool(name: str, loaded: LoadedPolicy = EXAMPLE, **fields: object) -> Action:
    operation = McpOperation("crypto-research-mcp", "tools/call", name, **fields)  # pyright: ignore
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
    assert decide("POST", url, content_type=UPLOAD_PACK, has_body=True) is Action.ALLOW


def test_git_rules_can_have_url_limits() -> None:
    # The example no longer sets them (D95), but a policy still can.
    limited = policy(
        """
        network:
          rules:
            - name: fetch
              operation: git
              match: {hosts: ["*"], git_service: upload-pack}
              limits: {max_url_bytes: 4096}
              action: allow
        """
    )
    url = "https://github.com/" + "a" * 20_000 + "/info/refs?service=git-upload-pack"
    assert decide("GET", url, limited) is Action.DENY
    assert decide("GET", url) is Action.ALLOW


# Finding 3: "*search*" matched the portal prefix of every tool.


@pytest.mark.parametrize(
    "name",
    [
        "github-mcp_delete_repository",
        "admin_reset_all",
        "paper-search-mcp_delete_everything",
    ],
)
def test_unknown_tools_ask_first(name: str) -> None:
    assert tool(name) is Action.ESCALATE


@pytest.mark.parametrize(
    "name",
    [
        "portal_enable_server",
        "portal_TOGGLE_servers",
        "portal_toggle_servers",
    ],
)
def test_portal_tools_other_than_listing_are_denied(name: str) -> None:
    assert tool(name) is Action.DENY
    assert tool("portal_list_servers") is Action.ALLOW


@pytest.mark.parametrize(
    "name", ["firecrawl-mcp_firecrawl_search", "other_prefix-portal_list_servers"]
)
def test_tools_outside_a_declared_prefix_are_denied(name: str) -> None:
    assert tool(name, PREFIXED) is Action.DENY
    assert tool(PREFIX + "firecrawl-mcp_firecrawl_search", PREFIXED) is Action.ALLOW
    # Without a prefix, as crypto-research-mcp lists its tools, the bare name is the tool.
    assert tool("firecrawl-mcp_firecrawl_search") is Action.ALLOW


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
    with pytest.raises(PolicyLoadError, match="value is missing"):
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
              escalation: {timeout: 5m, on_timeout: deny}
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
    # Second review: the whole host is reserved for the MCP proxy (other transports such
    # as /sse sit beside the declared path).
    assert decide("GET", "https://mcp.example.org/sse", loaded) is Action.DENY
    assert decide("GET", "https://mcp.example.org/docs", loaded) is Action.DENY


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


# Second review (515b5ea). Finding 1: an agent chooses its own Content-Type.

GITLAB_FILES = "https://gitlab.com/api/v4/projects/123/repository/files/git-upload-pack"
CODEBERG_CONTENTS = "https://codeberg.org/api/v1/repos/a/b/contents/git-upload-pack"


@pytest.mark.parametrize(
    ("url", "content_type", "has_body"),
    [
        (CODEBERG_CONTENTS, UPLOAD_PACK + "; x=json", True),  # Gitea binds JSON on "json"
        (CODEBERG_CONTENTS, UPLOAD_PACK + ";charset=utf-8", True),
        (GITLAB_FILES + "?branch=main&commit_message=x&content=SECRET", UPLOAD_PACK, False),
    ],
)
def test_git_classification_needs_the_exact_content_type_and_a_body(
    url: str, content_type: str, has_body: bool
) -> None:
    action = decide("POST", url, content_type=content_type, has_body=has_body, credentialed=True)
    assert action is Action.ESCALATE


# Second review, finding 2: resolved addresses and IPv6 forms in host lists.


@pytest.mark.parametrize("address", ["::ffff:10.0.0.5", "64:ff9b::a00:5", "10.0.0.5"])
def test_admin_hosts_match_every_form_of_a_resolved_address(address: str) -> None:
    floor = HardFloor(admin_hosts=frozenset({"10.0.0.5"}))
    assert floor.check_host(address) is not None
    decision = evaluate_request(
        HttpRequest("POST", "https://gateway.example.org/v1/policy", has_body=True),
        EXAMPLE,
        ceiling=OPEN,
        floor=floor,
        resolved=(address,),
    )
    assert decision.action is Action.DENY
    assert decision.layer == "floor"


def test_ceiling_deny_hosts_apply_to_resolved_addresses() -> None:
    ceiling = parse_ceiling('kind: PoolCeiling\nnetwork: {deny_hosts: ["93.184.215.14"]}\n')
    assert ceiling.check_host("::ffff:93.184.215.14") is not None
    for resolved in (("93.184.215.14",), ("::ffff:93.184.215.14",)):
        decision = evaluate_request(
            HttpRequest("GET", "http://93.184.215.14.nip.io/"),
            EXAMPLE,
            ceiling=ceiling,
            resolved=resolved,
        )
        assert decision.action is Action.DENY
        assert decision.layer == "ceiling"


@pytest.mark.parametrize(
    ("resolved", "layer"),
    [
        (("10.0.0.1",), "policy"),  # a public-looking name resolving to a LAN address
        (("8.8.8.8", "10.0.0.1"), "policy"),
        (("169.254.169.254",), "floor"),
        (("not-an-address",), "classification"),
    ],
)
def test_resolved_addresses_are_checked(resolved: tuple[str, ...], layer: str) -> None:
    decision = evaluate_request(
        HttpRequest("GET", "https://example.org/"), EXAMPLE, ceiling=OPEN, resolved=resolved
    )
    assert decision.action is Action.DENY
    assert decision.layer == layer


def test_public_resolved_addresses_keep_wildcard_allows() -> None:
    decision = evaluate_request(
        HttpRequest("GET", "https://example.org/"), EXAMPLE, ceiling=OPEN, resolved=("8.8.8.8",)
    )
    assert decision.action is Action.ALLOW


# Second review, finding 3: the MCP endpoint floor covers the whole host.


MCP_POLICY = policy(
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
          url: https://mcp-host.example.org/mcp
          revision: "1"
          auth: {mode: none}
    """
)


@pytest.mark.parametrize("path", ["/mcp", "/sse", "/messages/", "/", "/Mcp"])
def test_every_path_on_a_declared_mcp_host_is_reserved_for_the_proxy(path: str) -> None:
    url = "https://mcp-host.example.org" + path
    assert decide("GET", url, MCP_POLICY) is Action.DENY


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example.org/x/../mcp",
        "https://mcp.example.org/crypto%2Dresearch",
        "https://mcp.example.org/mcp;transport=http",
        "https://mcp.example.org/mcp?x=1",
    ],
)
def test_mcp_server_urls_need_plain_paths(url: str) -> None:
    with pytest.raises(PolicyLoadError, match="MCP server URL"):
        policy(
            f"""
            mcp:
              servers:
                tools: {{url: "{url}", revision: "1", auth: {{mode: none}}}}
            """
        )


# Second review, finding 4: escapes and off-screen entries.


@pytest.mark.parametrize(
    "entry", ['"\\x70ortal_toggle_servers"', '"portal\\u005ftoggle\\u005fservers"', '"a\\tb"']
)
def test_backslash_escapes_in_double_quoted_strings_are_rejected(entry: str) -> None:
    text = HEADER + f"mcp: {{proxy: {{worker_tools: [{entry}]}}}}\n"
    with pytest.raises(PolicyLoadError, match="backslash escapes"):
        parse_policy(text)


def test_single_quoted_and_plain_strings_still_work() -> None:
    loaded = parse_policy(
        HEADER
        + "mcp: {servers: {s: {url: 'https://mcp.example.org/x', revision: '1',\n"
        + "  auth: {mode: none}, tools: {allow: ['*_search', plain_name]}}}}\n"
    )
    assert loaded.policy.mcp.servers["s"].tools.allow == ("*_search", "plain_name")


def test_entries_cannot_hide_beyond_the_edge_of_the_screen() -> None:
    text = HEADER + 'mcp: {proxy: {worker_tools: ["*_search",' + " " * 400 + '"portal_x"]}}\n'
    with pytest.raises(PolicyLoadError, match=r"wider than|run of blanks"):
        parse_policy(text)


@pytest.mark.parametrize("hidden", ["\u00ad", "\u061c", "\u180e", "\U000e0041"])
def test_more_invisible_characters_are_rejected(hidden: str) -> None:
    with pytest.raises(PolicyLoadError, match="invisible"):
        parse_policy(HEADER + f"metadata_note: a{hidden}b\n")


# Second review, finding 5: a key without a value in a project policy.


@pytest.mark.parametrize(
    "body",
    [
        "mcp:\n  servers:\n    srv:\n      url: https://mcp.example.org/x\n      revision: '1'\n"
        "      auth: {mode: none}\n      tool_prefix:   # crypto_research_mcp-\n",
        "network:\n  rules:\n    - name: web\n      operation: http\n"
        "      match: {hosts: ['*'], methods: [GET]}\n      limits: { max_url_bytes: }\n"
        "      action: allow\n",
        "defaults:\n",
        "network: {rules: [null]}\n",
    ],
)
def test_keys_without_values_are_rejected_everywhere(body: str) -> None:
    with pytest.raises(PolicyLoadError, match="value is missing"):
        parse_policy(HEADER + body)


# Second review, finding 6: compound tool names must not ride on read patterns.


@pytest.mark.parametrize(
    "name",
    [
        "firecrawl-mcp_firecrawl_search_and_scrape",
        "paper-search-mcp_search_and_buy",
        "e-print-mcp_fetch_and_store",
        "web-fetch-mcp_fetch_post",
        "web-fetch-mcp_web_fetch_submit_form",
        "e-print-mcp_get_or_create_note",
        "attack-catalog-mcp_get_attack_and_update",
        "nist-mcp_find_and_replace",
        "e-print-mcp_list_purge",
    ],
)
def test_unknown_compound_tool_names_ask_first(name: str) -> None:
    assert tool(name) is Action.ESCALATE


def test_the_example_allows_tools_only_by_exact_name() -> None:
    server = EXAMPLE.policy.mcp.servers["crypto-research-mcp"]
    assert server.tools.allow
    assert not [pattern for pattern in server.tools.allow if "*" in pattern]


# Second review, findings 7 and 8.


def test_a_lone_surrogate_raises_a_policy_load_error() -> None:
    with pytest.raises(PolicyLoadError, match="UTF-8"):
        parse_policy(HEADER + 'metadata_note: "\ud800"\n')
    with pytest.raises(PolicyLoadError, match="UTF-8"):
        parse_ceiling('kind: PoolCeiling\nnote: "\ud800"\n')


def test_oracle_ipv6_metadata_is_on_the_floor() -> None:
    assert floor_reason("fd00:c1::a9fe:a9fe") is not None


# Second review, minor gaps closed in the same change.


@pytest.mark.parametrize(
    "body",
    [
        'mcp: {proxy: {worker_access: "yes"}}\n',
        "network:\n  rules:\n    - name: r\n      operation: http\n"
        "      match: {hosts: [example.org], ports: ['4_43']}\n      action: allow\n",
        'resources: {pids: "100"}\n',
    ],
)
def test_quoted_values_are_not_coerced(body: str) -> None:
    with pytest.raises(PolicyLoadError):
        parse_policy(HEADER + body)


@pytest.mark.parametrize("path", ["/v1/admin%EF%BC%8E", "/v1%EF%BC%8Fadmin", "/v1/ad%C2%ADmin"])
def test_paths_that_normalize_to_punctuation_or_lose_soft_hyphens_are_denied(path: str) -> None:
    assert decide("GET", "https://api.example.org" + path) is Action.DENY


# Third review (67c8fe6). Finding 1: padding inside the line-length limit.


GITHUB_READS = (
    "network:\n  rules:\n    - name: github-api-reads\n      operation: http\n"
    "      match: {{ hosts: [api.github.com], methods: [GET, HEAD,{pad}POST, PATCH, DELETE] }}\n"
    "      action: allow\n"
)


@pytest.mark.parametrize("pad", [" " * 200, " " * 40, "    ", "\t\t\t\t"])
def test_blank_runs_inside_a_line_are_rejected(pad: str) -> None:
    with pytest.raises(PolicyLoadError, match=r"run of blanks|wider than|invisible"):
        parse_policy(HEADER + GITHUB_READS.format(pad=pad))


def test_single_spaces_and_comment_alignment_are_accepted() -> None:
    loaded = parse_policy(
        HEADER
        + GITHUB_READS.format(pad=" ").replace("action: allow\n", "action: allow      # ok\n")
    )
    assert loaded.policy.network.rules[0].name == "github-api-reads"


@pytest.mark.parametrize("blank", ["\u3000", "\u00a0", "\u2003", "\u202f", "\u205f", "\u1680"])
def test_non_ascii_whitespace_is_rejected(blank: str) -> None:
    with pytest.raises(PolicyLoadError, match="invisible"):
        parse_policy(HEADER + f"defaults: {{action: escalate}}  # note{blank}x\n")


def test_wide_characters_count_double_toward_the_line_width() -> None:
    wide = "\u4e00" * 90  # 180 columns in 90 characters
    with pytest.raises(PolicyLoadError, match="wider than"):
        parse_policy(HEADER + f"# {wide}\n")


# Third review, finding 2: an MCP server declared by address, reached under a name.


def test_mcp_servers_declared_by_address_are_reserved_under_any_name() -> None:
    loaded = policy(
        """
        network:
          rules:
            - {name: web, operation: http, match: {hosts: ["*"], methods: [GET]}, action: allow}
        mcp:
          servers:
            tools: {url: "https://93.184.215.14/mcp", revision: "1", auth: {mode: none}}
        """
    )
    for resolved in (("93.184.215.14",), ("::ffff:93.184.215.14",)):
        decision = evaluate_request(
            HttpRequest("GET", "https://name.example.net/mcp"),
            loaded,
            ceiling=OPEN,
            resolved=resolved,
        )
        assert decision.action is Action.DENY
        assert decision.layer == "floor"


# Third review, finding 3: Windows best-fit characters and alternate data streams.


@pytest.mark.parametrize(
    "path",
    [
        "/v1/search/..%E2%88%95admin",  # U+2215 division slash
        "/v1/search%E2%81%84x",  # U+2044 fraction slash
        "/v1/search/%C2%A5x",  # U+00A5 yen sign, best-fit backslash
        "/v1/search/%E2%82%A9x",  # U+20A9 won sign
        "/v1/search/%E2%88%B6x",  # U+2236 ratio
        "/v1/admin::$DATA",
    ],
)
def test_windows_best_fit_punctuation_is_denied(path: str) -> None:
    loaded = policy(
        """
        network:
          rules:
            - name: search
              operation: http
              match: {hosts: [api.example.org], paths: [/v1/search], methods: [GET, POST]}
              action: allow
        """
    )
    assert decide("POST", "https://api.example.org" + path, loaded, has_body=True) is Action.DENY


# Third review, finding 4: quoted or infinite floats.


@pytest.mark.parametrize(
    "body",
    [
        'resources: {model_budget_usd_per_day: "inf"}\n',
        'resources: {model_budget_usd_per_day: "1e999"}\n',
        'resources: {cpu: "16"}\n',
        "resources: {cpu: 1e999}\n",
    ],
)
def test_resource_numbers_are_finite_and_unquoted(body: str) -> None:
    with pytest.raises(PolicyLoadError):
        parse_policy(HEADER + body)


def test_plain_resource_numbers_still_load() -> None:
    resources = parse_policy(HEADER + "resources: {cpu: 16, model_budget_usd_per_day: 12.5}\n")
    assert resources.policy.resources.cpu == 16
    assert resources.policy.resources.model_budget_usd_per_day == 12.5


# Third review, finding 5: control characters in MCP server URLs.


@pytest.mark.parametrize(
    "url", ["https://169.254.169.254\t.example.org/x", "https://mcp.example.org/a b"]
)
def test_mcp_server_urls_reject_whitespace_and_control_characters(url: str) -> None:
    # A literal tab is now rejected anywhere in a policy file; the URL check still
    # guards models built without the loader.
    with pytest.raises(PolicyLoadError, match=r"MCP server URLs|invisible"):
        policy(
            f"""
            mcp:
              servers:
                tools: {{url: "{url}", revision: "1", auth: {{mode: none}}}}
            """
        )


def test_git_content_type_must_be_ascii() -> None:
    url = "https://github.com/regenscheid/sandbox-crypto.git/git-upload-pack"
    kelvin = "application/x-git-upload-pac\u212a-request"  # lowercases to the git type
    action = decide("POST", url, content_type=kelvin, has_body=True)
    assert action is Action.ESCALATE


# Follow-up to the third review: the blank-run check must know where comments and
# quoted strings really are, including across lines.


def worker_tools(listing: str) -> str:
    return HEADER + f"mcp:\n  proxy:\n    worker_tools: {listing}\n"


@pytest.mark.parametrize(
    "listing",
    [
        '["a #",' + " " * 40 + '"portal_toggle_servers"]',  # " #" inside quotes is no comment
        "['a #'," + " " * 40 + "'portal_toggle_servers']",
        '["a\n      #",' + " " * 40 + '"portal_toggle_servers"]',  # quote continues on a new line
    ],
)
def test_quoted_hashes_do_not_open_a_comment(listing: str) -> None:
    with pytest.raises(PolicyLoadError, match=r"run of blanks|fit on one line|must end its line"):
        parse_policy(worker_tools(listing))


def test_blank_runs_inside_quoted_strings_are_rejected() -> None:
    with pytest.raises(PolicyLoadError, match="run of blanks"):
        parse_policy(HEADER + 'metadata_note: "a' + " " * 10 + 'b"\n')


@pytest.mark.parametrize(
    "tail",
    [
        "defaults: {action: escalate}          # aligned comment\n",
        "defaults: {action: escalate}  # a comment    with    gaps\n",
        "# a whole-line comment          with gaps\n",
        "defaults: {action: escalate}      \n",  # trailing blanks
    ],
)
def test_comment_alignment_and_trailing_blanks_are_accepted(tail: str) -> None:
    parse_policy(HEADER + tail)


def test_single_quotes_with_an_escaped_quote_are_accepted() -> None:
    text = HEADER.replace(
        "metadata: {name: test}", "metadata: {name: test, description: 'it''s'}      # note"
    )
    assert parse_policy(text).policy.metadata.description == "it's"


def test_deep_indentation_is_rejected() -> None:
    text = HEADER + "mcp:\n  proxy:\n    worker_tools: [\n" + " " * 60 + "portal_toggle_servers ]\n"
    with pytest.raises(PolicyLoadError, match="indented by more than"):
        parse_policy(text)


@pytest.mark.parametrize("style", ["|", ">", "|-", ">+"])
def test_block_scalars_are_rejected(style: str) -> None:
    text = HEADER.replace(
        "metadata: {name: test}\n", f"metadata:\n  name: test\n  description: {style}\n    text\n"
    )
    with pytest.raises(PolicyLoadError, match=r"block scalars|fit on one line"):
        parse_policy(text)


# Fourth review (b1bcbf2): layout checks now take positions from PyYAML's scanner.


def test_quoted_values_cannot_swallow_lines_that_look_like_settings() -> None:
    text = (
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\n"
        'metadata:\n  name: t\n  description: "Broad read access, governed side effects\n\n'
        "# Applies to anything no rule matches.\n"
        "defaults:\n  action: deny # allow | alert | escalate | deny\n"
        '  log: true # keep a record"\n'
    )
    with pytest.raises(PolicyLoadError, match="fit on one line"):
        parse_policy(text)


def test_a_stray_quote_in_a_plain_value_does_not_hide_padding() -> None:
    text = (
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\n"
        'metadata: {name: t, description: read "only}\n'
        "mcp:\n  servers:\n    s: {url: 'https://m.io/x', auth: {mode: none}, "
        'tool_prefix: p-, revision: "1 #' + " " * 36 + '", tools: {allow: [x]}}\n'
    )
    with pytest.raises(PolicyLoadError, match=r"run of blanks|must end its line"):
        parse_policy(text)


@pytest.mark.parametrize("blank", ["\u2800", "\u3164", "\uffa0", "\u115f", "\u1160", "\t"])
def test_blank_looking_characters_are_rejected(blank: str) -> None:
    text = HEADER + f'metadata_note: "1{blank * 3}.{blank * 3}"\n'
    with pytest.raises(PolicyLoadError, match="invisible"):
        parse_policy(text)


@pytest.mark.parametrize("value", ["caf\u00e9", "\u0430dmin", "\u4e00"])
def test_settings_are_ascii(value: str) -> None:
    with pytest.raises(PolicyLoadError, match="ASCII"):
        parse_policy(HEADER.replace("name: test}", f"name: test, description: '{value}'}}"))


def test_comments_may_use_other_scripts() -> None:
    parse_policy(HEADER + "# caf\u00e9 \u4e00\u4e8c\n")


# Fifth review (8212708): a '#' inside a value must not make live settings look like a comment.


def test_a_hash_inside_a_value_must_end_its_line() -> None:
    text = (
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: t}\n"
        "mcp:\n  servers:\n    s: {url: 'https://mcp.example.org/x', auth: {mode: none}, "
        'tool_prefix: p-, revision: "2025-11-25 # pinned", '
        "tools: {allow: [portal_toggle_servers]}}\n"
    )
    with pytest.raises(PolicyLoadError, match="must end its line"):
        parse_policy(text)


def test_a_hash_in_the_last_value_on_a_line_is_accepted() -> None:
    text = HEADER.replace(
        "metadata: {name: test}\n", "metadata:\n  name: test\n  description: 'see issue #12'\n"
    )
    assert parse_policy(text).policy.metadata.description == "see issue #12"


# Compiler review (5c341b0): package indexes are reachable only through the mirror, so a
# read rule cannot fetch packages around the packages section.


@pytest.mark.parametrize(
    "url",
    [
        "https://conda.anaconda.org/bioconda/linux-64/samtools-1.0-0.conda",
        "https://files.pythonhosted.org/packages/aa/bb/numpy-2.0-cp313.whl",
        "https://pypi.org/simple/numpy/",
        "https://repo.anaconda.com/pkgs/main/linux-64/repodata.json",
    ],
)
def test_package_indexes_are_reachable_only_through_the_mirror(url: str) -> None:
    decision = evaluate_request(HttpRequest("GET", url), EXAMPLE, ceiling=OPEN)
    assert decision.action is Action.DENY
    assert decision.layer == "floor"
