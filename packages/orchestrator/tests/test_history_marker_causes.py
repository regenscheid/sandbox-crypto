# SPDX-License-Identifier: MIT
"""The retirement causes step (f2a) left for their records (docs/specs/context-management.md,
CM-21a): a work marker on a claim or result retired by a later assessment that decided it;
a claimed obstruction retired by the assessment of it that decided it; a failure aggregate
retired by later identical calls that succeeded, and kept, once merged, while a work
record cites its calls. When in doubt a marker stays current."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from sandbox_science.orchestrator.context.history import (
    Archive,
    Archived,
    Entry,
    Judge,
    ledger_cites,
    ledger_works,
)
from sandbox_science.orchestrator.context.history.markers import Held, Work
from sandbox_science.orchestrator.context.history.overview import merge
from sandbox_science.orchestrator.context.history.skeleton import skeleton
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.ledger import Ledger

from .loop_harness import PROJECT, SESSION
from .test_history_overviews import archive as archive
from .test_history_overviews import entries, first_level, folded, said
from .test_ledger import MAIN

Json = dict[str, Any]


def work(entry: str, ending: str, record: str | None, at: str = "msg-1") -> Json:
    marker = {"kind": "caveat", "subject": entry, "text": f"work on {entry} ended {ending}",
              "entry": entry, "work": ending, "at": at}  # fmt: skip
    return marker if record is None else {**marker, "record": record}


def works(found: dict[str, list[Work]]) -> Any:
    return lambda handle: found.get(handle)


def test_an_assessment_that_decided_a_claim_retires_its_work_markers() -> None:
    """A work marker on a claim or result retires when a later assessment of the entry
    decided it, accepted or rejected; an inconclusive one, an earlier one, a marker without
    its record's ID, or work records that cannot be read leave it current."""
    markers = (
        work("C-3", "limit", "work-c3"),
        work("R-2", "inconclusive", "work-r2"),
        work("C-4", "limit", None),  # stored before records were named
        work("C-5", "limit", "work-c5"),
    )
    record = replace(first_level(), markers=markers)
    held = {h: Held("proposed", 2) for h in ("C-3", "R-2", "C-4", "C-5")}
    found = {
        "C-3": [Work("work-c3", 4, "limit"), Work("work-a1", 2, "assessment", "accepted")],
        "R-2": [Work("work-r2", 4, "assessment", "inconclusive"),
                Work("work-a2", 6, "assessment", "inconclusive")],
        "C-4": [Work("work-c4", 4, "limit"), Work("work-a3", 6, "assessment", "rejected")],
    }  # fmt: skip
    judge = Judge(entries(held), works=works(found))
    assert judge.retired([("F-1", record)]) == {}  # C-5's records cannot be read
    found["C-3"].append(Work("work-a4", 7, "assessment", "rejected"))
    found["R-2"].append(Work("work-a5", 8, "assessment", "accepted"))
    assert judge.retired([("F-1", record)]) == {"F-1": {
        0: "the assessment work-a4 decided C-3 (rejected)",
        1: "the assessment work-a5 decided R-2 (accepted)",
    }}  # fmt: skip


def test_a_claimed_obstruction_retires_when_its_assessment_decides_it() -> None:
    """A claimed obstruction on an open issue retires when the assessment of that record
    decides it; an inconclusive one, or a decided one of another record, leaves it."""
    record = replace(first_level(), markers=(work("I-1", "obstruction_claimed", "work-o1"),))
    held = {"I-1": Held("open", 3)}
    found = {"I-1": [
        Work("work-o1", 4, "obstruction_claimed"),
        Work("work-a1", 5, "assessment", "inconclusive", "work-o1"),
        Work("work-a2", 6, "assessment", "accepted", "work-other"),
    ]}  # fmt: skip
    judge = Judge(entries(held), works=works(found))
    assert judge.retired([("F-1", record)]) == {}
    found["I-1"].append(Work("work-a3", 7, "assessment", "rejected", "work-o1"))
    assert judge.retired([("F-1", record)]) == {
        "F-1": {0: "the assessment work-a3 decided the claimed obstruction (rejected)"}
    }


def called(archive: Archive, n: int, arguments: Json, state: str) -> Archived:
    """A shell call with ``arguments`` and its result in ``state``; the result."""
    call = {"id": f"call-{n}", "type": "function",
            "function": {"name": "shell", "arguments": json.dumps(arguments)}}  # fmt: skip
    archive.append(PROJECT, SESSION, "r", Entry("assistant", "assistant", None, tool_calls=(call,)))
    result = Entry("tool", "tool_result", json.dumps({"state": state}), source="shell",
                   call_id=f"call-{n}")  # fmt: skip
    return archive.append(PROJECT, SESSION, "r", result)


