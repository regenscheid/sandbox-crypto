# SPDX-License-Identifier: MIT
"""A delegation's outcome as a work record on the entry it checked (ledger spec, LG-6,
LG-F8): the delegated run's work, which the platform records as the outcome is kept,
citing the delegation, whose outcome stays as returned."""

from __future__ import annotations

from pathlib import Path

from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.delegation.work import work_record
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.ledger.models import WorkRecord
from sandbox_science.orchestrator.projects import ProjectStore

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import CHECK, REPORT, RESULT, children, configure, started, wait
from .test_delegation import delegating as delegating


def recorded(harness: Harness) -> list[WorkRecord]:
    documents = harness.documents()
    try:
        return Ledger(documents).work(PROJECT, "C-1").records
    finally:
        documents.close()


async def test_a_checks_outcome_is_work_on_the_entry_it_checked(delegating: Harness) -> None:
    root = await delegating.run(
        "root", [Reply(calls=[CHECK]), wait(), Reply("Done.")], delegated=[Reply(REPORT)]
    )
    (child,) = children(delegating, root)
    (delegation_id,) = started(root)
    (work,) = recorded(delegating)
    assert (work.handle, work.entry_revision, work.ending) == ("C-1", 1, "assessment")
    assert work.tried.startswith("An independent check of C-1 at revision 1")
    (reference,) = work.references
    assert reference["delegation"] == delegation_id
    assert reference["note"] == f"confirmed, complete: {RESULT['summary']}"
    assert (work.author["role"], work.author["run_id"]) == ("delegated", child.run_id)
    assert work.provenance == {"delegation": delegation_id}
    assert work.operation_id == f"work-{delegation_id}"


async def test_a_check_without_a_result_ends_its_work_at_a_limit(delegating: Harness) -> None:
    """The child used its one turn on a call: it stopped at its turn limit, with no
    result, and its work ended ``limit``: its time ran out."""
    configure(delegating, check_turns=1)
    await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Done.")],
        delegated=[Reply(calls=[call("ledger_read", entry="C-1", revision=1)])],
    )
    (work,) = recorded(delegating)
    assert (work.ending, work.limit) == ("limit", "time")
    assert work.references[0]["note"] == "limit_reached, turn_limit: no result"


def made(brief: dict[str, object]) -> Delegation:
    return Delegation.model_validate(
        {
            "delegation_id": "dlg-" + "0" * 24,
            "parent": "20261015T120000000000Z-root",
            "run": "20261015T120001000000Z-run-child",
            "kind": "check",
            "brief": brief,
            "started_at": "2026-10-15T12:00:01+00:00",
        }
    )


def test_recording_the_work_again_replays_it(tmp_path: Path) -> None:
    projects = ProjectStore(tmp_path)
    documents = DocumentStore(store_path(tmp_path))
    try:
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        projects.open_session(PROJECT, SESSION, dedicated=False)
        ledger = Ledger(documents)
        claim = {"type": "claim", "origin": "project", "title": "C", "statement": "S."}
        made_by = ledger.change(
            PROJECT,
            {"operation_id": "c", "reason": "r", "changes": [claim]},
            USER,
        )
        entry = made_by.entries[0].entry_id
        records = DelegationRecords(projects.control)
        delegation = made({"check": {"entry": entry, "handle": "C-1", "revision": 1}})
        records.start(PROJECT, SESSION, delegation)
        kept = records.end(
            PROJECT, SESSION, delegation.delegation_id, Ending("cancelled", "cancelled")
        )
        found = work_record(kept, SESSION)
        assert found is not None
        mutation, author = found
        first = ledger.change(PROJECT, mutation, author)
        again = ledger.change(PROJECT, mutation, author)
        assert (first.replayed, again.replayed) == (False, True)
        (work,) = ledger.work(PROJECT, "C-1").records
        assert (work.ending, work.limit) == ("limit", "tools")
        assert work_record(made({"task": "Check."}), SESSION) is None  # still running
    finally:
        documents.close()
        projects.close()
