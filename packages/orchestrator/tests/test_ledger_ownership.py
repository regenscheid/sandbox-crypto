# SPDX-License-Identifier: MIT
"""Public guarantees at the ledger's transaction boundary."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.control import EventLog
from sandbox_science.orchestrator.documents import USER, DocumentError, DocumentStore, store_path
from sandbox_science.orchestrator.ledger import Ledger, Mutation
from sandbox_science.orchestrator.ledger.models import MutationResult
from sandbox_science.orchestrator.projects import ProjectStore


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "local", dedicated=False)
    projects.create("q", "minimal", "sha256:" + "a" * 64)
    projects.open_session("q", "foreign", dedicated=False)
    documents = DocumentStore(store_path(tmp_path))
    yield Ledger(documents)
    documents.close()
    projects.close()


def test_callback_failure_rolls_back_every_ledger_effect_and_its_receipt(ledger: Ledger) -> None:
    ledger.define_category(
        "p",
        {
            "operation_id": "category",
            "reason": "Compare work.",
            "category": "cost",
            "target": "A target",
            "kind": "An attack",
            "measures": [{"name": "time", "unit": "operations", "direction": "lower"}],
            "conditions": [],
        },
        USER,
    )
    ledger.change(
        "p",
        {
            "operation_id": "seed",
            "reason": "Initial state.",
            "changes": [
                {
                    "type": "reference",
                    "key": "source",
                    "title": "Source",
                    "statement": "Source.",
                    "fields": {"identifiers": {"eprint": "2024/123"}, "published": "2024-02-01"},
                },
                {
                    "type": "assumption",
                    "key": "assumption",
                    "title": "Assume",
                    "statement": "First.",
                },
                {
                    "type": "claim",
                    "origin": "project",
                    "title": "Claim",
                    "statement": "Claim.",
                    "links": [{"kind": "assumes", "entry": "new:assumption"}],
                },
                {
                    "type": "result",
                    "origin": "published",
                    "title": "Result",
                    "statement": "Cost.",
                    "evidence": [
                        {"role": "supports", "type": "source_claim", "entry": "new:source"}
                    ],
                    "fields": {"category": "cost", "conditions": {}, "measures": {"time": 10}},
                },
            ],
        },
        USER,
    )
    before = ledger.mark("p"), ledger.frontier_history("p", "cost")
    mutation = Mutation.model_validate(
        {
            "operation_id": "atomic",
            "reason": "Accept research and fold together.",
            "changes": [
                {"entry": "A-1", "expected": 1, "statement": "Second."},
                {
                    "entry": "R-1",
                    "expected": 1,
                    "status": "withdrawn",
                    "fields": {"category": "cost", "conditions": {}, "measures": {"time": 5}},
                },
            ],
            "work": [{"entry": "C-1", "tried": "Checked.", "ending": "limit", "limit": "time"}],
        }
    )
    log = EventLog(ledger.control, "ledger_entry", "test")
    calls: list[bool] = []

    def accept(db: sqlite3.Connection, results: list[MutationResult]) -> None:
        calls.append(results[0].replayed)
        assert results[0].flagged == ["C-1"]
        assert len(results[0].work) == 1
        assert db.execute("SELECT head FROM ledger_entries WHERE type='result'").fetchone() == (2,)
        log.append(db, "test.fold_accepted", "p", None, "proposal", {}, ledger.clock())
        raise RuntimeError("proposal refused")

    with pytest.raises(RuntimeError, match="proposal refused"):
        ledger.apply("p", [(mutation, None)], USER, within=accept)
    assert calls == [False]
    assert (ledger.mark("p"), ledger.frontier_history("p", "cost")) == before
    assert ledger.entry("p", "A-1").statement == "First."
    assert ledger.entry("p", "C-1").flags == []
    assert ledger.work("p", "C-1").records == []
    with ledger.control.read() as tx:
        assert not [e for e in log.page(tx.sql, "p", 0, 100) if e.kind == "test.fold_accepted"]
    result = ledger.apply("p", [(mutation, None)], USER)[0]
    assert not result.replayed
    assert ledger.entry("p", "A-1").statement == "Second."
    assert len(ledger.entry("p", "C-1").flags) == 1
    assert len(ledger.work("p", "C-1").records) == 1
    assert ledger.frontier_history("p", "cost") != before[1]
    committed = ledger.mark("p"), ledger.frontier_history("p", "cost")
    replay = ledger.apply("p", [(mutation, None)], USER)[0]
    assert replay == result.model_copy(update={"replayed": True})
    assert (ledger.mark("p"), ledger.frontier_history("p", "cost")) == committed
    assert len(ledger.work("p", "C-1").records) == 1


def test_batch_preparation_sees_prior_writes_and_callback_runs_on_replay(ledger: Ledger) -> None:
    first = Mutation.model_validate(
        {
            "operation_id": "first",
            "reason": "Create.",
            "changes": [{"type": "task", "title": "Task", "statement": "Do it."}],
        }
    )
    second = Mutation.model_validate(
        {
            "operation_id": "second",
            "reason": "Revise.",
            "changes": [{"entry": "T-1", "expected": 1, "status": "done"}],
        }
    )
    callbacks: list[list[bool]] = []

    def accepted(db: sqlite3.Connection, results: list[MutationResult]) -> None:
        assert db.execute("SELECT head FROM ledger_entries").fetchone() == (2,)
        callbacks.append([r.replayed for r in results])

    batch: list[tuple[Mutation, dict[str, Any] | None]] = [(first, None), (second, None)]
    assert [r.entries[0].revision for r in ledger.apply("p", batch, USER, within=accepted)] == [
        1,
        2,
    ]
    ledger.apply("p", batch, USER, within=accepted)
    ledger.apply("p", [], USER, within=accepted)
    assert callbacks == [[False, False], [True, True], []]
    assert [(r.revision, r.status) for r in ledger.history("p", "T-1").revisions] == [
        (2, "done"),
        (1, "todo"),
    ]


@pytest.mark.parametrize("session", ["missing", "foreign"])
def test_decision_scope_rejects_missing_and_other_project_sessions(
    ledger: Ledger, session: str
) -> None:
    decision = {
        "type": "decision",
        "title": "Scope",
        "statement": "Use this session.",
        "fields": {"session": session},
    }
    raw = {"operation_id": "scope", "reason": "Set scope.", "changes": [decision]}
    with pytest.raises(DocumentError, match=f"no session {session} in this project") as refused:
        ledger.change("p", raw, USER)
    assert refused.value.kind == "not_found"
    assert ledger.entries("p").entries == []
    accepted = ledger.change(
        "p", {**raw, "changes": [{**decision, "fields": {"session": "local"}}]}, USER
    )
    assert not accepted.replayed
    assert ledger.entry("p", "D-1").fields == {"session": "local"}
