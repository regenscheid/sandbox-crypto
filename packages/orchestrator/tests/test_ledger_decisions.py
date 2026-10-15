# SPDX-License-Identifier: MIT
"""Decisions and the gate (docs/specs/ledger.md, LG-4, LG-7, LG-13, LG-17, LG-30):
statuses only a decision in effect sets, reversals by new decisions, and the gate the
main agent's decision to establish a result needs, with its pending clauses named."""

from __future__ import annotations

from typing import Any

import pytest

from sandbox_science.orchestrator.documents import USER, DocumentError
from sandbox_science.orchestrator.ledger import Ledger

from .test_ledger import DELEGATED, MAIN, NEW, change
from .test_ledger import ledger as ledger

ESTABLISH = {"type": "decision", "status": "in_effect", "title": "Establish", "statement": "s"}


def decide(*targets: str, **more: Any) -> dict[str, Any]:
    """A decision in effect that affects ``targets``."""
    return {**ESTABLISH, "links": [{"kind": "affects", "entry": t} for t in targets], **more}


def status(entry: str, expected: int, value: str) -> dict[str, Any]:
    return {"entry": entry, "expected": expected, "status": value}


def test_a_decision_in_effect_that_affects_an_entry_sets_its_status(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], NEW["claim"])
    made = change(ledger, decide("C-1"), status("C-1", 1, "established"), op="op-2", author=USER)
    assert [(e["handle"], e["status"]) for e in made["entries"]] == [
        ("D-1", "in_effect"),
        ("C-1", "established"),
    ]
    for n, changes in enumerate(
        (
            [status("C-2", 1, "established")],
            [decide("C-1"), status("C-2", 1, "established")],
            [{**decide("C-2"), "status": None}, status("C-2", 1, "established")],
        )
    ):
        with pytest.raises(DocumentError, match="puts in effect a decision that affects it"):
            change(ledger, *changes, op=f"refused-{n}", author=USER)
    # A decision put in effect by an earlier change decides nothing later.
    change(ledger, decide("C-2"), op="earlier", author=USER)
    with pytest.raises(DocumentError, match="puts in effect a decision that affects it"):
        change(ledger, status("C-2", 1, "established"), op="later", author=USER)
    # A proposed decision is put in effect with the change it decides.
    change(ledger, {**decide("C-2"), "status": None}, op="op-3")
    assert ledger.entry("p", "D-3").status == "proposed"
    change(
        ledger, status("D-3", 1, "in_effect"), status("C-2", 1, "refuted"), op="op-4", author=USER
    )
    assert ledger.entry("p", "C-2").status == "refuted"


def test_a_decision_is_reversed_by_a_new_decision_that_states_its_changes(ledger: Ledger) -> None:
    change(ledger, NEW["claim"])
    change(ledger, decide("C-1"), status("C-1", 1, "established"), op="op-2", author=USER)
    change(
        ledger,
        decide("D-1", "C-1", title="Reverse"),
        status("D-1", 1, "reversed"),
        status("C-1", 2, "disputed"),
        op="op-3",
        author=USER,
    )
    assert (ledger.entry("p", "D-1").status, ledger.entry("p", "C-1").status) == (
        "reversed",
        "disputed",
    )
    for n, (entry, expected, value, message) in enumerate(
        (
            ("D-1", 2, "in_effect", "a reversed decision stays reversed"),
            ("D-2", 1, "proposed", "reversed by another decision"),
            ("D-2", 1, "rejected", "reversed by another decision"),
        )
    ):
        with pytest.raises(DocumentError, match=message):
            change(ledger, status(entry, expected, value), op=f"refused-{n}", author=USER)
    # A proposed decision is rejected as it is, and stays rejected.
    change(ledger, {**decide("C-1"), "status": None}, op="op-4")
    change(ledger, status("D-3", 1, "rejected"), op="op-5")
    with pytest.raises(DocumentError, match="a rejected decision stays rejected"):
        change(ledger, status("D-3", 2, "in_effect"), op="op-6")
    change(ledger, {**decide("C-1"), "status": None}, op="op-7")
    with pytest.raises(DocumentError, match="rejected, not reversed"):
        change(ledger, status("D-4", 1, "reversed"), op="op-8")


def test_a_delegated_agents_decision_is_proposed(ledger: Ledger) -> None:
    change(ledger, NEW["claim"])
    with pytest.raises(DocumentError, match="not_permitted_for_role"):
        change(ledger, decide("C-1"), op="in-effect", author=DELEGATED)
    made = change(ledger, {**decide("C-1"), "status": None}, op="op-2", author=DELEGATED)
    assert made["entries"][0]["status"] == "proposed"


