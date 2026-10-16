# SPDX-License-Identifier: MIT
"""Flags for reconsideration (docs/specs/ledger.md, LG-28, LG-29): what a change flags,
how flags pass on to the entries resting on a flagged one, and their clearing, with a
reason, by the user or the main agent."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from sandbox_science.orchestrator.documents import USER, DocumentError

from .ledger_support import AnchoredLedger as Ledger
from .test_ledger import DELEGATED, MAIN, NEW, change, events
from .test_ledger import ledger as ledger


def flags_on(ledger: Ledger, handle: str) -> list[tuple[str, str, bool]]:
    """Each open flag on ``handle``: its cause, the handle it came from, and whether it
    spread from another flag."""
    return [
        (flag.cause, flag.source["handle"], flag.via is not None)
        for flag in ledger.entry("p", handle).flags
    ]


def rests_on(target: str, kind: str = "builds_on", **more: Any) -> dict[str, Any]:
    return {**NEW["claim"], "links": [{"kind": kind, "entry": target}], **more}


def cites(target: str) -> dict[str, Any]:
    evidence = [{"role": "supports", "type": "proof", "entry": target}]
    return {**NEW["result"], "evidence": evidence}


def test_a_revised_assumption_flags_the_entries_pinning_an_earlier_revision(
    ledger: Ledger,
) -> None:
    change(ledger, NEW["assumption"])
    change(ledger, rests_on("A-1", "assumes"), op="assumes")
    change(ledger, rests_on("C-1"), op="builds")
    retitled = change(ledger, {"entry": "A-1", "expected": 1, "title": "p is 17"}, op="t")
    assert retitled["flagged"] == []
    revised = change(ledger, {"entry": "A-1", "expected": 2, "statement": "$p = 19$."}, op="s")
    assert revised["flagged"] == ["C-1", "C-2"]
    assert flags_on(ledger, "C-1") == [("assumption_revised", "A-1", False)]
    # The flag passes on through the entries resting on C-1, naming where it came from.
    assert flags_on(ledger, "C-2") == [("spread", "C-1", True)]
    (spread,) = ledger.entry("p", "C-2").flags
    assert spread.via == ledger.entry("p", "C-1").flags[0].flag_id
    # A flag changes nothing else: C-1 is still at its revision and status.
    view = ledger.entry("p", "C-1")
    assert (view.revision, view.status, view.open_flags) == (1, "proposed", 1)
    listed = {e.handle: e.open_flags for e in ledger.entries("p").entries}
    assert (listed["C-1"], listed["C-2"], listed["A-1"]) == (1, 1, 0)
    # One event records the change's flags.
    (raised,) = [payload for kind, payload in events(ledger) if kind == "ledger.flags_raised"]
    assert (raised["total"], raised["operation_id"]) == (2, "s")
    assert [(f["handle"], f["cause"], f["from"]) for f in raised["flags"]] == [
        ("C-1", "assumption_revised", "A-1"),
        ("C-2", "spread", "C-1"),
    ]
    # An entry that pins the new revision is not flagged by it.
    change(ledger, rests_on("A-1", "assumes"), op="pins-new")
    assert flags_on(ledger, "C-3") == []


def test_a_withdrawn_assumption_flags_every_entry_resting_on_it(ledger: Ledger) -> None:
    """Probe P7 of the review of #296: evidence pinned to the assumption is flagged too
    (LG-28's evidence clause), not only the entries that assume it."""
    change(ledger, NEW["assumption"])
    change(ledger, rests_on("A-1", "assumes"), op="assumes")
    change(ledger, {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}, op="s")
    change(ledger, rests_on("A-1", "assumes"), cites("A-1"), op="pins-new")
    withdrawn = change(ledger, {"entry": "A-1", "expected": 2, "status": "withdrawn"}, op="w")
    assert withdrawn["flagged"] == ["C-1", "C-2", "R-1"]
    assert flags_on(ledger, "C-2") == [("assumption_withdrawn", "A-1", False)]
    assert flags_on(ledger, "R-1") == [("assumption_withdrawn", "A-1", False)]


def test_an_entry_set_aside_flags_what_builds_on_it_assumes_it_or_cites_it(
    ledger: Ledger,
) -> None:
    change(ledger, NEW["claim"])
    change(ledger, rests_on("C-1"), cites("C-1"), NEW["task"], op="rest")
    disputed = change(ledger, {"entry": "C-1", "expected": 1, "status": "disputed"}, op="d")
    assert disputed["flagged"] == ["C-2", "R-1"]
    assert flags_on(ledger, "R-1") == [("dependency_status", "C-1", False)]
    # Moving to another set-aside status flags anew (probe P2 of the review of #296):
    # disputed, the flag cleared, then withdrawn.
    (flag,) = ledger.entry("p", "R-1").flags
    ledger.change("p", clearing([flag.flag_id], "weak"), MAIN)
    moved = change(ledger, {"entry": "C-1", "expected": 2, "status": "withdrawn"}, op="w")
    assert moved["flagged"] == ["C-2", "R-1"]
    assert flags_on(ledger, "R-1") == [("dependency_status", "C-1", False)]
    assert ledger.entry("p", "R-1").flags[0].source["status"] == "withdrawn"


def test_a_changed_statement_flags_the_evidence_pinned_to_an_earlier_revision(
    ledger: Ledger,
) -> None:
    change(ledger, NEW["claim"])
    change(ledger, cites("C-1"), op="cites")
    assert change(ledger, {"entry": "C-1", "expected": 1, "title": "T"}, op="t")["flagged"] == []
    # The user's check of C-1 is evidence pinned to C-1 itself, which flags nothing.
    ledger.check("p", "C-1", 2, USER, operation_id="checked")
    changed = change(ledger, {"entry": "C-1", "expected": 3, "statement": "Other."}, op="s")
    assert changed["flagged"] == ["R-1"]
    assert flags_on(ledger, "R-1") == [("evidence_changed", "C-1", False)]


def clearing(flags: list[str], op: str, reason: str = "It still holds.") -> dict[str, Any]:
    return {"operation_id": op, "reason": reason, "flags_cleared": flags}


def test_the_user_or_the_main_agent_clears_a_flag_with_a_reason(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], NEW["claim"])
    change(ledger, cites("C-1"), op="cites")
    change(ledger, {"entry": "C-1", "expected": 1, "statement": "Other."}, op="s")
    (flag,) = ledger.entry("p", "R-1").flags
    with pytest.raises(DocumentError, match="not_permitted_for_role"):
        ledger.change("p", clearing([flag.flag_id], "delegated"), DELEGATED)
    with pytest.raises(
        DocumentError, match="names entries to change, work to record, or flags to clear"
    ):
        ledger.change("p", {"operation_id": "none", "reason": "r"}, MAIN)
    with pytest.raises(DocumentError) as missing:
        ledger.change("p", clearing(["flag-" + "0" * 24], "missing"), MAIN)
    assert missing.value.kind == "not_found"
    cleared = ledger.change("p", clearing([flag.flag_id], "cleared"), MAIN)
    assert (cleared.entries, cleared.flags_cleared) == ([], [flag.flag_id])
    assert ledger.entry("p", "R-1").flags == []
    assert ledger.change("p", clearing([flag.flag_id], "cleared"), MAIN).replayed
    with pytest.raises(DocumentError) as again:
        ledger.change("p", clearing([flag.flag_id], "again"), MAIN)
    assert again.value.kind == "conflict"
    with ledger.control.read() as tx:
        by, reason = tx.sql.execute(
            "SELECT cleared_by,reason FROM ledger_flags WHERE flag_id=?", (flag.flag_id,)
        ).fetchone()
    assert (json.loads(by)["run_id"], reason) == ("r1", "It still holds.")
    (cleared_event,) = [p for k, p in events(ledger) if k == "ledger.flags_cleared"]
    assert (cleared_event["flags"][0]["handle"], cleared_event["reason"]) == (
        "R-1",
        "It still holds.",
    )
    assert (cleared_event["run"], cleared_event["total"]) == ("r1", 1)
    # The user clears through the entry it is on; another entry's flag is not found there.
    change(ledger, {"entry": "C-1", "expected": 2, "statement": "Again."}, op="s2")
    (other,) = ledger.entry("p", "R-1").flags
    with pytest.raises(DocumentError, match="C-2 has no flag") as elsewhere:
        ledger.clear_flags("p", "C-2", [other.flag_id], USER, reason="r", operation_id="u1")
    assert elsewhere.value.kind == "not_found"
    by_user = ledger.clear_flags(
        "p", "R-1", [other.flag_id], USER, reason="Revised to pin it.", operation_id="u2"
    )
    assert by_user.flags_cleared == [other.flag_id]


