# SPDX-License-Identifier: MIT
"""The read-only ledger routes, through HTTP (LG-34): a page of entries, an entry at a
revision with its links, and an entry's history; typed answers, no-store, the project's
own entries only, and the read routes' guards."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.history import Archive, RunArchive
from sandbox_science.orchestrator.hooks import Appended
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.tools import ToolOutcome
from sandbox_science.workspace.ledger import (
    LedgerCitations,
    LedgerEntry,
    LedgerHistory,
    LedgerListing,
    LedgerWork,
)

from .ledger_support import AnchoredLedger as Ledger
from .route_support import ClientFactory, assert_security_headers, error_of, origin, workspace

MAIN = Author(kind="agent", session_id="a", run_id="run-1", role="main", model="scripted")
BASE = "/api/v0/projects/p1/ledger"


@pytest.fixture
def seeded(state_dir: Path) -> Path:
    documents = DocumentStore(store_path(state_dir))
    ledger = Ledger(documents)
    try:
        ledger.change(
            "p1",
            {
                "operation_id": "seed",
                "reason": "Recorded.",
                "changes": [
                    {
                        "type": "issue",
                        "key": "issue",
                        "title": "Every prime",
                        "statement": "Is it $p$?",
                    },
                    {
                        "type": "claim",
                        "origin": "project",
                        "title": "For 17",
                        "statement": "$a^{16}=1$.",
                        "links": [{"kind": "answers", "entry": "new:issue"}],
                    },
                    {"type": "task", "title": "Check", "statement": "Run it."},
                ],
            },
            MAIN,
        )
        ledger.change(
            "p1",
            {
                "operation_id": "start",
                "reason": "Started.",
                "changes": [{"entry": "T-1", "expected": 1, "status": "in_progress"}],
            },
            MAIN,
        )
    finally:
        documents.close()
    return state_dir


async def test_the_entries_page_is_typed_filtered_and_paged(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, seeded, static_dir)
    response = await client.get(BASE, params={"limit": "2"})
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert_security_headers(response.headers)
    first = LedgerListing.model_validate(await response.json())
    assert first.project_id == "p1"
    assert [e.handle for e in first.page.entries] == ["I-1", "C-1"]
    rest = LedgerListing.model_validate(
        await (await client.get(BASE, params={"after": first.page.next_after or ""})).json()
    )
    assert [(e.handle, e.status) for e in rest.page.entries] == [("T-1", "in_progress")]
    tasks = LedgerListing.model_validate(
        await (await client.get(BASE, params={"type": "task", "status": "in_progress"})).json()
    )
    assert [e.handle for e in tasks.page.entries] == ["T-1"]


async def test_an_entry_reads_with_its_links_at_any_revision(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, seeded, static_dir)
    issue = LedgerEntry.model_validate(await (await client.get(BASE + "/I-1")).json())
    assert issue.entry.statement == "Is it $p$?"
    assert [(link.kind, link.handle) for link in issue.entry.incoming] == [("answers", "C-1")]
    by_id = await client.get(f"{BASE}/{issue.entry.entry_id}")
    assert LedgerEntry.model_validate(await by_id.json()).entry.handle == "I-1"
    first = LedgerEntry.model_validate(
        await (await client.get(BASE + "/T-1", params={"revision": "1"})).json()
    )
    assert (first.entry.revision, first.entry.status, first.entry.head) == (1, "todo", 2)
    history = LedgerHistory.model_validate(await (await client.get(BASE + "/T-1/history")).json())
    assert [r.status for r in history.history.revisions] == ["in_progress", "todo"]
    assert issue.entry.incoming_total == 1
    later = LedgerEntry.model_validate(
        await (await client.get(BASE + "/I-1", params={"incoming_after": "1"})).json()
    )
    assert (later.entry.incoming, later.entry.incoming_total) == ([], 1)


@pytest.mark.parametrize(
    ("path", "params", "status", "kind"),
    [
        (BASE + "/I-9", {}, 404, "not_found"),
        (BASE + "/T-1", {"revision": "9"}, 404, "not_found"),
        ("/api/v0/projects/p2/ledger/I-1", {}, 404, "not_found"),
        ("/api/v0/projects/nobody/ledger", {}, 404, "not_found"),
        (BASE + "/X-1", {}, 400, "invalid_request"),
        (BASE, {"limit": "0"}, 400, "invalid_request"),
        (BASE, {"limit": "500"}, 400, "invalid_request"),
        (BASE, {"type": "lesson"}, 400, "invalid_request"),
        (BASE, {"sort": "title"}, 400, "invalid_request"),
        (BASE + "/I-1", {"revision": "one"}, 400, "invalid_request"),
        (BASE + "/I-1", {"incoming_after": "-1"}, 400, "invalid_request"),
        (BASE + "/I-1", {"incoming_after": "01"}, 400, "invalid_request"),
    ],
)
async def test_reads_outside_the_ledger_are_refused(
    aiohttp_client: ClientFactory,
    seeded: Path,
    static_dir: Path,
    path: str,
    params: dict[str, str],
    status: int,
    kind: str,
) -> None:
    client = await workspace(aiohttp_client, seeded, static_dir)
    await error_of(await client.get(path, params=params), status, kind)


async def test_the_routes_only_read(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, seeded, static_dir)
    response = await client.post(BASE, json={}, headers=origin(client))
    assert response.status in (404, 405)
    logged_out = await workspace(aiohttp_client, seeded, static_dir, login=False)
    assert (await logged_out.get(BASE)).status == 401


async def test_a_project_without_a_ledger_reads_empty(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    listing: Any = await (await client.get("/api/v0/projects/p2/ledger")).json()
    assert listing["page"] == {"entries": [], "next_after": None}


async def test_the_user_records_a_check_through_the_workspace(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """LG-17: the user's record that they checked an entry at the revision they read:
    same-origin, the user's, a conflict once the entry has moved on."""
    client = await workspace(aiohttp_client, seeded, static_dir)
    url = BASE + "/C-1/checks"
    body = {"revision": 1, "note": "Steps 1-4.", "operation_id": "check-1"}
    response = await client.post(url, json=body, headers=origin(client))
    assert response.status == 200, await response.text()
    assert (await response.json())["entries"][0]["revision"] == 2
    entry = LedgerEntry.model_validate(await (await client.get(BASE + "/C-1")).json()).entry
    (check,) = [e for e in entry.evidence if e.role == "independent_check"]
    assert (check.checked, check.scope, entry.author["kind"]) == (1, "Steps 1-4.", "user")
    await error_of(
        await client.post(url, json={**body, "operation_id": "check-2"}, headers=origin(client)),
        409,
        "conflict",
    )
    await error_of(
        await client.post(url, json={**body, "extra": 1}, headers=origin(client)),
        400,
        "invalid_request",
    )
    assert (await client.post(url, json=body)).status == 403


