# SPDX-License-Identifier: MIT
"""Specification tests for request classification (docs/POLICY.md, "Classify the operation").

These tests are derived from the specification, not from the implementation. They cover git
smart-HTTP recognition, the request shapes that must be rejected as ambiguous, host, port and
method normalization, MCP JSON-RPC message classification, and how the ``evaluate_*`` entry
points turn classification failures into denials.
"""

import textwrap
from urllib.parse import urlsplit

import pytest

from sandbox_science.policy import (
    Action,
    ClassificationError,
    GitOperation,
    HttpOperation,
    HttpRequest,
    LoadedPolicy,
    PoolCeiling,
    classify_http_request,
    classify_mcp_message,
    evaluate_mcp_message,
    evaluate_request,
    parse_policy,
)

HEADER = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: test
"""

REPO = "https://git.example.org/team/repo.git"

# Allows every HTTP and git request, so any denial below comes from classification.
ALLOW_ALL_NETWORK = """
    defaults: {action: allow}
    network:
      rules:
        - {name: any-http, operation: http, match: {hosts: ["*"]}, action: allow}
        - {name: any-git, operation: git, match: {hosts: ["*"]}, action: allow}
"""

# Allows every tool on one MCP server; worker_access keeps its default (false).
ALLOW_ALL_MCP = """
    defaults: {action: allow}
    mcp:
      servers:
        search:
          url: https://mcp.example.org/mcp
          revision: "2025-11-25"
          auth: {mode: none}
          tools: {allow: ["*"], escalate: [], deny: []}
