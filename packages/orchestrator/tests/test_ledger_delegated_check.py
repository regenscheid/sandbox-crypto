# SPDX-License-Identifier: MIT
"""A delegation's check as a proved result's independent check (docs/specs/ledger.md,
LG-8, LG-13, LG-17; plans-and-delegation spec, PD-13, PD-17): the outcome of a check of
the entry at a revision is an evidence target, and the gate counts it only when the
check returned ``confirmed`` and ``complete``, met no plan fence, read the entry at that
revision, and was not run by the run that added the proof."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.ledger.models import GateClause

from .loop_harness import PROJECT, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import RESULT, results, started, wait
from .test_delegation import delegating as delegating
from .test_ledger import MAIN, NEW, change
from .test_ledger import ledger as ledger

ROOT = "20261015T120000000000Z-run-root"
CHECKER = "20261015T120001000000Z-run-checker"
CONFIRMED: dict[str, Any] = {**RESULT, "revision": 2, "obstacle": None}


def proved(ledger: Ledger, author: Author = MAIN) -> str:
    """C-1 at revision 2, which rests on a proof ``author`` added; its entry ID."""
    lemma = {**NEW["claim"], "title": "Lemma", "statement": "A lemma."}
    change(ledger, NEW["claim"], lemma)
    proof = {"role": "derivation", "type": "proof", "entry": "C-2"}
    change(ledger, {"entry": "C-1", "expected": 1, "evidence": [proof]}, op="proof", author=author)
    return ledger.entry("p", "C-1").entry_id


def checked(
    ledger: Ledger,
    entry_id: str,
    *,
    revision: int = 2,
    run: str = CHECKER,
    project: str = "p",
    session: str = "s",
    n: int = 1,
    ended: Any = "returned",
    **outcome: Any,
) -> str:
    """A delegation that checked ``entry_id`` at ``revision`` and ended as given."""
    records = DelegationRecords(ledger.documents.control)
    made = Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": ROOT,
            "run": run,
            "kind": "check",
            "brief": {"task": "Check.", "check": {"entry": entry_id, "revision": revision}},
            "started_at": "2026-10-15T12:00:01+00:00",
        }
    )
    records.start(project, session, made)
    if ended is not None:
        found: dict[str, Any] = {
            "result": CONFIRMED,
            "read_entry": True,
            "alias": "main",
            "models": ("scripted-main",),
            **outcome,
        }
        records.end(project, session, made.delegation_id, Ending(ended, "answered", "{}", **found))
    return made.delegation_id


def link(delegation_id: str, checked: int = 2, **more: Any) -> dict[str, Any]:
    return {
        "role": "independent_check",
        "type": "proof",
        "delegation": delegation_id,
        "checked": checked,
        **more,
    }


def clause(ledger: Ledger) -> GateClause:
    return next(c for c in ledger.gate("p", "C-1").clauses if c.clause == "independent_check")


def test_a_confirmed_check_counts_as_the_independent_check(ledger: Ledger) -> None:
    entry = proved(ledger)
    assert not clause(ledger).holds
    delegation = checked(ledger, entry)
    change(ledger, {"entry": "C-1", "expected": 2, "evidence": [link(delegation)]}, op="check")
    found = clause(ledger)
    assert found.holds
    assert delegation in found.reason
    (stored,) = [e for e in ledger.entry("p", "C-1").evidence if e.target == "delegation"]
    assert (stored.target_id, stored.checked, stored.revision) == (delegation, 2, None)
    assert stored.locator == {
        "run": CHECKER,
        "alias": "main",
        "models": ["scripted-main"],
        "ended": "returned",
        "verdict": "confirmed",
        "status": "complete",
    }


@pytest.mark.parametrize(
    ("make", "item", "error"),
    [
        ({}, {"role": "supports", "checked": None}, "only as an independent_check"),
        ({}, {"checked": 1}, "did not check C-1 at revision 1"),
        ({"revision": 1}, {}, "did not check C-1 at revision 2"),
        ({}, {"revision": 2}, "names only the delegation"),
        ({"ended": None}, {}, "has not ended"),
        ({"project": "q", "session": "t"}, {}, "no delegation"),
    ],
)
def test_a_delegation_link_names_an_ended_check_of_this_entry_and_revision(
    ledger: Ledger, make: dict[str, Any], item: dict[str, Any], error: str
) -> None:
    entry = proved(ledger)
    delegation = checked(ledger, entry, **make)
    with pytest.raises(DocumentError, match=error):
        change(
            ledger,
            {"entry": "C-1", "expected": 2, "evidence": [link(delegation, **item)]},
            op="link",
        )


def test_an_unknown_delegation_is_no_evidence(ledger: Ledger) -> None:
    proved(ledger)
    with pytest.raises(DocumentError, match="no delegation") as refused:
        change(
            ledger,
            {"entry": "C-1", "expected": 2, "evidence": [link("dlg-" + "f" * 24)]},
            op="link",
        )
    assert refused.value.kind == "not_found"


@pytest.mark.parametrize(
    "outcome",
    [
        {"result": {**CONFIRMED, "verdict": "refuted"}},
        {"result": {**CONFIRMED, "status": "partial"}},  # one the schema would refuse
        {"result": None},
        {"refused": "the session's plan is a draft, which allows no effects"},
        {"read_entry": False},
        {"ended": "cancelled"},
        {"ended": "limit_reached"},
    ],
)
def test_a_check_counts_only_when_it_confirmed_a_complete_unfenced_read(
    ledger: Ledger, outcome: dict[str, Any]
) -> None:
    entry = proved(ledger)
    delegation = checked(ledger, entry, **outcome)
    change(ledger, {"entry": "C-1", "expected": 2, "evidence": [link(delegation)]}, op="check")
    assert not clause(ledger).holds


def test_the_run_that_added_the_proof_cannot_check_it(ledger: Ledger) -> None:
    author = Author(kind="agent", session_id="s", run_id="run-checker", role="delegated")
    entry = proved(ledger, author)
    delegation = checked(ledger, entry)
    change(ledger, {"entry": "C-1", "expected": 2, "evidence": [link(delegation)]}, op="check")
    assert not clause(ledger).holds


def test_a_check_no_longer_counts_once_the_statement_changes(ledger: Ledger) -> None:
    entry = proved(ledger)
    delegation = checked(ledger, entry)
    change(ledger, {"entry": "C-1", "expected": 2, "evidence": [link(delegation)]}, op="check")
    assert clause(ledger).holds
    change(ledger, {"entry": "C-1", "expected": 3, "statement": "Changed."}, op="edit")
    assert not clause(ledger).holds


async def test_a_main_run_checks_its_proof_by_delegation(delegating: Harness) -> None:
    """The owner's first milestone: the main agent delegates a check of C-1 at the
    revision that holds its proof; a fresh child reads it, confirms it, and the main
    agent records the outcome as C-1's independent check, which the gate counts."""
    documents = delegating.documents()
    try:
        ledger = Ledger(documents)
        lemma = {**NEW["claim"], "title": "Lemma", "statement": "A lemma."}
        change(ledger, lemma, op="lemma")
        proof = {"role": "derivation", "type": "proof", "entry": "C-2"}
        change(ledger, {"entry": "C-1", "expected": 1, "evidence": [proof]}, op="proof")
    finally:
        documents.close()
    check = call(
        "delegate", task="Check the proof from scratch.", check={"entry": "C-1", "revision": 2}
    )

    def record(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        started_with = [json.loads(str(m["content"])) for m in messages if m["role"] == "tool"]
        delegation_id = str(started_with[0]["delegation_id"])
        return [
            call(
                "ledger_change",
                reason="A fresh-context check confirmed it.",
                changes=[{"entry": "C-1", "expected": 2, "evidence": [link(delegation_id)]}],
            )
        ]

    root = await delegating.run(
        "root",
        [Reply(calls=[check]), wait(), Reply(calls_from=record), Reply("C-1 is checked.")],
        delegated=[
            Reply(calls=[call("ledger_read", entry="C-1", revision=2)]),
            Reply(json.dumps({**RESULT, "revision": 2})),
        ],
    )
    recorded = json.loads(results(root)[2])
    assert "error" not in recorded, recorded
    documents = delegating.documents()
    try:
        found = next(
            c
            for c in Ledger(documents).gate(PROJECT, "C-1").clauses
            if c.clause == "independent_check"
        )
    finally:
        documents.close()
    assert found.holds, found.reason
    assert started(root)[0] in found.reason