def test_a_version_24_store_takes_flags_after_the_migration(ledger: Ledger, tmp_path) -> None:
    """Version 25 adds the flags table; an older store's entries have none, and its next
    change flags as any does."""
    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.documents import DocumentStore, store_path

    from .control_versions import older_store

    change(ledger, NEW["claim"])
    change(ledger, cites("C-1"), op="cites")
    ledger.documents.close()
    old = tmp_path / "old"
    old.mkdir()
    older_store(store_path(tmp_path), store_path(old), migrations.BASELINE + 22)
    raw = sqlite3.connect(store_path(old))
    try:
        tables = {n for (n,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = raw.execute("PRAGMA user_version").fetchone()[0]
    finally:
        raw.close()
    assert (version, "memory_heads" in tables, "ledger_flags" in tables) == (24, True, False)
    migrated = DocumentStore(store_path(old))
    migrated.initialize()
    try:
        moved = Ledger(migrated)
        assert moved.entry("p", "R-1").flags == []
        flagged = moved.change(
            "p",
            {
                "operation_id": "s",
                "reason": "Recorded.",
                "changes": [{"entry": "C-1", "expected": 1, "statement": "Other."}],
            },
            MAIN,
        )
        assert flagged.flagged == ["R-1"]
    finally:
        migrated.close()


def test_an_entry_that_comes_to_rest_on_a_flagged_set_aside_or_restated_entry_is_flagged(
    ledger: Ledger,
) -> None:
    """Probe P3 of the review of #296: an entry made, or revised, to rest on an entry with
    an open flag, on a set-aside entry, or on a revision older than the target's
    statement, is flagged as the entries already resting on it were."""
    change(ledger, NEW["assumption"])
    change(ledger, rests_on("A-1", "assumes"), op="assumes")
    change(ledger, {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}, op="s")
    (first,) = ledger.entry("p", "C-1").flags
    # A delegated agent builds on flagged C-1.
    late = change(ledger, rests_on("C-1"), op="late", author=DELEGATED)
    assert late["flagged"] == ["C-2"]
    (spread,) = ledger.entry("p", "C-2").flags
    assert (spread.cause, spread.source["handle"], spread.via) == ("spread", "C-1", first.flag_id)
    # The main agent assumes A-1 at its stale revision 1.
    stale = {**NEW["claim"], "links": [{"kind": "assumes", "entry": "A-1", "revision": 1}]}
    assert change(ledger, stale, op="stale")["flagged"] == ["C-3"]
    assert flags_on(ledger, "C-3") == [("assumption_revised", "A-1", False)]
    # Evidence pinned to a restated revision of a claim.
    change(ledger, NEW["claim"], op="c4")
    change(ledger, {"entry": "C-4", "expected": 1, "statement": "Restated."}, op="c4s")
    old = [{"role": "supports", "type": "proof", "entry": "C-4", "revision": 1}]
    assert change(ledger, {**NEW["result"], "evidence": old}, op="old")["flagged"] == ["R-1"]
    assert flags_on(ledger, "R-1") == [("evidence_changed", "C-4", False)]
    # An entry that comes to build on a withdrawn one, made or revised.
    change(ledger, {"entry": "C-4", "expected": 2, "status": "withdrawn"}, op="w")
    assert change(ledger, rests_on("C-4"), op="on-withdrawn")["flagged"] == ["C-5"]
    assert flags_on(ledger, "C-5") == [("dependency_status", "C-4", False)]
    task = {"entry": "C-3", "expected": 1, "links": [*stale["links"], {"kind": "builds_on",
            "entry": "C-4"}]}  # fmt: skip
    assert change(ledger, task, op="relinked")["flagged"] == ["C-3"]
    assert ("dependency_status", "C-4", False) in flags_on(ledger, "C-3")
    # A revision that keeps its links raises nothing again once the flags are cleared.
    clear_all = {
        "operation_id": "clear",
        "reason": "Holds.",
        "flags_cleared_on": [{"entry": "C-3"}],
    }
    assert ledger.change("p", clear_all, MAIN).cleared_total == 2
    assert change(ledger, {"entry": "C-3", "expected": 2, "title": "T"}, op="t")["flagged"] == []


def test_flags_clear_in_bulk_by_entry_and_cause_and_the_user_reopens_them(
    ledger: Ledger,
) -> None:
    change(ledger, NEW["assumption"], NEW["claim"])
    both = {**cites("C-1"), "links": [{"kind": "assumes", "entry": "A-1"}]}
    change(ledger, both, op="both")
    change(ledger, {"entry": "A-1", "expected": 1, "statement": "$q$."}, op="a")
    change(ledger, {"entry": "C-1", "expected": 1, "statement": "Other."}, op="c")
    assert sorted(cause for cause, _, _ in flags_on(ledger, "R-1")) == [
        "assumption_revised",
        "evidence_changed",
    ]
    by_cause = {
        "operation_id": "by-cause",
        "reason": "C-1 still supports it.",
        "flags_cleared_on": [{"entry": "R-1", "cause": "evidence_changed"}],
    }
    assert ledger.change("p", by_cause, MAIN).cleared_total == 1
    assert flags_on(ledger, "R-1") == [("assumption_revised", "A-1", False)]
    with pytest.raises(DocumentError, match="R-1 has no open flag of cause evidence_changed"):
        ledger.change("p", {**by_cause, "operation_id": "again"}, MAIN)
    (cleared,) = ledger.history("p", "R-1").flags_cleared
    assert (cleared.cause, cleared.reason, cleared.cleared_by["role"]) == (
        "evidence_changed",
        "C-1 still supports it.",
        "main",
    )
    assert ledger.history("p", "R-1", before=2).flags_cleared == []
    # Only the user reopens a flag, as a new flag that names the one it reopens.
    reopen = {
        "operation_id": "reopen",
        "reason": "It does not.",
        "flags_reopened": [cleared.flag_id],
    }
    with pytest.raises(DocumentError, match="only the user reopens a flag"):
        ledger.change("p", reopen, MAIN)
    reopened = ledger.reopen_flags(
        "p", "R-1", [cleared.flag_id], USER, reason="It does not.", operation_id="reopen"
    )
    (again,) = [f for f in ledger.entry("p", "R-1").flags if f.cause == "evidence_changed"]
    assert (reopened.flags_reopened, again.reopens) == ([again.flag_id], cleared.flag_id)
    with pytest.raises(DocumentError) as twice:
        ledger.reopen_flags(
            "p", "R-1", [cleared.flag_id], USER, reason="r", operation_id="reopen-2"
        )
    assert twice.value.kind == "conflict"
    # All of an entry's open flags at once.
    everything = ledger.clear_flags("p", "R-1", None, USER, reason="Holds.", operation_id="all")
    assert (everything.cleared_total, ledger.entry("p", "R-1").flags) == (2, [])


def test_a_change_lists_as_many_flagged_entries_as_it_shows_and_counts_them_all(
    ledger: Ledger,
) -> None:
    """Probe P4 of the review of #296: 2,600 flagged entries made a 22,697-character
    answer, all of it in the receipt."""
    from sandbox_science.orchestrator.ledger.models import SHOWN_FLAGGED

    change(ledger, NEW["assumption"])
    for batch in range(2):
        change(ledger, *[rests_on("A-1", "assumes")] * 32, op=f"b{batch}")
    revised = {"entry": "A-1", "expected": 1, "statement": "$q$."}
    result = change(ledger, revised, op="s")
    assert (len(result["flagged"]), result["flagged_total"]) == (SHOWN_FLAGGED, 64)
    assert change(ledger, revised, op="s") == {**result, "replayed": True}


def test_a_cycle_of_links_flags_each_entry_once(ledger: Ledger) -> None:
    """Probe P5 of the review of #296."""
    change(ledger, NEW["assumption"])
    change(ledger, rests_on("A-1", "assumes"), op="c1")
    change(ledger, rests_on("C-1"), op="c2")
    links = [{"kind": "assumes", "entry": "A-1", "revision": 1}, {"kind": "builds_on",
             "entry": "C-2"}]  # fmt: skip
    change(ledger, {"entry": "C-1", "expected": 1, "links": links}, op="cycle")
    result = change(ledger, {"entry": "A-1", "expected": 1, "statement": "$q$."}, op="s")
    assert result["flagged"] == ["C-1", "C-2"]
    assert flags_on(ledger, "C-1") == [("assumption_revised", "A-1", False)]
    assert flags_on(ledger, "C-2") == [("spread", "C-1", True)]


def test_an_import_raises_flags_as_a_change_does(ledger: Ledger) -> None:
    """Probe P6 of the review of #296: a results file's lines are changes, and flag."""
    change(ledger, NEW["claim"])
    change(ledger, cites("C-1"), op="cites")
    line = {
        "operation_id": "line-1",
        "reason": "Recorded.",
        "changes": [{"entry": "C-1", "expected": 1, "statement": "Computed."}],
    }
    from sandbox_science.orchestrator.ledger import Mutation

    mutation = Mutation.model_validate(line)
    (result,) = ledger.apply("p", [(mutation, {"file": "results.jsonl", "line": 1})], MAIN)
    assert (result.flagged, flags_on(ledger, "R-1")) == (
        ["R-1"],
        [("evidence_changed", "C-1", False)],
    )
