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
    Decision,
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


class Tool(TypedDict):
    name: str
    # READ, SPEND, WRITE or CONFIG, from each tool's own description (2026-10-10).
    class_: str


# The class each tool of the 2026-10-10 notes takes in the checked-in classification,
# where it differs from the notes' word for it (D96, H1). Cost is not a reason to ask:
# the services limit and bill their own use, so SPEND tools are reads.
RECLASSED = {
    # Reads that fill the server's own document cache on first use (D95: reads).
    "e-print-mcp_eprint_file": "read",
    "e-print-mcp_eprint_fulltext": "read",
    "nist-mcp_csrc_file": "read",
    "nist-mcp_csrc_fulltext": "read",
    # Reads by name; their effects come from argument rules (H1).
    "firecrawl-mcp_firecrawl_crawl": "read",
    "firecrawl-mcp_firecrawl_scrape": "read",
    "firecrawl-mcp_firecrawl_parse": "read",
    # Browser automation, off by default.
    "firecrawl-mcp_firecrawl_agent": "browse",
    "firecrawl-mcp_firecrawl_interact": "browse",
    "firecrawl-mcp_firecrawl_interact_stop": "browse",
    # Monitors that notify later.
    "firecrawl-mcp_firecrawl_monitor_create": "communicate",
    "firecrawl-mcp_firecrawl_monitor_update": "communicate",
    # Shared corpora rebuilt for everyone.
    "e-print-mcp_refresh_eprint": "admin",
    "nist-mcp_refresh_csrc": "admin",
}
BY_NOTE = {"READ": "read", "SPEND": "read", "WRITE": "write", "CONFIG": "admin"}
EXPECTED_BY_CLASS = {
    "read": Action.ALLOW,
    "write": Action.ESCALATE,
    "communicate": Action.ESCALATE,
    "admin": Action.ESCALATE,
    "browse": Action.DENY,
}


def load_tools() -> list[Tool]:
    data = json.loads(FIXTURE.read_text())
    return [Tool(name=entry["name"], class_=entry["class"]) for entry in data["tools"]]


@pytest.fixture(scope="module")
def policy() -> LoadedPolicy:
    return load_policy(EXAMPLE)


def call(policy: LoadedPolicy, tool: str, arguments: dict[str, object] | None = None) -> Decision:
    operation = McpOperation(
        server="crypto-research-mcp", method="tools/call", tool=tool, arguments=arguments
    )
    return evaluate(operation, policy, ceiling=CEILING)


@pytest.mark.parametrize("tool", load_tools(), ids=lambda tool: tool["name"])
def test_every_crypto_research_mcp_tool(policy: LoadedPolicy, tool: Tool) -> None:
    name = tool["name"]
    effect = RECLASSED.get(name, BY_NOTE[tool["class_"]])
    decision = call(policy, name)
    if name.startswith("portal_"):
        # Portal configuration is denied by pattern; listing its servers is allowed by name.
        expected = Action.ALLOW if name == "portal_list_servers" else Action.DENY
    else:
        expected = EXPECTED_BY_CLASS[effect]
        assert decision.effect == effect, decision.reason
    assert decision.action is expected, decision.reason


def test_fixture_covers_the_reclassed_tools() -> None:
    names = {tool["name"] for tool in load_tools()}
    assert names >= set(RECLASSED)
    assert len(names) == 78


@pytest.mark.parametrize(
    ("tool", "arguments", "effect", "action"),
    [
        ("firecrawl-mcp_firecrawl_scrape", {"url": "https://e.org/"}, "read", Action.ALLOW),
        (
            "firecrawl-mcp_firecrawl_scrape",
            {"url": "https://e.org/", "actions": []},
            "read",
            Action.ALLOW,
        ),
        (
            "firecrawl-mcp_firecrawl_scrape",
            {"url": "https://e.org/", "actions": [{"type": "click"}]},
            "browse",
            Action.DENY,
        ),
        (
            "firecrawl-mcp_firecrawl_scrape",
            {"url": "https://e.org/", "profile": {"name": "p", "saveChanges": True}},
            "write",
            Action.ESCALATE,
        ),
        (
            "firecrawl-mcp_firecrawl_scrape",
            {"url": "https://e.org/", "profile": {"name": "p", "saveChanges": False}},
            "read",
            Action.ALLOW,
        ),
        (
            "firecrawl-mcp_firecrawl_crawl",
            {"url": "https://e.org/", "webhook": "https://h.example/"},
            "communicate",
            Action.ESCALATE,
        ),
        (
            "firecrawl-mcp_firecrawl_crawl",
            {
                "url": "https://e.org/",
                "webhook": "https://h.example/",
                "scrapeOptions": {"actions": [{"type": "write"}]},
            },
            "browse",
            Action.DENY,
        ),
        (
            "firecrawl-mcp_firecrawl_search",
            {"query": "SNOVA", "scrapeOptions": {"actions": [{"type": "press"}]}},
            "browse",
            Action.DENY,
        ),
        ("firecrawl-mcp_firecrawl_agent", {"prompt": "find papers"}, "browse", Action.DENY),
    ],
)
def test_firecrawl_argument_rules(
    policy: LoadedPolicy, tool: str, arguments: dict[str, object], effect: str, action: Action
) -> None:
    """H1: the arguments that act on the world make the call an effect, whatever its name."""
    decision = call(policy, tool, arguments)
    assert (decision.effect, decision.action) == (effect, action), decision.reason