def test_only_a_project_claim_or_result_is_established(ledger: Ledger) -> None:
    change(
        ledger,
        {**NEW["reference"], "key": "paper"},
        {
            **NEW["claim"],
            "origin": "published",
            "evidence": [{"role": "supports", "type": "source_claim", "entry": "new:paper"}],
        },
    )
    with pytest.raises(DocumentError, match="only a project claim or result is established"):
        change(ledger, decide("C-1"), status("C-1", 1, "established"), op="op-2", author=USER)
    # The main agent refutes a published result with a decision (settled question 2).
    change(ledger, decide("C-1"), status("C-1", 1, "refuted"), op="op-3", author=MAIN)
    assert ledger.entry("p", "C-1").status == "refuted"


def gate(ledger: Ledger, op: str, expected: int = 1) -> DocumentError:
    """The main agent tries to establish C-1; the gate's refusal."""
    with pytest.raises(DocumentError, match="gate_not_met") as refused:
        change(ledger, decide("C-1"), status("C-1", expected, "established"), op=op)
    assert refused.value.kind == "policy_denied"
    return refused.value


def test_the_gate_names_the_clause_the_main_agents_decision_does_not_meet(
    ledger: Ledger,
) -> None:
    """LG-F3 as far as it can run: each refusal names its clause, a check the main agent
    produced does not count, and the review clause cannot hold yet."""
    documents = ledger.documents
    doc = documents.create("p", name="ff", title="FF", text="# F\n\nProof.\n", author=MAIN)
    block = documents.mapped("p", doc.document_id, 1).block_ids[1]
    change(ledger, NEW["claim"], {**NEW["assumption"], "key": "a"}, author=DELEGATED)
    refused = gate(ledger, "no-evidence")
    assert refused.details == {"entry": ledger.entry("p", "C-1").entry_id, "clause": "evidence"}
    synthetic = {"role": "supports", "type": "synthetic", "entry": "A-1"}
    change(ledger, {"entry": "C-1", "expected": 1, "evidence": [synthetic]}, op="e-1")
    assert gate(ledger, "synthetic", 2).details["clause"] == "evidence"
    proof = {"role": "derivation", "type": "proof", "document": "ff", "revision": 1, "block": block}
    change(ledger, {"entry": "C-1", "expected": 2, "evidence": [proof]}, op="e-2")
    assert gate(ledger, "unchecked", 3).details["clause"] == "independent_check"
    # The main agent's own check does not count: it wrote the block it points to.
    check = {
        "role": "independent_check",
        "type": "proof",
        "document": "ff",
        "revision": 1,
        "block": block,
        "checked": 3,
    }
    change(ledger, {"entry": "C-1", "expected": 3, "evidence": [check]}, op="e-3")
    assert gate(ledger, "own-check", 4).details["clause"] == "independent_check"
    # The user's check counts; the review clause cannot hold yet.
    documents.create("p", name="check", title="Check", text="# C\n\nChecked.\n", author=USER)
    user_block = documents.mapped("p", "check", 1).block_ids[1]
    checked = {**check, "document": "check", "block": user_block, "checked": 4}
    change(ledger, {"entry": "C-1", "expected": 4, "evidence": [checked]}, op="e-4")
    refused = gate(ledger, "unreviewed", 5)
    assert refused.details["clause"] == "review"
    assert "the reviewer does not read ledger entries yet" in refused.message
    # A check of a revision whose statement the change replaces no longer counts.
    with pytest.raises(DocumentError, match="gate_not_met") as stale:
        change(
            ledger,
            decide("C-1"),
            {"entry": "C-1", "expected": 5, "status": "established", "statement": "Changed."},
            op="stale",
        )
    assert stale.value.details["clause"] == "independent_check"
    # The user's decision needs no gate.
    change(ledger, decide("C-1"), status("C-1", 5, "established"), op="user", author=USER)
    assert ledger.entry("p", "C-1").status == "established"


def test_computational_evidence_is_pinned_to_an_artifact(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], NEW["assumption"])
    loose = {"role": "supports", "type": "exact_computation", "entry": "A-1"}
    change(ledger, {"entry": "C-1", "expected": 1, "evidence": [loose]}, op="e-1")
    assert gate(ledger, "loose", 2).details["clause"] == "computation"


def test_an_obstruction_needs_an_assessment_the_main_agent_cannot_give_yet(
    ledger: Ledger,
) -> None:
    change(ledger, NEW["issue"])
    with pytest.raises(DocumentError, match="gate_not_met") as refused:
        change(ledger, decide("I-1"), status("I-1", 1, "obstructed"), op="op-2")
    assert refused.value.details["clause"] == "assessment"
    change(ledger, decide("I-1"), status("I-1", 1, "obstructed"), op="op-3", author=USER)
    assert ledger.entry("p", "I-1").status == "obstructed"
