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
from sandbox_science.orchestrator.ledger import Ledger

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
    raised = [payload for kind, payload in events(ledger) if kind == "ledger.flag_raised"]
    assert [(p["handle"], p["cause"]) for p in raised] == [
        ("C-1", "assumption_revised"),
        ("C-2", "spread"),
    ]
    # An entry that pins the new revision is not flagged by it.
    change(ledger, rests_on("A-1", "assumes"), op="pins-new")
    assert flags_on(ledger, "C-3") == []


def test_a_withdrawn_assumption_flags_every_entry_that_assumes_it(ledger: Ledger) -> None:
    change(ledger, NEW["assumption"])
    change(ledger, rests_on("A-1", "assumes"), op="assumes")
    change(ledger, {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}, op="s")
    change(ledger, rests_on("A-1", "assumes"), op="pins-new")
    withdrawn = change(ledger, {"entry": "A-1", "expected": 2, "status": "withdrawn"}, op="w")
    assert withdrawn["flagged"] == ["C-1", "C-2"]
    assert flags_on(ledger, "C-2") == [("assumption_withdrawn", "A-1", False)]


def test_an_entry_set_aside_flags_what_builds_on_it_assumes_it_or_cites_it(
    ledger: Ledger,
) -> None:
    change(ledger, NEW["claim"])
    change(ledger, rests_on("C-1"), cites("C-1"), NEW["task"], op="rest")
    disputed = change(ledger, {"entry": "C-1", "expected": 1, "status": "disputed"}, op="d")
    assert disputed["flagged"] == ["C-2", "R-1"]
    assert flags_on(ledger, "R-1") == [("dependency_status", "C-1", False)]
    # Leaving one set-aside status for another flags nothing new.
    change(ledger, {"entry": "C-1", "expected": 2, "status": "withdrawn"}, op="w")
    assert len(ledger.entry("p", "R-1").flags) == 1


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
    with pytest.raises(DocumentError, match="names entries to change or flags to clear"):
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
    (cleared_event,) = [p for k, p in events(ledger) if k == "ledger.flag_cleared"]
    assert (cleared_event["handle"], cleared_event["reason"], cleared_event["run"]) == (
        "R-1",
        "It still holds.",
        "r1",
    )
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