def test_escalations_carry_the_class_ceiling(policy: LoadedPolicy) -> None:
    feedback = call(policy, "firecrawl-mcp_firecrawl_search_feedback")
    assert (feedback.action, feedback.effect, feedback.max_grant) == (
        Action.ESCALATE,
        "write",
        "project",
    )
    monitor = call(policy, "firecrawl-mcp_firecrawl_monitor_create")
    assert (monitor.effect, monitor.max_grant) == ("communicate", "session")
    refresh = call(policy, "e-print-mcp_refresh_eprint")
    assert (refresh.effect, refresh.max_grant) == ("admin", "once")


def test_a_made_up_tool_is_unclassified_and_asks(policy: LoadedPolicy) -> None:
    decision = call(policy, "e-print-mcp_get_or_create_note")
    assert (decision.action, decision.effect, decision.max_grant) == (
        Action.ESCALATE,
        "unclassified",
        "once",
    )
    assert decision.layer == "default"


@pytest.mark.parametrize(
    ("method", "url", "expected", "rule"),
    [
        ("GET", "https://eprint.iacr.org/2024/123.pdf", Action.ALLOW, "research-hosts"),
        ("GET", "https://example.org/paper.pdf", Action.ALLOW, "read-only-web"),
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
        # D95: a fetch from a git host the policy does not list is a read.
        ("POST", "https://git.example.org/x.git/git-upload-pack", Action.ALLOW, "git-fetch"),
        ("POST", "https://git.example.org:8443/x.git/git-upload-pack", Action.ALLOW, "git-fetch"),
        ("POST", "https://git.example.org/x.git/git-receive-pack", Action.ESCALATE, "git-push"),
        # D95: GET and HEAD on any port are reads.
        ("GET", "https://example.org:8443/", Action.ALLOW, "read-only-web"),
        ("GET", "http://example.org:8080/data.csv", Action.ALLOW, "read-only-web"),
        ("HEAD", "https://gmplib.org:8443/", Action.ALLOW, "source-hosts"),
        # Reviewed read-only POST endpoints; any other POST asks first.
        (
            "POST",
            "https://api.semanticscholar.org/graph/v1/paper/batch",
            Action.ALLOW,
            "semantic-scholar-reads",
        ),
        (
            "POST",
            "https://api.semanticscholar.org/recommendations/v1/papers/",
            Action.ALLOW,
            "semantic-scholar-reads",
        ),
        ("POST", "https://export.arxiv.org/api/query", Action.ALLOW, "arxiv-query"),
        (
            "POST",
            "https://api.semanticscholar.org/graph/v1/paper",
            Action.ESCALATE,
            "external-writes",
        ),
        ("POST", "https://export.arxiv.org/api/other", Action.ESCALATE, "external-writes"),
        ("PUT", "https://export.arxiv.org/api/query", Action.ESCALATE, "external-writes"),
        ("POST", "https://api.crossref.org/works", Action.ESCALATE, "external-writes"),
        # A write on a non-default port: no rule, so the default asks.
        ("POST", "https://example.org:8443/api", Action.ESCALATE, None),
        # Non-public destinations: a wildcard allow cannot grant them.
        ("GET", "http://192.168.1.10/admin", Action.DENY, "read-only-web"),
        ("GET", "http://nas.local/", Action.DENY, "read-only-web"),
        # Hard floor.
        ("GET", "http://169.254.169.254/latest/meta-data/", Action.DENY, None),
        ("GET", "http://localhost:8080/", Action.DENY, None),
        # D95 sets no URL length limit on reads.
        ("GET", "https://example.org/" + "a" * 5000, Action.ALLOW, "read-only-web"),
        # Ambiguous request.
        ("GET", "https://example.org/a/../admin", Action.DENY, None),
        ("GET", "https://example.org/admin;jsessionid=1", Action.DENY, None),
        ("GET", "https://example.org/admin%3Bx", Action.DENY, None),
    ],
)
def test_example_network_rules(
    policy: LoadedPolicy, method: str, url: str, expected: Action, rule: str | None
) -> None:
    content_type = None
    if method == "POST" and url.endswith(("/git-upload-pack", "/git-receive-pack")):
        content_type = f"application/x-{url.rsplit('/', 1)[1]}-request"
    has_body = method in ("POST", "PUT", "PATCH")
    request = HttpRequest(method, url, content_type=content_type, has_body=has_body)
    decision = evaluate_request(request, policy, ceiling=CEILING)
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
        # EasyCrypt and Jasmin come from opam, Cryptol and Tamarin from Hackage.
        (PackageOperation("opam", None, digest=f"sha256:{'a' * 64}"), Action.ALLOW),
        (PackageOperation("opam", None, kind="index"), Action.ALLOW),
        (PackageOperation("hackage", "cryptol"), Action.ALLOW),
        (PackageOperation("hackage", None, kind="index"), Action.ALLOW),
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
        # Research code has no MCP access (D98: the MCP proxy retired).
        ({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "worker", Action.DENY),
        ({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "orchestrator", Action.ALLOW),
        (
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "paper-search-mcp_arxiv_search", "arguments": {}},
            },
            "worker",
            Action.DENY,
        ),
        (
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "paper-search-mcp_arxiv_search", "arguments": {}},
            },
            "orchestrator",
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


