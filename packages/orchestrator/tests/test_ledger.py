# SPDX-License-Identifier: MIT
"""The project ledger's store (docs/specs/ledger.md): entries of the seven types with
their handles and revisions (LG-1, LG-2), mutations that commit all or nothing under an
operation ID (LG-30, LG-31), their events (LG-33), and who may change what (LG-13)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.control import EventLog
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    store_path,
)
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.projects import ProjectStore

MAIN = Author(kind="agent", session_id="s", run_id="r1", role="main", model="scripted")
DELEGATED = Author(kind="agent", session_id="s", run_id="r2", role="delegated")
REFERENCE_FIELDS = {"identifiers": {"eprint": "2024/123"}, "published": "2024-02-01"}

NEW: dict[str, dict[str, Any]] = {
    "issue": {"type": "issue", "title": "Prove it for every prime", "statement": "Is $a^{p-1}=1$?"},
    "claim": {
        "type": "claim",
        "origin": "project",
        "title": "Fermat for 17",
        "statement": "$a^{16} = 1$ for every nonzero $a$ in GF(17).",
        "fields": {"kind": "observation"},
    },
    "result": {
        "type": "result",
        "origin": "project",
        "title": "Key recovery",
        "statement": "Time $2^{105}$.",
        "fields": {"category": "key-recovery", "measures": {"time": 105, "memory": 80}},
    },
    "assumption": {"type": "assumption", "title": "p = 17", "statement": "$p = 17$."},
    "decision": {"type": "decision", "title": "Use Sage", "statement": "Sage for the field."},
    "task": {"type": "task", "title": "Run the check", "statement": "Run it over GF(17)."},
    "reference": {
        "type": "reference",
        "title": "A paper",
        "statement": "A. Author, *A paper*, ePrint 2024/123.",
        "fields": REFERENCE_FIELDS,
    },
}


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.create("q", "minimal", "sha256:" + "a" * 64)
    projects.open_session("q", "t", dedicated=False)
    projects.close()
    documents = DocumentStore(store_path(tmp_path))
    yield Ledger(documents)
    documents.close()


def change(
    ledger: Ledger, *changes: dict[str, Any], op: str = "op-1", author: Author = MAIN
) -> dict[str, Any]:
    return ledger.change(
        "p", {"operation_id": op, "reason": "Recorded.", "changes": list(changes)}, author
    ).model_dump()


def events(ledger: Ledger) -> list[tuple[str, dict[str, Any]]]:
    log = EventLog(ledger.control, "ledger_entry", "test")
    with ledger.control.read() as tx:
        return [(e.kind, e.payload) for e in log.page(tx.sql, "p", 0, 200)]


def test_each_type_gets_its_handle_its_first_status_and_its_event(ledger: Ledger) -> None:
    made = change(ledger, *NEW.values())
    assert [(e["handle"], e["status"], e["revision"]) for e in made["entries"]] == [
        ("I-1", "open", 1),
        ("C-1", "proposed", 1),
        ("R-1", "proposed", 1),
        ("A-1", "active", 1),
        ("D-1", "proposed", 1),
        ("T-1", "todo", 1),
        ("REF-1", "active", 1),
    ]
    reference = ledger.entry("p", "REF-1")
    assert (reference.type, reference.fields) == ("reference", REFERENCE_FIELDS)
    assert reference.statement == NEW["reference"]["statement"]
    assert reference.author["role"] == "main"
    assert [kind for kind, _ in events(ledger)] == ["ledger.entry_created"] * 7
    more = change(ledger, NEW["claim"], NEW["claim"], op="op-2")
    assert [e["handle"] for e in more["entries"]] == ["C-2", "C-3"]


def test_a_statement_is_saved_in_the_dollar_form(ledger: Ledger) -> None:
    change(ledger, {**NEW["assumption"], "statement": r"Let \(p = 17\)."})
    assert ledger.entry("p", "A-1").statement == "Let $p = 17$."


def test_a_revision_names_the_revision_it_expects_and_is_never_rebased(ledger: Ledger) -> None:
    change(ledger, NEW["task"])
    change(ledger, {"entry": "T-1", "expected": 1, "status": "in_progress"}, op="op-2")
    with pytest.raises(DocumentError, match="at revision 2, not 1") as stale:
        change(ledger, {"entry": "T-1", "expected": 1, "title": "Late"}, op="op-3")
    assert (stale.value.kind, stale.value.details["current"]) == ("conflict", 2)
    done = change(ledger, {"entry": "T-1", "expected": 2, "status": "done"}, op="op-4")
    assert done["entries"][0]["revision"] == 3
    history = ledger.history("p", "T-1")
    assert [(r.revision, r.status) for r in history.revisions] == [
        (3, "done"),
        (2, "in_progress"),
        (1, "todo"),
    ]
    assert ledger.entry("p", "T-1", revision=1).status == "todo"
    revised = [p for kind, p in events(ledger) if kind == "ledger.entry_revised"]
    assert [(p["status_before"], p["status"], p["changed"]) for p in revised] == [
        ("todo", "in_progress", ["status"]),
        ("in_progress", "done", ["status"]),
    ]


def test_a_retry_replays_the_first_result_and_a_reused_id_is_refused(ledger: Ledger) -> None:
    first = change(ledger, NEW["issue"])
    again = change(ledger, NEW["issue"])
    assert again == {**first, "replayed": True}
    assert len(ledger.entries("p").entries) == 1
    with pytest.raises(DocumentError, match="names another ledger change"):
        change(ledger, NEW["task"])


def test_a_mutation_commits_all_or_nothing(ledger: Ledger) -> None:
    with pytest.raises(DocumentError, match="a new task starts todo"):
        change(ledger, NEW["issue"], {**NEW["task"], "status": "done"})
    change(ledger, NEW["task"], op="op-2")
    with pytest.raises(DocumentError, match="no entry T-9"):
        change(
            ledger,
            {"entry": "T-1", "expected": 1, "status": "done"},
            {"entry": "T-9", "expected": 1, "status": "done"},
            op="op-3",
        )
    assert [e.handle for e in ledger.entries("p").entries] == ["T-1"]
    assert ledger.entry("p", "T-1").status == "todo"
    assert [kind for kind, _ in events(ledger)] == ["ledger.entry_created"]


def test_statuses_follow_their_type_and_decisions(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], NEW["issue"])
    for status, message in (
        ("established", "only a decision in effect"),
        ("refuted", "only a decision in effect"),
        ("recorded", "only a published entry is recorded"),
        ("done", "a claim is never done"),
    ):
        with pytest.raises(DocumentError, match=message):
            change(ledger, {"entry": "C-1", "expected": 1, "status": status}, op=status)
    with pytest.raises(DocumentError, match="only a decision in effect"):
        change(ledger, {"entry": "I-1", "expected": 1, "status": "obstructed"}, op="obstructed")
    for entry, status in (("I-1", "split"), ("C-1", "superseded")):
        with pytest.raises(DocumentError, match=f"becomes {status} with its links"):
            change(ledger, {"entry": entry, "expected": 1, "status": status}, op=status)
    change(ledger, {"entry": "C-1", "expected": 1, "status": "disputed"}, op="disputed")
    assert ledger.entry("p", "C-1").status == "disputed"


def test_a_new_entry_starts_in_its_first_status_with_its_fields(ledger: Ledger) -> None:
    for n, (bad, message) in enumerate(
        (
            ({**NEW["task"], "status": "done"}, "a new task starts todo"),
            ({**NEW["claim"], "origin": None}, "claims and results, and only they"),
            ({**NEW["issue"], "origin": "project"}, "claims and results, and only they"),
            ({**NEW["claim"], "origin": "published"}, "come with evidence links"),
            ({**NEW["reference"], "fields": {"published": "2024"}}, "names a locator"),
            (
                {**NEW["reference"], "fields": {"identifiers": {"doi": "10.1/x"}}},
                "when it was retrieved",
            ),
            ({**NEW["task"], "fields": {"kind": "lemma"}}, "task fields"),
            ({**NEW["task"], "title": None}, "a title and a statement"),
        )
    ):
        with pytest.raises(DocumentError, match=message):
            change(ledger, bad, op=f"bad-{n}")
    assert ledger.entries("p").entries == []


def test_who_may_change_what(ledger: Ledger) -> None:
    proposed = change(ledger, NEW["claim"], author=DELEGATED)
    assert proposed["entries"][0]["status"] == "proposed"
    with pytest.raises(DocumentError, match="not_permitted_for_role") as refused:
        change(
            ledger,
            {"entry": "C-1", "expected": 1, "status": "disputed"},
            op="op-2",
            author=DELEGATED,
        )
    assert refused.value.kind == "policy_denied"
    reviewer = Author(kind="agent", session_id="s", run_id="r3", role="reviewer")
    worker = Author(
        kind="worker",
        worker_binding="b",
        environment_id="e",
        incarnation="i",
        role="delegated",
        session_id="s",
    )
    for author in (reviewer, worker):
        with pytest.raises(DocumentError, match="not_permitted_for_role"):
            change(ledger, NEW["issue"], op="op-3", author=author)
    stranger = Author(kind="agent", session_id="t", run_id="r4", role="main")
    with pytest.raises(DocumentError, match="not in this project"):
        change(ledger, NEW["issue"], op="op-4", author=stranger)
    change(ledger, {"entry": "C-1", "expected": 1, "status": "disputed"}, op="op-5", author=USER)
    assert ledger.entry("p", "C-1").author == USER.model_dump(exclude_none=True)


def test_another_projects_entries_are_not_found(ledger: Ledger) -> None:
    made = change(ledger, NEW["issue"])
    entry = made["entries"][0]["entry_id"]
    with pytest.raises(DocumentError, match="no entry"):
        ledger.entry("q", entry)
    with pytest.raises(DocumentError, match="no entry"):
        ledger.entry("q", "I-1")


def test_lists_and_histories_page(ledger: Ledger) -> None:
    change(ledger, *(NEW["task"] for _ in range(5)))
    change(ledger, NEW["issue"], op="op-2")
    change(ledger, {"entry": "T-2", "expected": 1, "status": "done"}, op="op-3")
    page = ledger.entries("p", limit=4)
    assert [e.handle for e in page.entries] == ["T-1", "T-2", "T-3", "T-4"]
    rest = ledger.entries("p", after=page.next_after)
    assert ([e.handle for e in rest.entries], rest.next_after) == (["T-5", "I-1"], None)
    assert [e.handle for e in ledger.entries("p", kind="task", status="done").entries] == ["T-2"]
    for n in range(3):
        change(ledger, {"entry": "I-1", "expected": n + 1, "title": f"Title {n}"}, op=f"t{n}")
    history = ledger.history("p", "I-1", limit=2)
    assert [r.revision for r in history.revisions] == [4, 3]
    older = ledger.history("p", "I-1", before=history.next_before)
    assert [(r.revision, r.statement_changed) for r in older.revisions] == [
        (2, False),
        (1, True),
    ]


def test_several_mutations_apply_together_with_their_provenance(ledger: Ledger) -> None:
    from sandbox_science.orchestrator.ledger import Mutation

    lines = [
        Mutation.model_validate(
            {"operation_id": f"line-{n}", "reason": "From a file.", "changes": [NEW["task"]]}
        )
        for n in range(2)
    ]
    source = {"file": "results.jsonl", "sha256": "0" * 64}
    results = ledger.apply("p", [(m, {**source, "line": n}) for n, m in enumerate(lines, 1)], MAIN)
    assert [r.entries[0].handle for r in results] == ["T-1", "T-2"]
    (revision,) = ledger.history("p", "T-2").revisions
    assert revision.provenance == {**source, "line": 2}
    again = ledger.apply("p", [(lines[0], None)], MAIN)
    assert again[0].replayed
