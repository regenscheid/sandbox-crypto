# SPDX-License-Identifier: MIT
"""Grants of a class on a server, a host or a package source, for a session or the
project (D97), and the migration of remembered tool approvals."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.documents.models import USER, Author
from sandbox_science.orchestrator.project_grants import Asked, ProjectGrants
from sandbox_science.orchestrator.projects import ProjectStore

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
POLICY = "sha256:" + "a" * 64
IMPORTED = Author(kind="import", user="user", import_id="imp-1")


def write(server: str = "papers", effect: str = "write", **changed: object) -> Asked:
    fields: dict[str, object] = {
        "kind": "mcp", "caller_kind": "agent", "session": "s1", "max_grant": "project",
        "server": server, "effect": effect, **changed,
    }  # fmt: skip
    return Asked(**fields)  # pyright: ignore[reportArgumentType]


@pytest.fixture
def grants(tmp_path: Path) -> ProjectGrants:
    store = ProjectStore(tmp_path / "state")
    store.create("p1", "minimal", POLICY)
    store.close()
    return ProjectGrants(DocumentStore(tmp_path / "state" / "control.sqlite3"))


def test_a_class_grant_covers_its_class_on_its_server_for_the_agent_only(
    grants: ProjectGrants,
) -> None:
    [given] = grants.give("p1", write(), "project", USER, NOW)
    assert (given.kind, given.server, given.effect) == ("mcp", "papers", "write")
    assert grants.covering("p1", write(), NOW) == [given]
    assert grants.covering("p1", write(effect="communicate", max_grant="session"), NOW) == []
    assert grants.covering("p1", write(server="web"), NOW) == []
    assert grants.covering("p1", write(caller_kind="code"), NOW) == []
    assert grants.covering("p1", write(max_grant="session"), NOW) == []  # ceiling lowered
    assert grants.covering("p1", write(effect="unclassified"), NOW) == []
    # Given again, the same grant is kept, not repeated.
    assert grants.give("p1", write(), "project", USER, NOW) == [given]


def test_the_ceiling_and_the_caller_bound_what_may_be_granted(grants: ProjectGrants) -> None:
    refused = (
        (write(effect="communicate", max_grant="session"), "project"),
        (write(effect="unclassified"), "session"),
        (write(caller_kind="code"), "project"),
        (write(session=None), "session"),
        (Asked("network", "code", None, "project", host="h.org", method="POST"), "project"),
        (Asked("network", "agent", None, "project", host="h.org", method="GET"), "project"),
        (Asked("packages", "agent", None, "project"), "project"),  # no source named
    )
    for asked, scope in refused:
        with pytest.raises(DocumentError) as caught:
            grants.give("p1", asked, scope, USER, NOW)
        assert caught.value.kind == "policy_denied", (asked, scope)
    with pytest.raises(DocumentError):
        grants.give("p1", write(), "project", IMPORTED, NOW)  # only the user grants
    assert grants.listing("p1").grants == []


def test_session_grants_expiry_and_revocation(grants: ProjectGrants) -> None:
    [session] = grants.give("p1", write(), "session", USER, NOW, hours=1)
    assert grants.covering("p1", write(), NOW) == [session]
    assert grants.covering("p1", write(session="s2"), NOW) == []
    assert grants.covering("p1", write(), NOW + timedelta(hours=2)) == []  # expired
    [project] = grants.give("p1", write(), "project", USER, NOW)
    assert grants.covering("p1", write(), NOW) == [project]  # the project's first
    grants.used("p1", [project.grant_id], NOW)
    [used] = [g for g in grants.listing("p1").grants if g.grant_id == project.grant_id]
    assert (used.uses, used.last_used_at) == (1, NOW)
    grants.revoke("p1", project.grant_id, USER, NOW)
    assert grants.covering("p1", write(), NOW) == [session]
    with pytest.raises(DocumentError):
        grants.revoke("p1", "grant-unknown", USER, NOW)


def test_a_package_request_needs_every_source_granted(grants: ProjectGrants) -> None:
    forge = Asked("packages", "agent", "s1", "project", sources=(("conda", "conda-forge"),))
    grants.give("p1", forge, "project", USER, NOW)
    both = Asked(
        "packages", "agent", "s1", "project",
        sources=(("conda", "conda-forge"), ("conda", "bioconda")),
    )  # fmt: skip
    assert grants.covering("p1", both, NOW) == []
    assert len(grants.covering("p1", forge, NOW)) == 1


def test_host_grants_cover_research_codes_reads_and_never_an_agents_call(
    grants: ProjectGrants,
) -> None:
    read = Asked("network", "code", None, "project", host="data.org", method="GET")
    [host] = grants.give("p1", read, "project", USER, NOW)
    assert (host.host, host.methods) == ("data.org", "read")
    assert grants.covering("p1", read, NOW) == [host]
    head = Asked("network", "code", None, "project", host="data.org", method="HEAD")
    assert grants.covering("p1", head, NOW) == [host]
    post = Asked("network", "code", None, "once", host="data.org", method="POST")
    assert grants.covering("p1", post, NOW) == []
    agent = Asked("mcp", "agent", "s1", "project", server="data.org", effect="write")
    assert grants.covering("p1", agent, NOW) == []


def test_remembered_tool_approvals_become_class_grants_and_server_wide_ones_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state" / "control.sqlite3"
    with monkeypatch.context() as old:
        old.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:-1])
        old.setattr(migrations, "VERSION", migrations.VERSION - 1)
        store = ProjectStore(tmp_path / "state")
        store.create("p1", "minimal", POLICY)
        store.close()
        remembered = [
            {"server": "crypto-research-mcp", "tool": "firecrawl-mcp_firecrawl_feedback"},
            {"server": "crypto-research-mcp", "tool": "firecrawl-mcp_firecrawl_monitor_run"},
            {"server": "crypto-research-mcp", "tool": "firecrawl-mcp_firecrawl_monitor_create"},
            {"server": "crypto-research-mcp", "tool": "not-classified"},
            {"server": "crypto-research-mcp", "tool": None},
            {"server": "unknown-server", "tool": "anything"},
        ]
        with closing(DocumentStore(path)) as documents, documents.control.write() as tx:
            tx.sql.execute(
                "INSERT INTO project_approval_settings VALUES (?,?,?,?)",
                ("p1", 1, json.dumps(remembered), USER.model_dump_json()),
            )
    with closing(DocumentStore(path)) as documents:
        found = ProjectGrants(documents).listing("p1").grants
        with documents.control.read() as tx:
            tables = {
                row[0]
                for row in tx.sql.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
    # Two write tools become one write grant; a communicate tool (a session ceiling), an
    # unclassified tool, a server-wide approval and an unknown server's are dropped.
    assert [(g.kind, g.server, g.effect, g.scope) for g in found] == [
        ("mcp", "crypto-research-mcp", "write", "project")
    ]
    assert "project_approval_settings" not in tables