async def test_the_user_checks_a_reference_through_the_workspace(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """LG-16 as the coordinator decided: the user's check of a reference, which vouches
    for the PDF the revision names, goes through the same route."""
    documents = DocumentStore(store_path(seeded))
    try:
        reference = {"type": "reference", "title": "A paper", "statement": "A paper.",
                     "fields": {"identifiers": {"eprint": "2024/123"},
                                "published": "2024-02-01"}}  # fmt: skip
        Ledger(documents).change(
            "p1", {"operation_id": "ref", "reason": "r", "changes": [reference]}, MAIN
        )
    finally:
        documents.close()
    client = await workspace(aiohttp_client, seeded, static_dir)
    body = {"revision": 1, "note": "Read the PDF.", "operation_id": "check-ref"}
    response = await client.post(BASE + "/REF-1/checks", json=body, headers=origin(client))
    assert response.status == 200, await response.text()
    entry = LedgerEntry.model_validate(await (await client.get(BASE + "/REF-1")).json()).entry
    (check,) = entry.evidence
    assert (check.role, check.checked, entry.author["kind"]) == ("independent_check", 1, "user")


async def test_a_reference_reads_how_far_its_sources_were_read(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """LG-15 as CM-12 amends it: a reference's entry read gives the read register's
    extent for each of its sources a session read; another entry gives none."""
    documents = DocumentStore(store_path(seeded))
    try:
        fields = {"identifiers": {"eprint": "2024/1234"}, "published": "2024"}
        reference = {"type": "reference", "title": "A paper", "statement": "A paper.",
                     "fields": fields}  # fmt: skip
        Ledger(documents).change(
            "p1", {"operation_id": "ref", "reason": "r", "changes": [reference]}, MAIN
        )
    finally:
        documents.close()
    projects = ProjectStore(seeded)
    context = ExecutionContext(project_id="p1", session_id="a", environment_id="env-a")
    log = RunArchive(Archive(projects.control), context, run="run-1", parent=None)
    try:
        call = ToolCall("call-1", "e-print-mcp_get_eprint", json.dumps({"paper_id": "2024/1234"}))
        said = "[2024/1234] A paper\nAuthors: A. Author\nAbstract: It shows a thing."
        message = {"role": "tool", "tool_call_id": call.id, "content": said}
        outcome = ToolOutcome(said, {"connector": "crypto-research-mcp"})
        await log.append(message, Appended("tool_result", call=call, outcome=outcome))
    finally:
        projects.close()
    client = await workspace(aiohttp_client, seeded, static_dir)
    read = LedgerEntry.model_validate(await (await client.get(BASE + "/REF-1")).json()).read
    assert [(r.kind, r.source, r.extent) for r in read] == [
        ("eprint", "2024/1234", "abstract_only")
    ]
    issue = LedgerEntry.model_validate(await (await client.get(BASE + "/I-1")).json())
    assert issue.read == []


async def test_an_entry_reads_its_quotes_citation_checks(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """CR-4's read for the entry view: each quote of the revision with its checks; an
    entry without quotes has none, and the read takes only a revision."""
    client = await workspace(aiohttp_client, seeded, static_dir)
    response = await client.get(BASE + "/C-1/citations?revision=1")
    assert response.status == 200, await response.text()
    found = LedgerCitations.model_validate(await response.json()).citations
    assert (found.handle, found.revision, found.quotes) == ("C-1", 1, [])
    await error_of(await client.get(BASE + "/C-1/citations?after=1"), 400, "invalid_request")
    await error_of(await client.get(BASE + "/C-1/citations?revision=9"), 404, "not_found")


async def test_an_entry_reads_its_work_records_a_page_at_a_time(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """LG-6: the work records on an issue, newest first, a page at a time."""
    documents = DocumentStore(store_path(seeded))
    try:
        for number, limit in enumerate(("time", "ideas"), 1):
            Ledger(documents).change(
                "p1",
                {"operation_id": f"work-{number}", "reason": "Tried.",
                 "work": [{"entry": "I-1", "tried": f"Attempt {number}.", "ending": "limit",
                           "limit": limit}]},
                MAIN,
            )  # fmt: skip
    finally:
        documents.close()
    client = await workspace(aiohttp_client, seeded, static_dir)
    response = await client.get(BASE + "/I-1/work?limit=1")
    assert response.status == 200, await response.text()
    first = LedgerWork.model_validate(await response.json()).work
    assert [(r.tried, r.limit, r.author["role"]) for r in first.records] == [
        ("Attempt 2.", "ideas", "main")
    ]
    assert first.next_before == first.records[0].position
    rest = LedgerWork.model_validate(
        await (await client.get(BASE + f"/I-1/work?before={first.next_before}")).json()
    ).work
    assert ([r.tried for r in rest.records], rest.next_before) == (["Attempt 1."], None)
    for query in ("before=0", "limit=101", "after=1"):
        await error_of(await client.get(BASE + f"/I-1/work?{query}"), 400, "invalid_request")
    await error_of(await client.get(BASE + "/I-9/work"), 404, "not_found")


async def test_the_user_assesses_a_claimed_obstruction_through_the_workspace(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """LG-7: the user's acceptance of an obstruction claimed on an issue, at the revision
    read: same-origin, the user's, a conflict once the issue has moved on."""
    documents = DocumentStore(store_path(seeded))
    try:
        claim = Ledger(documents).change(
            "p1",
            {"operation_id": "claim", "reason": "Claimed.",
             "work": [{"key": "t", "entry": "I-1", "tried": "Swept.", "ending": "limit",
                       "limit": "time"},
                      {"entry": "I-1", "tried": "No factor base.",
                       "ending": "obstruction_claimed", "references": [{"work": "new:t"}]}]},
            MAIN,
        ).work[1]  # fmt: skip
    finally:
        documents.close()
    client = await workspace(aiohttp_client, seeded, static_dir)
    url = BASE + "/I-1/assessments"
    body = {"claim": claim, "verdict": "accepted", "revision": 1, "note": "Read it.",
            "operation_id": "assess-1"}  # fmt: skip
    response = await client.post(url, json=body, headers=origin(client))
    assert response.status == 200, await response.text()
    (made,) = (await response.json())["work"]
    work = LedgerWork.model_validate(await (await client.get(BASE + "/I-1/work")).json()).work
    assert (work.records[0].record_id, work.records[0].author["kind"]) == (made, "user")
    stale = {**body, "revision": 9, "operation_id": "assess-2"}
    await error_of(await client.post(url, json=stale, headers=origin(client)), 409, "conflict")
    await error_of(
        await client.post(url, json={**body, "verdict": "maybe"}, headers=origin(client)),
        400,
        "invalid_request",
    )
    assert (await client.post(url, json=body)).status == 403


async def test_the_user_drops_a_dominates_link_through_the_workspace(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """LG-22 (the review's M3): the user drops a dominates link an entry holds, with the
    reason: same-origin, the user's, at the revision read."""
    from sandbox_science.orchestrator.documents import USER

    documents = DocumentStore(store_path(seeded))
    try:
        ledger = Ledger(documents)
        category = {"category": "key-recovery", "target": "T", "kind": "K", "conditions": [],
                    "measures": [{"name": "time", "unit": "bits", "direction": "lower"}],
                    "reason": "r", "operation_id": "category"}  # fmt: skip
        ledger.define_category("p1", category, USER)
        reference = {"type": "reference", "title": "A paper", "statement": "A paper.",
                     "fields": {"identifiers": {"eprint": "2024/123"},
                                "published": "2024-02-01"}}  # fmt: skip
        cite = [{"role": "supports", "type": "source_claim", "entry": "REF-1"}]

        def published(time: object, **more: object) -> dict[str, object]:
            return {"type": "result", "origin": "published", "title": "P", "statement": "s",
                    "fields": {"category": "key-recovery", "measures": {"time": time}},
                    "evidence": cite, **more}  # fmt: skip

        p1 = published(120, links=[{"kind": "dominates", "entry": "R-1", "note": "Faster."}])
        for op, change in (("ref", reference), ("p4", published("$2^n$")), ("p1", p1)):
            ledger.change("p1", {"operation_id": op, "reason": "r", "changes": [change]}, USER)
    finally:
        documents.close()
    client = await workspace(aiohttp_client, seeded, static_dir)
    url = BASE + "/R-2/dominates/dropped"
    body = {"target": "R-1", "revision": 1, "reason": "Not for small n.", "operation_id": "d"}
    assert (await client.post(url, json=body)).status == 403
    await error_of(
        await client.post(url, json={**body, "revision": 0}, headers=origin(client)),
        400,
        "invalid_request",
    )
    response = await client.post(url, json=body, headers=origin(client))
    assert response.status == 200, await response.text()
    assert (await response.json())["entries"][0]["revision"] == 2
    await error_of(
        await client.post(url, json={**body, "operation_id": "again"}, headers=origin(client)),
        409,
        "conflict",
    )


async def test_the_user_clears_flags_through_the_workspace(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """LG-29: the user clears an entry's open flags with the reason: same-origin, the
    user's, and only the flags on that entry."""
    documents = DocumentStore(store_path(seeded))
    try:
        ledger = Ledger(documents)
        cited = [{"role": "supports", "type": "proof", "entry": "C-1"}]
        result = {"type": "result", "origin": "project", "title": "R", "statement": "s"}
        for op, change in (
            ("cites", {**result, "evidence": cited}),
            ("revised", {"entry": "C-1", "expected": 1, "statement": "Other."}),
        ):
            ledger.change("p1", {"operation_id": op, "reason": "r", "changes": [change]}, MAIN)
        (flag,) = ledger.entry("p1", "R-1").flags
    finally:
        documents.close()
    client = await workspace(aiohttp_client, seeded, static_dir)
    body = {"flags": [flag.flag_id], "reason": "It still holds.", "operation_id": "clear-1"}
    assert (await client.post(BASE + "/R-1/flags", json=body)).status == 403
    await error_of(
        await client.post(BASE + "/C-1/flags", json=body, headers=origin(client)),
        404,
        "not_found",
    )
    await error_of(
        await client.post(BASE + "/R-1/flags", json={**body, "flags": []}, headers=origin(client)),
        400,
        "invalid_request",
    )
    response = await client.post(BASE + "/R-1/flags", json=body, headers=origin(client))
    assert response.status == 200, await response.text()
    assert (await response.json())["flags_cleared"] == [flag.flag_id]
    entry = LedgerEntry.model_validate(await (await client.get(BASE + "/R-1")).json()).entry
    assert (entry.flags, entry.open_flags) == ([], 0)
    # The user reopens it, as a new flag; and clears every open flag on the entry at once.
    reopen = {"flags": [flag.flag_id], "reason": "It does not.", "operation_id": "reopen-1"}
    response = await client.post(BASE + "/R-1/flags/reopened", json=reopen, headers=origin(client))
    assert response.status == 200, await response.text()
    (again,) = (await response.json())["flags_reopened"]
    history = LedgerHistory.model_validate(
        await (await client.get(BASE + "/R-1/history")).json()
    ).history
    assert [(f.flag_id, f.reason) for f in history.flags_cleared] == [
        (flag.flag_id, "It still holds.")
    ]
    await error_of(
        await client.post(
            BASE + "/R-1/flags",
            json={**body, "cause": "spread", "operation_id": "both"},
            headers=origin(client),
        ),
        400,
        "invalid_request",
    )
    everything = {"reason": "Holds.", "operation_id": "clear-all"}
    response = await client.post(BASE + "/R-1/flags", json=everything, headers=origin(client))
    assert (await response.json())["flags_cleared"] == [again]


async def test_the_gate_through_the_workspace(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """LG-17: the entry panel reads each clause of the gate for an entry."""
    from sandbox_science.workspace.ledger import LedgerGate

    client = await workspace(aiohttp_client, seeded, static_dir)
    answer = LedgerGate.model_validate(await (await client.get(BASE + "/C-1/gate")).json())
    assert (answer.gate.handle, answer.gate.applies, answer.gate.holds) == ("C-1", True, False)
    assert answer.gate.clauses[0].clause == "evidence"
    assert answer.gate.clauses[0].holds is False
    await error_of(await client.get(BASE + "/C-1/gate", params={"x": "1"}), 400, "invalid_request")
    await error_of(await client.get(BASE + "/C-9/gate"), 404, "not_found")


async def test_the_frontier_categories_through_the_workspace(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """LG-18: the user defines a category (same-origin), and the GUI reads them."""
    from sandbox_science.workspace.frontier import FrontierCategories, FrontierCategory

    client = await workspace(aiohttp_client, seeded, static_dir)
    url = "/api/v0/projects/p1/frontier"
    body = {
        "category": "key-recovery",
        "target": "The test scheme",
        "kind": "Key recovery cost",
        "measures": [{"name": "time", "unit": "log2 operations", "direction": "lower"}],
        "conditions": ["model"],
        "reason": "What the project compares.",
        "operation_id": "category-1",
    }
    assert (await client.post(url, json=body)).status == 403
    response = await client.post(url, json=body, headers=origin(client))
    assert response.status == 200, await response.text()
    assert (await response.json())["author"]["kind"] == "user"
    await error_of(
        await client.post(url, json={**body, "operation_id": "again"}, headers=origin(client)),
        409,
        "conflict",
    )
    listed = FrontierCategories.model_validate(await (await client.get(url)).json())
    assert [c.category for c in listed.page.categories] == ["key-recovery"]
    one = FrontierCategory.model_validate(
        await (await client.get(url + "/key-recovery/definition")).json()
    )
    assert one.category.measures[0].direction == "lower"
    await error_of(await client.get(url + "/bounds/definition"), 404, "not_found")
    await error_of(await client.get(url, params={"limit": "0"}), 400, "invalid_request")


async def test_a_frontier_through_the_workspace(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LG-21, LG-23: the GUI reads a category's frontier, a page of groups at a time, each
    with the first results of each kind and how many more (the review's L3)."""
    from sandbox_science.workspace import frontier
    from sandbox_science.workspace.frontier import FrontierAnswer

    documents = DocumentStore(store_path(seeded))
    try:
        ledger = Ledger(documents)
        ledger.define_category(
            "p1",
            {
                "category": "key-recovery",
                "target": "The test scheme",
                "kind": "Key recovery cost",
                "measures": [{"name": "time", "unit": "log2 operations", "direction": "lower"}],
                "conditions": ["model"],
                "reason": "What the project compares.",
                "operation_id": "category-1",
            },
            MAIN,
        )
        candidate = {
            "type": "result",
            "origin": "project",
            "title": "R1",
            "statement": "Time $2^{105}$.",
            "fields": {"category": "key-recovery", "conditions": {"model": "classical"},
                       "measures": {"time": 105}},
        }  # fmt: skip
        ledger.change("p1", {"operation_id": "r1", "reason": "r", "changes": [candidate]}, MAIN)
        quantum = {**candidate["fields"], "conditions": {"model": "quantum"}}
        more = [candidate, {**candidate, "fields": quantum}]
        ledger.change("p1", {"operation_id": "r2", "reason": "r", "changes": more}, MAIN)
    finally:
        documents.close()
    client = await workspace(aiohttp_client, seeded, static_dir)
    url = "/api/v0/projects/p1/frontier/key-recovery"
    answer = FrontierAnswer.model_validate(await (await client.get(url)).json())
    first, _ = answer.frontier.groups
    assert (first.conditions, [r.handle for r in first.candidates]) == (
        {"model": "classical"},
        ["R-2", "R-1"],
    )
    assert first.candidates[-1].statement == "Time $2^{105}$."
    assert answer.next_groups_after is None
    monkeypatch.setattr(frontier, "SHOWN_RESULTS", 1)
    page = FrontierAnswer.model_validate(
        await (await client.get(url, params={"limit": "1"})).json()
    )
    (group,) = page.frontier.groups
    # The newest of a group's candidates first, consistent with candidates_before.
    assert ([r.handle for r in group.candidates], group.more, page.next_groups_after) == (
        ["R-2"],
        {"candidates": 1},
        1,
    )
    page = FrontierAnswer.model_validate(
        await (await client.get(url, params={"groups_after": "1", "limit": "1"})).json()
    )
    assert ([g.conditions for g in page.frontier.groups], page.next_groups_after) == (
        [{"model": "quantum"}],
        None,
    )
    # Groups page by their folded conditions key too, which a change between reads keeps.
    page = FrontierAnswer.model_validate(
        await (await client.get(url, params={"limit": "1"})).json()
    )
    (first,) = page.frontier.groups
    assert page.next_groups_key == first.key == '{"model": "classical"}'
    documents = DocumentStore(store_path(seeded))
    try:
        early = {**candidate, "fields": {**candidate["fields"], "conditions": {"model": "aaa"}}}
        Ledger(documents).change(
            "p1", {"operation_id": "r3", "reason": "r", "changes": [early]}, MAIN
        )
    finally:
        documents.close()
    shifted = FrontierAnswer.model_validate(
        await (await client.get(url, params={"groups_after": "1", "limit": "1"})).json()
    )
    assert [g.conditions for g in shifted.frontier.groups] == [{"model": "classical"}]
    keyed = FrontierAnswer.model_validate(
        await (await client.get(url, params={"groups_after_key": first.key, "limit": "1"})).json()
    )
    assert ([g.conditions for g in keyed.frontier.groups], keyed.next_groups_key) == (
        [{"model": "quantum"}],
        None,
    )
    both = {"groups_after": "1", "groups_after_key": first.key}
    for bad in (
        {"x": "1"}, {"groups_after": "-1"}, {"limit": "0"}, {"candidates_before": "0"}, both
    ):  # fmt: skip
        await error_of(await client.get(url, params=bad), 400, "invalid_request")
    await error_of(await client.get("/api/v0/projects/p1/frontier/bounds"), 404, "not_found")


async def test_a_frontiers_history_through_the_workspace(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LG-24: the GUI reads a category's changes, and its best known at a past time, each
    change with the first results of each list and how many more (the review's L3)."""
    from datetime import UTC, datetime

    from sandbox_science.orchestrator.documents import USER
    from sandbox_science.workspace import frontier
    from sandbox_science.workspace.frontier import FrontierHistoryAnswer, FrontierPastAnswer

    client = await workspace(aiohttp_client, seeded, static_dir)
    url = "/api/v0/projects/p1/frontier"
    body = {
        "category": "key-recovery",
        "target": "The test scheme",
        "kind": "Key recovery cost",
        "measures": [{"name": "time", "unit": "log2 operations", "direction": "lower"}],
        "conditions": [],
        "reason": "What the project compares.",
        "operation_id": "category-1",
    }
    assert (await client.post(url, json=body, headers=origin(client))).status == 200
    history = FrontierHistoryAnswer.model_validate(
        await (await client.get(url + "/key-recovery/history")).json()
    )
    assert history.history.changes == []
    now = datetime.now(UTC).isoformat()
    past = FrontierPastAnswer.model_validate(
        await (await client.get(url + "/key-recovery/past", params={"at": now})).json()
    )
    assert past.past.groups == []
    await error_of(
        await client.get(url + "/key-recovery/past", params={"at": "yesterday"}),
        400,
        "invalid_request",
    )
    await error_of(
        await client.get(url + "/key-recovery/past", params={"at": "2026-10-15T00:00:00"}),
        400,
        "invalid_request",
    )
    documents = DocumentStore(store_path(seeded))
    try:
        reference = {"type": "reference", "title": "A paper", "statement": "A paper.",
                     "fields": {"identifiers": {"eprint": "2024/123"},
                                "published": "2024-02-01"}}  # fmt: skip
        cite = [{"role": "supports", "type": "source_claim", "entry": "new:paper"}]
        both = [
            {"type": "result", "origin": "published", "title": "P", "statement": "s",
             "fields": {"category": "key-recovery", "measures": {"time": 1}}, "evidence": cite}
            for _ in range(2)
        ]  # fmt: skip
        changes = [{**reference, "key": "paper"}, *both]
        Ledger(documents).change(
            "p1", {"operation_id": "p", "reason": "r", "changes": changes}, USER
        )
    finally:
        documents.close()
    monkeypatch.setattr(frontier, "SHOWN_RESULTS", 1)
    history = FrontierHistoryAnswer.model_validate(
        await (await client.get(url + "/key-recovery/history")).json()
    )
    (change,) = history.history.changes
    assert ([m.handle for m in change.best], change.added, change.more) == (
        ["R-1"],
        ["R-1"],
        {"best": 1, "added": 1},
    )
    later = datetime.now(UTC).isoformat()
    past = FrontierPastAnswer.model_validate(
        await (await client.get(url + "/key-recovery/past", params={"at": later})).json()
    )
    assert [(len(g.best), g.more) for g in past.past.groups] == [(1, 1)]


async def test_the_user_accepts_and_dismisses_fold_proposals(
    aiohttp_client: ClientFactory, seeded: Path, static_dir: Path
) -> None:
    """CM-15: the session's waiting proposals list; the user accepts one with the user's
    authority, again without effect, and dismisses another with a reason; same-origin."""
    from sandbox_science.orchestrator.history import Entry
    from sandbox_science.orchestrator.ledger.proposals import FoldProposals

    documents = DocumentStore(store_path(seeded))
    try:
        said = Archive(documents.control).append(
            "p1", "a", "run-1", Entry("user", "user", "Try the small primes first.")
        )
        question = {"op": "issue", "kind": "question", "title": "Small primes?",
                    "statement": "Do the small primes suffice?",
                    "anchors": [{"message": said.message_id}]}  # fmt: skip
        found = FoldProposals(Ledger(documents)).propose(
            "p1", "a", "run-1", said.message_id, [said], [question, question]
        )
        assert found["kept"] == 2
    finally:
        documents.close()
    client = await workspace(aiohttp_client, seeded, static_dir)
    url = "/api/v0/projects/p1/sessions/a/proposals"
    listed = (await (await client.get(url)).json())["proposals"]
    first, second = (p["proposal_id"] for p in listed)
    accepted = url + f"/{first}/accepted"
    body = {"operation_id": "accept-1"}
    response = await client.post(accepted, json=body, headers=origin(client))
    assert response.status == 200, await response.text()
    assert (await response.json())["handles"] == ["I-2"]
    again = await client.post(accepted, json={"operation_id": "accept-2"}, headers=origin(client))
    assert (await again.json())["replayed"] is True
    entry = LedgerEntry.model_validate(await (await client.get(BASE + "/I-2")).json()).entry
    assert (entry.author["kind"], entry.basis) == ("user", "interpreted")
    elsewhere = f"/api/v0/projects/p1/sessions/c/proposals/{second}/dismissed"
    await error_of(
        await client.post(elsewhere, json={"reason": "Same."}, headers=origin(client)),
        404,
        "not_found",
    )
    dismissed = url + f"/{second}/dismissed"
    assert (await client.post(dismissed, json={"reason": "Same."})).status == 403
    response = await client.post(dismissed, json={"reason": "Same."}, headers=origin(client))
    assert (await response.json())["state"] == "dismissed"
    assert (await (await client.get(url)).json())["proposals"] == []
    await error_of(
        await client.post(url + "/prop-x/accepted", json=body, headers=origin(client)),
        400,
        "invalid_request",
    )