def test_example_sets_no_exfiltration_limits(policy: LoadedPolicy) -> None:
    # D95: no egress-volume monitor and no URL limits by default; both remain available.
    assert evaluate_monitors(policy, EgressUsage(10**15)) == ()
    assert all(rule.limits is None for rule in policy.policy.network.rules)


PRE_ADMITTED = (
    # Research destinations.
    "eprint.iacr.org",
    "arxiv.org",
    "zenodo.org",
    "huggingface.co",
    "dblp.org",
    "www.iacr.org",
    "csrc.nist.gov",
    "api.crossref.org",
    "api.openalex.org",
    "gitlab.inria.fr",
    # Code sources (D97).
    "github.com",
    "gitlab.com",
    "codeberg.org",
    "bitbucket.org",
    "sourceforge.net",
    "ftp.gnu.org",
    "gmplib.org",
    "flintlib.org",
    "www.sagemath.org",
)


@pytest.mark.parametrize("host", PRE_ADMITTED)
def test_research_and_source_hosts_are_named_so_they_need_no_admission(
    policy: LoadedPolicy, host: str
) -> None:
    named = {
        pattern.value
        for rule in policy.policy.network.rules
        if rule.name in ("research-hosts", "source-hosts")
        for pattern in rule.match.hosts
    }
    assert host in named
    for method in ("GET", "HEAD"):
        decision = evaluate_request(
            HttpRequest(method, f"https://{host}/x"), policy, ceiling=CEILING
        )
        assert decision.action is Action.ALLOW, decision.reason
    # Writes to them still ask first, and pushes too.
    write = evaluate_request(HttpRequest("POST", f"https://{host}/x"), policy, ceiling=CEILING)
    assert write.action is Action.ESCALATE
    push = evaluate_request(
        HttpRequest(
            "POST",
            f"https://{host}/r.git/git-receive-pack",
            content_type="application/x-git-receive-pack-request",
            has_body=True,
        ),
        policy,
        ceiling=CEILING,
    )
    assert (push.action, push.rule) == (Action.ESCALATE, "git-push")
    assert push.escalation is not None
    assert push.escalation.on_timeout is Action.DENY


def test_example_resources_are_capped_by_the_ceiling(policy: LoadedPolicy) -> None:
    ceiling = PoolCeiling.model_validate({"resources": {"memory": "32Gi", "cpu": 32}})
    resources, notices = effective_resources(policy.policy, ceiling)
    assert resources.memory == 32 * 1024**3
    assert resources.cpu == 16
    assert resources.wall_time == timedelta(hours=72)
    assert len(notices) == 1
    assert "memory" in notices[0]