def test_a_failure_aggregate_retires_when_the_same_calls_later_succeed(archive: Archive) -> None:
    """Every call the aggregate counts followed by a call of the same tool with the same
    arguments, written in any key order, that succeeded: retired. One not yet, or one
    tried with other arguments: current."""
    first = called(archive, 1, {"script": "make", "cwd": "a"}, "failed")
    second = called(archive, 2, {"script": "test"}, "timed_out")
    called(archive, 3, {"script": "make", "cwd": "b"}, "succeeded")  # other arguments
    fixed = called(archive, 4, {"cwd": "a", "script": "make"}, "succeeded")
    failures = {"kind": "failures", "subject": "shell", "count": 2,
                "ids": [first.message_id, second.message_id]}  # fmt: skip
    record = replace(first_level(), markers=(failures,))
    rows = archive.messages(PROJECT, SESSION)
    assert Judge().retired([("F-1", record)], rows) == {}
    again = called(archive, 5, {"script": "test"}, "succeeded")
    rows = archive.messages(PROJECT, SESSION)
    assert Judge().retired([("F-1", record)], rows) == {
        "F-1": {0: f"a later identical call succeeded ({again.message_id})"}
    }
    one = replace(record, markers=({**failures, "ids": [first.message_id]},))
    assert Judge().retired([("F-1", one)], rows)["F-1"][0].endswith(f"({fixed.message_id})")


def test_a_merged_failure_aggregate_is_kept_while_a_work_record_cites_its_calls(
    archive: Archive,
) -> None:
    """Once its fold is merged into an overview, a failure aggregate stays current while a
    work record cites one of its calls, and when the citations cannot be read."""
    rows = [said(archive, f"Message {n}.") for n in range(2)]
    failures = {"kind": "failures", "subject": "shell", "count": 1, "ids": ["msg-f"]}
    overview = merge([folded(archive, rows[:1], [failures]), folded(archive, rows[1:])], 4_000)
    merged = [("msg-overview", overview)]
    assert Judge(cites=lambda ids: None).retired(merged) == {
        "msg-overview": {0: "its fold was merged into an overview"}
    }
    assert Judge(cites=lambda ids: "work-1" if "msg-f" in ids else None).retired(merged) == {}

    def unreadable(ids: Any) -> str:
        raise OSError("the store is gone")

    assert Judge(cites=unreadable).retired(merged) == {}


def test_the_skeleton_names_a_work_records_id_and_the_ledger_tells_the_judge(
    tmp_path: Path,
) -> None:
    """A work marker keeps the ID of the record its change wrote, which the ledger's work
    records and their anchors are read by."""
    from sandbox_science.orchestrator.projects import ProjectStore

    projects = ProjectStore(tmp_path)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    projects.close()
    documents = DocumentStore(store_path(tmp_path))
    try:
        ledger, archive = Ledger(documents), Archive(documents.control)
        failed = called(archive, 1, {"script": "bkz"}, "failed")
        ledger.change(PROJECT, {"operation_id": "i", "reason": "Asked.", "changes": [
            {"type": "issue", "title": "Q", "statement": "Is it?"}]}, USER)  # fmt: skip
        ran = {"entry": "I-1", "tried": "BKZ.", "ending": "limit", "limit": "time",
               "anchors": [{"message": failed.message_id}]}  # fmt: skip
        asked = {"reason": "Ran out.", "work": [ran]}
        made = ledger.change(PROJECT, {"operation_id": "w", **asked}, MAIN)
        call = {"id": "call-9", "type": "function",
                "function": {"name": "ledger_change", "arguments": json.dumps(asked)}}  # fmt: skip
        archive.append(PROJECT, SESSION, "r", Entry("assistant", "assistant", None,
                                                    tool_calls=(call,)))  # fmt: skip
        result = Entry("tool", "tool_result", made.model_dump_json(), source="ledger_change",
                       call_id="call-9")  # fmt: skip
        archive.append(PROJECT, SESSION, "r", result)
        rows = archive.messages(PROJECT, SESSION)
        built = skeleton(rows, 20_000, 2_000, 3.0)
        (marker,) = [m for m in built.markers if "work" in m]
        assert marker["record"] == made.work[0]
        listed = ledger_works(ledger, PROJECT)("I-1")
        assert [(w.record, w.ending) for w in listed or ()] == [(made.work[0], "limit")]
        cites = ledger_cites(ledger, PROJECT)
        assert cites([failed.message_id]) == made.work[0]
        assert cites(["msg-" + "0" * 16]) is None
        assert ledger_works(ledger, PROJECT)("I-9") is None
    finally:
        documents.close()
