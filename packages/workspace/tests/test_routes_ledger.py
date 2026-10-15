# SPDX-License-Identifier: MIT
"""The read-only ledger routes, through HTTP (LG-34): a page of entries, an entry at a
revision with its links, and an entry's history; typed answers, no-store, the project's
own entries only, and the read routes' guards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.workspace.ledger import LedgerEntry, LedgerHistory, LedgerListing

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
