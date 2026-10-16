# SPDX-License-Identifier: MIT
"""Work records (docs/specs/ledger.md, LG-6, LG-7, LG-12, LG-13): append-only records on
an issue, a claim or a result of what was tried and how it ended, with pinned
references, written in a change after its entries, by any role that changes the ledger,
and read newest first."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.documents import USER, Author, DocumentError
from sandbox_science.orchestrator.ledger.tool import LedgerTool

from .ledger_support import AnchoredLedger as Ledger
from .test_ledger import DELEGATED, MAIN, NEW, change, events
from .test_ledger import ledger as ledger
from .test_ledger_evidence import publish

REVIEWER = Author(kind="agent", session_id="s", run_id="r3", role="reviewer")


def work(
    ledger: Ledger, *items: dict[str, Any], op: str = "w-1", author: Author = MAIN, **more: Any
) -> dict[str, Any]:
    return ledger.change(
        "p", {"operation_id": op, "reason": "Tried it.", "work": list(items), **more}, author
    ).model_dump()


def ended(ledger: Ledger, delegation: str = "dlg-" + "0" * 24) -> str:
    """A check delegation of session s that returned."""
    from sandbox_science.orchestrator.delegation.records import (
        Delegation,
        DelegationRecords,
        Ending,
    )

    records = DelegationRecords(ledger.documents)
    made = Delegation.model_validate(
        {"delegation_id": delegation, "parent": "20261015T120000000000Z-root",
         "run": "20261015T120001000000Z-child", "kind": "check",
         "brief": {"task": "Check."}, "started_at": "2026-10-15T12:00:01+00:00"}
    )  # fmt: skip
    records.start("p", "s", made)
    records.end("p", "s", delegation, Ending("returned", "answered", "Done."))
    return delegation


def test_a_limit_leaves_its_issue_open_and_reads_newest_first(ledger: Ledger) -> None:
    """LG-6, LG-7: a work record that ends limit changes no status; it is kept on its
    issue, newest first, with who recorded it and the revision it was recorded at."""
    change(ledger, NEW["issue"])
    first = work(ledger, {"entry": "I-1", "tried": "Brute force to $2^{40}$.",
                          "ending": "limit", "limit": "time"})  # fmt: skip
    work(ledger, {"entry": "I-1", "tried": "A lattice attack.", "ending": "limit",
                  "limit": "ideas"}, op="w-2", author=DELEGATED)  # fmt: skip
    assert first["entries"] == []
    assert len(first["work"]) == 1
    assert ledger.entry("p", "I-1").status == "open"
    page = ledger.work("p", "I-1")
    assert [(r.tried, r.limit, r.author["role"]) for r in page.records] == [
        ("A lattice attack.", "ideas", "delegated"),
        ("Brute force to $2^{40}$.", "time", "main"),
    ]
    assert page.records[1].record_id == first["work"][0]
    assert (page.records[1].entry_revision, page.next_before) == (1, None)
    one = ledger.work("p", "I-1", limit=1)
    assert one.next_before == one.records[0].position
    assert [r.tried for r in ledger.work("p", "I-1", before=one.next_before).records] == [
        "Brute force to $2^{40}$."
    ]
    assert [kind for kind, _ in events(ledger)].count("ledger.work_recorded") == 2


def test_a_change_records_work_on_what_it_creates(ledger: Ledger) -> None:
    """LG-35's shape for a delegation's outcome (LG-F8): the change creates a claim, and
    records the outcome's work on the issue with the claim it produced, and a claimed
    obstruction and a limit as parts of it, by keys."""
    change(ledger, NEW["issue"])
    delegation = ended(ledger)
    made = ledger.change(
        "p",
        {
            "operation_id": "outcome",
            "reason": "The check returned.",
            "changes": [{**NEW["claim"], "key": "found",
                         "links": [{"kind": "answers", "entry": "I-1"}]}],
            "work": [
                {"key": "outcome", "entry": "I-1", "tried": "The delegated check.",
                 "ending": "produced", "produced": ["new:found"],
                 "references": [{"delegation": delegation, "note": "As returned."}]},
                {"entry": "I-1", "tried": "Index calculus.", "ending": "obstruction_claimed",
                 "references": [{"work": "new:outcome"}], "part_of": "new:outcome"},
                {"entry": "new:found", "tried": "Every case of the proof.", "ending": "limit",
                 "limit": "context", "part_of": "new:outcome"},
            ],
        },
        DELEGATED,
    )  # fmt: skip
    outcome, claimed, cut = made.work
    assert [r.record_id for r in ledger.work("p", "I-1").records] == [claimed, outcome]
    (record,) = [r for r in ledger.work("p", "I-1").records if r.record_id == outcome]
    assert (record.produced, record.references) == (
        ["C-1"],
        [{"delegation": delegation, "note": "As returned."}],
    )
    assert [(r.record_id, r.part_of) for r in ledger.work("p", "C-1").records] == [(cut, outcome)]
    replay = ledger.change(
        "p",
        {"operation_id": "outcome", "reason": "The check returned.",
         "changes": [{**NEW["claim"], "key": "found",
                      "links": [{"kind": "answers", "entry": "I-1"}]}],
         "work": [{"key": "outcome", "entry": "I-1", "tried": "The delegated check.",
                   "ending": "produced", "produced": ["new:found"],
                   "references": [{"delegation": delegation, "note": "As returned."}]},
                  {"entry": "I-1", "tried": "Index calculus.", "ending": "obstruction_claimed",
                   "references": [{"work": "new:outcome"}], "part_of": "new:outcome"},
                  {"entry": "new:found", "tried": "Every case of the proof.", "ending": "limit",
                   "limit": "context", "part_of": "new:outcome"}]},
        DELEGATED,
    )  # fmt: skip
    assert (replay.replayed, replay.work) == (True, made.work)


def test_an_assessment_names_the_claim_it_assesses_on_the_same_entry(
    ledger: Ledger, tmp_path: Path
) -> None:
    change(ledger, NEW["issue"], NEW["claim"])
    artifact, digest = publish(ledger, tmp_path)
    (claim,) = work(
        ledger,
        {
            "entry": "I-1",
            "tried": "Index calculus.",
            "ending": "obstruction_claimed",
            "references": [{"artifact": artifact, "revision": 1}],
        },
    )["work"]
    (record,) = ledger.work("p", "I-1").records
    assert record.references == [{"artifact": artifact, "revision": 1, "sha256": digest}]
    accepted = work(ledger, {"entry": "I-1", "tried": "Read the argument.",
                             "ending": "assessment", "assesses": claim,
                             "verdict": "accepted"}, op="w-2", author=USER)  # fmt: skip
    (latest, _) = ledger.work("p", "I-1").records
    assert (latest.record_id, latest.assesses, latest.verdict) == (
        accepted["work"][0],
        claim,
        "accepted",
    )
    with pytest.raises(DocumentError, match="assesses work on another entry"):
        work(ledger, {"entry": "C-1", "tried": "x", "ending": "assessment", "assesses": claim,
                      "verdict": "rejected"}, op="w-3")  # fmt: skip


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({"ending": "limit"}, "names what ran out"),
        ({"ending": "produced"}, "names the entries"),
        ({"ending": "obstruction_claimed"}, "cites its evidence"),
        ({"ending": "assessment", "verdict": "accepted"}, "gives its verdict"),
        ({"ending": "limit", "limit": "time", "verdict": "accepted"}, "only an assessment"),
        ({"ending": "limit", "limit": "time", "entry": "T-1"}, "on an issue, a claim"),
        ({"ending": "limit", "limit": "time", "entry": "new:x"}, "no new entry"),
        ({"ending": "limit", "limit": "time", "part_of": "new:x"}, "keyed new:x"),
        ({"ending": "limit", "limit": "time", "part_of": "work-" + "0" * 24}, "no work record"),
        (
            {"ending": "assessment", "references": [{"delegation": "dlg-" + "1" * 24}]},
            "no delegation",
        ),
        ({"ending": "assessment", "references": [{"artifact": "a"}]}, "names its revision"),
        ({"ending": "assessment", "tried": "Hidden‮flip"}, "may not see"),
    ],
)
def test_a_work_record_that_does_not_hold_is_refused(
    ledger: Ledger, item: dict[str, Any], message: str
) -> None:
    change(ledger, NEW["issue"], NEW["task"])
    with pytest.raises(DocumentError, match=message):
        work(ledger, {"entry": "I-1", "tried": "Tried.", **item})
    assert ledger.work("p", "I-1").records == []


def test_work_is_append_only_and_the_reviewer_records_none(ledger: Ledger) -> None:
    change(ledger, NEW["issue"])
    work(ledger, {"entry": "I-1", "tried": "Tried.", "ending": "limit", "limit": "tools"})
    for statement in ("UPDATE ledger_work SET tried='x'", "DELETE FROM ledger_work"):
        with (
            pytest.raises(sqlite3.IntegrityError, match="append-only"),
            ledger.control.write() as tx,
        ):
            tx.sql.execute(statement)
    with pytest.raises(DocumentError, match="does not change the ledger"):
        work(ledger, {"entry": "I-1", "tried": "x", "ending": "limit", "limit": "time"},
             op="w-2", author=REVIEWER)  # fmt: skip


@pytest.mark.asyncio
async def test_the_agent_records_and_reads_work_through_its_tools(ledger: Ledger) -> None:
    from sandbox_science.contracts import ExecutionContext

    from .test_ledger_tools import CONTEXT

    assert isinstance(CONTEXT, ExecutionContext)
    change(ledger, NEW["issue"])
    tools = LedgerTool(ledger, CONTEXT, run_id="r1", role="main", output_chars=4000)
    item = {"entry": "I-1", "tried": "x" * 3000, "ending": "limit", "limit": "time"}
    for number in range(3):
        text, record = await tools.call(
            "ledger_change", {"reason": "Tried.", "work": [item]}, operation=f"t-{number}"
        )
        assert len(record["result"]["work"]) == 1, text
    first = json.loads((await tools.call("ledger_work", {"entry": "I-1"}, operation="r"))[0])
    assert len(first["records"]) == 1
    assert first["next_before"] is not None
    assert len(first["records"][0]["tried"]) < 3000
    rest = json.loads(
        (await tools.call("ledger_work", {"entry": "I-1", "before": first["next_before"]},
                          operation="r2"))[0]
    )  # fmt: skip
    assert rest["records"][0]["position"] < first["records"][0]["position"]


def test_a_version_33_store_migrates_and_keeps_its_ledger(tmp_path: Path) -> None:
    """Version 34 adds work records and keeps the rest: an entry of a version 33 store
    takes a work record once migrated."""
    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.documents import DocumentStore, store_path
    from sandbox_science.orchestrator.projects import ProjectStore

    from .control_versions import older_store

    current = ProjectStore(tmp_path / "current")
    try:
        current.create("p", "minimal", "sha256:" + "a" * 64)
        current.open_session("p", "s", dedicated=False)
    finally:
        current.close()
    documents = DocumentStore(store_path(tmp_path / "current"))
    try:
        change(Ledger(documents), NEW["issue"])
    finally:
        documents.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 33)
    with sqlite3.connect(target / "control.sqlite3") as db:
        assert "ledger_work" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    ProjectStore(target).close()
    documents = DocumentStore(store_path(target))
    try:
        migrated = Ledger(documents)
        assert migrated.entry("p", "I-1").status == "open"
        work(migrated, {"entry": "I-1", "tried": "Tried.", "ending": "limit", "limit": "time"})
        assert len(migrated.work("p", "I-1").records) == 1
    finally:
        documents.close()
    with sqlite3.connect(target / "control.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)


def test_produced_names_only_what_the_change_or_the_cited_delegation_created(
    ledger: Ledger,
) -> None:
    """The review (low, #365): produced named any entry. It names entries the change
    creates, or those the delegation the record cites created, never the record's own."""
    child = Author(kind="agent", session_id="s", run_id="20261015T120001000000Z-child",
                   role="delegated")  # fmt: skip
    change(ledger, NEW["issue"], NEW["claim"])
    change(ledger, NEW["claim"], op="by-child", author=child)  # C-2, the child's
    delegation = ended(ledger)
    produced = {"entry": "I-1", "tried": "Tried.", "ending": "produced"}
    for names, message in ((["C-1"], "named as produced"), (["I-1"], "names it as produced")):
        with pytest.raises(DocumentError, match=message):
            work(ledger, {**produced, "produced": names,
                          "references": [{"delegation": delegation}]})  # fmt: skip
    made = work(ledger, {**produced, "produced": ["C-2"],
                         "references": [{"delegation": delegation}]})  # fmt: skip
    assert ledger.work("p", "I-1").records[0].record_id == made["work"][0]
    assert ledger.work("p", "I-1").records[0].produced == ["C-2"]


def test_long_notes_are_refused_cleanly_and_kept_as_written(ledger: Ledger) -> None:
    """The review (low, #365): notes could pass the model and fail the table's bound.
    They hold 16 KiB in all, and are kept as UTF-8, not escaped."""
    change(ledger, NEW["issue"])
    (first,) = work(ledger, {"entry": "I-1", "tried": "x", "ending": "limit",
                             "limit": "time"})["work"]  # fmt: skip
    wide = [{"work": first, "note": "格" * 1000} for _ in range(16)]
    with pytest.raises(DocumentError, match="notes are at most 16384 bytes"):
        work(ledger, {"entry": "I-1", "tried": "x", "ending": "assessment",
                      "references": wide}, op="w-2")  # fmt: skip
    fits = [{"work": first, "note": "格" * 340} for _ in range(16)]
    work(ledger, {"entry": "I-1", "tried": "x", "ending": "assessment", "references": fits},
         op="w-3")  # fmt: skip
    assert ledger.work("p", "I-1").records[0].references[0]["note"] == "格" * 340


def test_no_insert_replaces_a_record_and_an_entry_holds_a_bounded_number(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hardening from the review: INSERT OR REPLACE cannot rewrite a record, and an
    entry holds at most MAX_ENTRY_WORK records."""
    from sandbox_science.orchestrator.ledger import store as ledger_store

    change(ledger, NEW["issue"])
    item = {"entry": "I-1", "tried": "x", "ending": "limit", "limit": "time"}
    work(ledger, item)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), ledger.control.write() as tx:
        tx.sql.execute("INSERT OR REPLACE INTO ledger_work SELECT * FROM ledger_work")
    monkeypatch.setattr(ledger_store, "MAX_ENTRY_WORK", 2)
    work(ledger, item, op="w-2")
    with pytest.raises(DocumentError, match="holds 2 work records"):
        work(ledger, item, op="w-3")


@pytest.mark.asyncio
async def test_a_record_at_its_largest_reads_within_a_small_budget(ledger: Ledger) -> None:
    """The review (medium, #365): a record larger than the tool's budget made its page
    unreadable (IndexError). It is shortened, notes first, and every page reads."""
    from .test_ledger_tools import CONTEXT

    change(ledger, NEW["issue"])
    (first,) = work(ledger, {"entry": "I-1", "tried": "x", "ending": "limit",
                             "limit": "time"})["work"]  # fmt: skip
    largest = {
        "entry": "I-1",
        "tried": "y" * 8192,
        "ending": "assessment",
        "references": [{"work": first, "note": "z" * 1024} for _ in range(16)],
    }
    for number in range(2):
        work(ledger, largest, op=f"big-{number}")
    tools = LedgerTool(ledger, CONTEXT, run_id="r1", role="main", output_chars=2000)
    seen: list[int] = []
    before: int | None = None
    for _ in range(5):
        args: dict[str, Any] = {"entry": "I-1", **({"before": before} if before else {})}
        text, _ = await tools.call("ledger_work", args, operation="r")
        assert len(text) <= 2000
        page = json.loads(text)
        seen += [r["position"] for r in page["records"]]
        before = page["next_before"]
        if before is None:
            break
    assert len(seen) == 3
    assert seen == sorted(seen, reverse=True)


@pytest.mark.asyncio
async def test_a_results_file_carries_work_records(ledger: Ledger) -> None:
    """LG-41: a results file's lines are mutations, so they carry work records too, each
    keeping the file's path, SHA-256 and line as its provenance."""
    from .test_ledger_tools import Files, imports, lines, tool

    change(ledger, NEW["issue"])
    files = Files()
    files.files["out.jsonl"] = lines(
        {"operation_id": "helper-1", "reason": "The helper's sweep.",
         "work": [{"entry": "I-1", "tried": "Swept $q < 2^{20}$.", "ending": "limit",
                   "limit": "time"}]},
    )  # fmt: skip
    found = await imports(tool(ledger, files), files, "out.jsonl")
    assert (found["work"], len(found["imported"][0]["work"])) == (1, 1), found
    (record,) = ledger.work("p", "I-1").records
    assert record.provenance is not None
    assert (record.provenance["file"], record.provenance["line"]) == ("out.jsonl", 1)