"""


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


def classify(method: str, url: str, *, credentialed: bool = False) -> HttpOperation | GitOperation:
    return classify_http_request(client_request(method, url, credentialed=credentialed))


def classify_http(method: str, url: str) -> HttpOperation:
    operation = classify(method, url)
    assert isinstance(operation, HttpOperation)
    return operation


def classify_git(method: str, url: str) -> GitOperation:
    operation = classify(method, url)
    assert isinstance(operation, GitOperation)
    return operation


def tool_call(name: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}


# --- Git smart-HTTP ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "service"),
    [
        pytest.param(
            "GET", f"{REPO}/info/refs?service=git-upload-pack", "upload-pack", id="fetch-discovery"
        ),
        pytest.param("POST", f"{REPO}/git-upload-pack", "upload-pack", id="fetch"),
        pytest.param(
            "GET", f"{REPO}/info/refs?service=git-receive-pack", "receive-pack", id="push-discovery"
        ),
        pytest.param("POST", f"{REPO}/git-receive-pack", "receive-pack", id="push"),
    ],
)
def test_git_smart_http_requests_are_git_operations(method: str, url: str, service: str) -> None:
    operation = classify_git(method, url)
    assert operation.service == service
    assert operation.method == method
    assert operation.host == "git.example.org"


@pytest.mark.parametrize(
    ("method", "url"),
    [("GET", f"{REPO}/info/refs?service=git-upload-pack"), ("POST", f"{REPO}/git-receive-pack")],
)
def test_git_repo_path_is_the_part_before_the_service_suffix(method: str, url: str) -> None:
    assert classify_git(method, url).repo_path == "/team/repo.git"


def test_post_to_git_upload_pack_is_a_fetch_not_an_http_post() -> None:
    # Git fetch sends its reads as POST; it must not reach generic HTTP method rules.
    operation = classify("POST", f"{REPO}/git-upload-pack")
    assert isinstance(operation, GitOperation)
    assert operation.service == "upload-pack"


@pytest.mark.parametrize(
    "path",
    [
        "/info/refs",
        "/HEAD",
        "/objects/info/packs",
        "/objects/12/3456789abcdef0123456789abcdef012345678",
    ],
)
def test_dumb_http_git_reads_are_plain_http_gets(path: str) -> None:
    operation = classify_http("GET", f"{REPO}{path}")
    assert operation.method == "GET"
    assert operation.path == f"/team/repo.git{path}"


@pytest.mark.parametrize(
    "query",
    [
        "service=git-upload-pack&service=git-upload-pack",
        "service=git-upload-pack&service=git-receive-pack",
        "service=git-receive-pack&service=git-upload-pack",
    ],
)
def test_repeated_git_service_parameter_is_rejected(query: str) -> None:
    with pytest.raises(ClassificationError):
        classify("GET", f"{REPO}/info/refs?{query}")


def test_git_operation_records_method_destination_and_size() -> None:
    url = f"{REPO}/git-upload-pack"
    operation = classify_git("POST", url)
    assert operation.method == "POST"
    assert operation.scheme == "https"
    assert operation.host == "git.example.org"
    assert operation.port == 443
    assert operation.url_bytes == len(url.encode())


@pytest.mark.parametrize("credentialed", [False, True])
def test_credential_flag_is_carried_into_http_and_git_operations(credentialed: bool) -> None:
    http = classify("GET", "https://example.org/", credentialed=credentialed)
    git = classify("POST", f"{REPO}/git-receive-pack", credentialed=credentialed)
    assert http.credentialed == credentialed
    assert git.credentialed == credentialed


# --- Ambiguous requests are rejected -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.org/",
        "https://test-user:test-secret-value@example.org/",
        "https://example.org@example.com/",
        "https://@example.org/",
        "https://user@example.org:8443/",
    ],
)
def test_user_information_in_the_url_is_rejected(url: str) -> None:
    with pytest.raises(ClassificationError):
        classify("GET", url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://b\u00fccher.example.org/", id="latin-umlaut"),
        pytest.param("https://ex\u0430mple.org/", id="cyrillic-a"),
        pytest.param("https://\uff45xample.org/", id="fullwidth-e"),
        pytest.param("https://example\u3002org/", id="ideographic-full-stop"),
    ],
)
def test_non_ascii_host_names_are_rejected(url: str) -> None:
    with pytest.raises(ClassificationError):
        classify("GET", url)


@pytest.mark.parametrize(
    "host",
    [
        "2130706433",
        "0x7f.1",
        "0x7f000001",
        "0177.0.0.1",
        "127.1",
        "010.8.8.8",
        "8.8.8.010",
        "256.1.1.1",
        "1.2.3.4.5",
        "0",
    ],
)
def test_numeric_looking_hosts_that_are_not_canonical_ips_are_rejected(host: str) -> None:
    with pytest.raises(ClassificationError):
        classify("GET", f"https://{host}/")


@pytest.mark.parametrize("path", ["/a%zz", "/a%2", "/a%", "/%G0", "/a%%20"])
def test_invalid_percent_encoding_is_rejected(path: str) -> None:
    with pytest.raises(ClassificationError):
        classify("GET", f"https://example.org{path}")


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/a%2Fb", id="slash"),
        pytest.param("/a%2fb", id="slash-lowercase"),
        pytest.param("/a%5Cb", id="backslash"),
        pytest.param("/a%5cb", id="backslash-lowercase"),
        pytest.param("/report%2Etxt", id="dot"),
        pytest.param("/v1/%2e%2e/admin", id="dot-segment"),
        pytest.param("/a%00b", id="nul"),
    ],
)
def test_encoded_slash_backslash_dot_or_nul_in_path_is_rejected(path: str) -> None:
    with pytest.raises(ClassificationError):
        classify("GET", f"https://example.org{path}")


@pytest.mark.parametrize(
    "path", ["/a/../b", "/a/./b", "/..", "/.", "/a/..", "/a/.", "/a//b", "//a"]
)
def test_dot_and_empty_path_segments_are_rejected(path: str) -> None:
    with pytest.raises(ClassificationError):
        classify("GET", f"https://example.org{path}")


def test_path_rules_do_not_apply_to_the_query_string() -> None:
    operation = classify_http("GET", "https://example.org/search?q=a%2Fb&next=../x//y")
    assert operation.path == "/search"


# --- Normalization -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://Example.ORG/", "example.org"),
        ("https://example.org./", "example.org"),
        ("https://WWW.Example.Org.:8443/x", "www.example.org"),
        ("https://8.8.8.8/", "8.8.8.8"),
        ("https://xn--bcher-kva.example.org/", "xn--bcher-kva.example.org"),
        ("https://123.example.org/", "123.example.org"),
    ],
)
def test_host_is_lowercased_without_trailing_dot(url: str, host: str) -> None:
    assert classify_http("GET", url).host == host


@pytest.mark.parametrize(
    ("url", "scheme", "port"),
    [
        ("https://example.org/", "https", 443),
        ("http://example.org/", "http", 80),
        ("https://example.org:443/", "https", 443),
        ("https://example.org:8443/", "https", 8443),
        ("http://example.org:8080/", "http", 8080),
    ],
)
def test_port_defaults_from_the_scheme(url: str, scheme: str, port: int) -> None:
    operation = classify_http("GET", url)
    assert operation.scheme == scheme
    assert operation.port == port


@pytest.mark.parametrize("method", ["get", "gEt", "post", "Delete"])
def test_methods_must_already_be_upper_case(method: str) -> None:
    # HTTP methods are case-sensitive (RFC 9110); a server may not treat "get" as GET.
    with pytest.raises(ClassificationError, match="upper case"):
        classify(method, "https://example.org/")


def test_path_is_percent_decoded() -> None:
    assert classify_http("GET", "https://example.org/v1/a%20b/%7Euser").path == "/v1/a b/~user"


def test_url_bytes_is_the_length_of_the_full_url_including_the_query() -> None:
    url = "https://example.org/caf%C3%A9?q=" + "x" * 40
    assert classify_http("GET", url).url_bytes == len(url.encode())


# --- evaluate_request turns classification failures into denials --------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.org/",
        "https://2130706433/",
        "https://example.org/a%2Fb",
        "https://example.org/a/../b",
        f"{REPO}/info/refs?service=git-upload-pack&service=git-upload-pack",
    ],
)
def test_evaluate_request_denies_ambiguous_requests_at_classification(url: str) -> None:
    decision = evaluate_request(
        HttpRequest(method="GET", url=url),
        load(ALLOW_ALL_NETWORK),
        ceiling=PoolCeiling.unrestricted(),
    )
    assert decision.action == Action.DENY
    assert decision.layer == "classification"
    assert decision.reason


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/file",
        "file:///etc/passwd",
        "https:///no-host",
        "https://example.org:99999/",
        "https://example.org:port/",
        "example.org/no-scheme",
        "",
    ],
)
def test_evaluate_request_denies_unsupported_or_malformed_urls(url: str) -> None:
    # Fail closed: a URL that is not a well-formed http(s) URL is never let through.
    decision = evaluate_request(
        HttpRequest(method="GET", url=url),
        load(ALLOW_ALL_NETWORK),
        ceiling=PoolCeiling.unrestricted(),
    )
    assert decision.action == Action.DENY


def test_evaluate_request_allows_a_well_formed_request_under_the_same_policy() -> None:
    decision = evaluate_request(
        HttpRequest(method="GET", url="https://example.org/docs?page=2"),
        load(ALLOW_ALL_NETWORK),
        ceiling=PoolCeiling.unrestricted(),
    )
    assert decision.action == Action.ALLOW


# --- MCP JSON-RPC messages -----------------------------------------------------------------------


def test_tools_call_is_classified_by_server_method_and_tool_name() -> None:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "web_search", "arguments": {"query": "lattice reduction"}},
    }
    operation = classify_mcp_message("search", message)
    assert operation.server == "search"
    assert operation.method == "tools/call"
    assert operation.tool == "web_search"
    assert operation.origin == "orchestrator"


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, id="init"),
        pytest.param({"jsonrpc": "2.0", "method": "notifications/initialized"}, id="notification"),
        pytest.param({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, id="tools-list"),
        pytest.param(
            {"jsonrpc": "2.0", "id": 3, "method": "prompts/get", "params": {"name": "summarize"}},
            id="prompt-name-is-not-a-tool",
        ),
    ],
)
def test_methods_other_than_tools_call_carry_no_tool(message: dict[str, object]) -> None:
    operation = classify_mcp_message("search", message)
    assert operation.method == message["method"]
    assert operation.tool is None


def test_worker_origin_is_recorded() -> None:
    operation = classify_mcp_message("search", tool_call("web_search"), origin="worker")
    assert operation.origin == "worker"


@pytest.mark.parametrize(
    "message",
    [
        pytest.param([], id="empty"),
        pytest.param([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], id="single"),
        pytest.param(
            [{"jsonrpc": "2.0", "id": 1, "method": "ping"}, tool_call("web_search")], id="several"
        ),
    ],
)
def test_json_rpc_batches_are_rejected(message: list[object]) -> None:
    with pytest.raises(ClassificationError):
        classify_mcp_message("search", message)


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"jsonrpc": "2.0", "id": 1}, id="no-method"),
        pytest.param({"jsonrpc": "2.0", "id": 1, "result": {}}, id="response"),
        pytest.param({"jsonrpc": "2.0", "id": 1, "method": None}, id="null-method"),
        pytest.param({"jsonrpc": "2.0", "id": 1, "method": 7}, id="numeric-method"),
        pytest.param({"jsonrpc": "2.0", "id": 1, "method": ["tools/list"]}, id="list-method"),
        pytest.param("tools/list", id="bare-string"),
        pytest.param(None, id="null"),
    ],
)
def test_messages_without_a_string_method_are_rejected(message: object) -> None:
    with pytest.raises(ClassificationError):
        classify_mcp_message("search", message)


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, id="no-params"),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}, id="no-name"
        ),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": None}},
            id="null-name",
        ),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 3}},
            id="numeric-name",
        ),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ["web_search"]}},
            id="list-name",
        ),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["web_search"]},
            id="positional-params",
        ),
    ],
)
def test_tools_call_without_a_string_tool_name_is_rejected(message: object) -> None:
    with pytest.raises(ClassificationError):
        classify_mcp_message("search", message)


@pytest.mark.parametrize(
    "message",
    [
        pytest.param([tool_call("web_search")], id="batch"),
        pytest.param({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, id="tools-call-no-name"),
        pytest.param({"jsonrpc": "2.0", "id": 1}, id="no-method"),
    ],
)
def test_evaluate_mcp_message_denies_unclassifiable_messages(message: object) -> None:
    decision = evaluate_mcp_message(
        "search", message, load(ALLOW_ALL_MCP), ceiling=PoolCeiling.unrestricted()
    )
    assert decision.action == Action.DENY
    assert decision.layer == "classification"


def test_evaluate_mcp_message_denies_an_empty_method() -> None:
    # Whether the empty method fails classification or the method check, it is never allowed.
    decision = evaluate_mcp_message(
        "search",
        {"jsonrpc": "2.0", "id": 1, "method": ""},
        load(ALLOW_ALL_MCP),
        ceiling=PoolCeiling.unrestricted(),
    )
    assert decision.action == Action.DENY


def test_evaluate_mcp_message_decides_a_classified_tool_call() -> None:
    decision = evaluate_mcp_message(
        "search", tool_call("web_search"), load(ALLOW_ALL_MCP), ceiling=PoolCeiling.unrestricted()
    )
    assert decision.action == Action.ALLOW


def test_evaluate_mcp_message_passes_the_origin_through() -> None:
    # worker_access defaults to false, so the same call is denied when it comes from worker code.
    decision = evaluate_mcp_message(
        "search",
        tool_call("web_search"),
        load(ALLOW_ALL_MCP),
        origin="worker",
        ceiling=PoolCeiling.unrestricted(),
    )
    assert decision.action == Action.DENY
