# SPDX-License-Identifier: MIT
"""What approval cards show (``summary``) and how widely a request may be granted
(``ceilings``), D97."""

from __future__ import annotations

import json

from sandbox_science.policy.approvals import (
    allowed,
    call_summary,
    http_summary,
    network_max_grant,
    offered,
    widest,
)
from sandbox_science.policy.classification import catalog


def test_a_call_is_summarized_by_its_classifications_significant_arguments() -> None:
    tools = catalog("crypto-research-mcp").tools
    scrape = tools["firecrawl-mcp_firecrawl_scrape"]
    summary = call_summary(
        scrape.significant,
        {"url": "https://eprint.iacr.org/2024/1\x07", "formats": ["markdown"], "actions": [{}]},
    )
    assert summary == {"url": "https://eprint.iacr.org/2024/1", "host": "eprint.iacr.org"}
    # Arguments the classification does not name are not shown.
    assert "formats" not in json.dumps(summary)


def test_a_list_of_urls_names_the_first_few_and_the_host_of_the_first() -> None:
    summary = call_summary({"url": "urls"}, {"urls": [f"https://a{n}.example/x" for n in range(5)]})
    assert summary["url"].endswith("(+2 more)")
    assert summary["host"] == "a0.example"


def test_long_values_are_truncated_and_objects_counted() -> None:
    summary = call_summary(
        {"title": "title", "size": "n", "query": "q"},
        {"title": "t" * 1000, "n": 12, "q": {"a": 1, "b": 2}},
    )
    assert len(summary["title"]) == 160
    assert summary["title"].endswith("…")
    assert (summary["size"], summary["query"]) == ("12", "{2 fields}")
    assert call_summary({"url": "url"}, None) == {}


def test_an_http_summary_lists_a_json_bodys_top_level_fields_only() -> None:
    body = json.dumps({"to": "x@example.org", "body": "b" * 300, "nested": {"deep": 1}}).encode()
    summary = http_summary("POST", "api.example.org", 443, "/send", "application/json", body)
    assert summary["size"] == len(body)
    fields = summary["fields"]
    assert isinstance(fields, dict)
    assert fields["to"] == "x@example.org"
    assert fields["nested"] == "{1 fields}"
    assert "deep" not in json.dumps(summary)
    # Not JSON, or not an object: summarized by its size only.
    for content_type, data in (("text/plain", body), ("application/json", b"[1]")):
        assert "fields" not in http_summary("POST", "h", 443, "/", content_type, data)


def test_network_requests_from_code_are_granted_by_host_for_reads_only() -> None:
    assert network_max_grant("GET") == "project"
    assert network_max_grant("head") == "project"
    assert network_max_grant("POST") == "once"


def test_a_grant_is_never_wider_than_its_ceiling() -> None:
    assert offered("project", "write", session=True) == ["once", "session", "project"]
    assert offered("project", "write", session=False) == ["once", "project"]
    assert offered("session", "communicate", session=True) == ["once", "session"]
    assert not allowed("project", "session", "communicate")
    assert offered("none", "admin", session=True) == ["once"]
    assert offered(None, None, session=True) == ["once"]  # an unknown ceiling: once
    assert not allowed("forever", "project")


def test_an_unclassified_call_is_never_granted_wider_than_once() -> None:
    # Whatever the policy's ceiling says.
    assert widest("project", "unclassified") == "once"
    assert offered("project", "unclassified", session=True) == ["once"]
    assert not allowed("session", "project", "unclassified")
