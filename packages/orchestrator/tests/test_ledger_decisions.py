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


def closed(entry: str, expected: int, value: str) -> dict[str, Any]:
    """A decision rejected or reversed, with its reopen condition (CM-10)."""
    return {**status(entry, expected, value), "fields": {"reopen_condition": "none"}}


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
        closed("D-1", 1, "reversed"),
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
    change(ledger, closed("D-3", 1, "rejected"), op="op-5")
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
    """LG-F3 up to the review: each refusal names its clause, and a check the main agent
    produced does not count."""
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
    # A block the user wrote counts only when the user adds the check.
    documents.create("p", name="check", title="Check", text="# C\n\nChecked.\n", author=USER)
    user_block = documents.mapped("p", "check", 1).block_ids[1]
    checked = {**check, "document": "check", "block": user_block, "checked": 4}
    change(ledger, {"entry": "C-1", "expected": 4, "evidence": [checked]}, op="e-4")
    assert gate(ledger, "agent-added", 5).details["clause"] == "independent_check"
    checked = {**checked, "checked": 5}
    change(ledger, {"entry": "C-1", "expected": 5, "evidence": [checked]}, op="e-5", author=USER)
    refused = gate(ledger, "unreviewed", 6)
    assert refused.details["clause"] == "review"
    assert "no completed consistency review has covered C-1" in refused.message
    # A check of a revision whose statement the change replaces no longer counts.
    with pytest.raises(DocumentError, match="gate_not_met") as stale:
        change(
            ledger,
            decide("C-1"),
            {"entry": "C-1", "expected": 6, "status": "established", "statement": "Changed."},
            op="stale",
        )
    assert stale.value.details["clause"] == "independent_check"
    # The user's decision needs no gate.
    change(ledger, decide("C-1"), status("C-1", 6, "established"), op="user", author=USER)
    assert ledger.entry("p", "C-1").status == "established"


def test_computational_evidence_is_pinned_to_an_artifact(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], NEW["assumption"])
    loose = {"role": "supports", "type": "exact_computation", "entry": "A-1"}
    change(ledger, {"entry": "C-1", "expected": 1, "evidence": [loose]}, op="e-1")
    assert gate(ledger, "loose", 2).details["clause"] == "computation"


def record(ledger: Ledger, author: Any, op: str, *items: dict[str, Any]) -> list[str]:
    """Work records ``items``, appended by ``author`` under ``op``."""
    return ledger.change(
        "p", {"operation_id": op, "reason": "Recorded.", "work": list(items)}, author
    ).work


def claimed(ledger: Ledger, issue: str, op: str) -> str:
    """A delegated agent's claimed obstruction on ``issue``, with its evidence."""
    _, claim = record(
        ledger, DELEGATED, op,
        {"key": "t", "entry": issue, "tried": "Index calculus.", "ending": "limit",
         "limit": "ideas"},
        {"entry": issue, "tried": "No small factor base exists.",
         "ending": "obstruction_claimed", "references": [{"work": "new:t"}]},
    )  # fmt: skip
    return claim


def assessed(ledger: Ledger, issue: str, claim: str, op: str, author: Any = USER,
             verdict: str = "accepted") -> str:  # fmt: skip
    (made,) = record(ledger, author, op, {"entry": issue, "tried": "Read the argument.",
                                          "ending": "assessment", "assesses": claim,
                                          "verdict": verdict})  # fmt: skip
    return made


def obstructing(issue: str, assessment: str | None, **more: Any) -> dict[str, Any]:
    """The main agent's decision that makes ``issue`` obstructed, naming ``assessment``,
    the scope it excludes and its evidence (C-1)."""
    fields = {"excluded_scope": "Index calculus over prime fields below 2^80."}
    if assessment is not None:
        fields["assessment"] = assessment
    evidence = [{"role": "supports", "type": "proof", "entry": "C-1"}]
    return decide(issue, fields=fields, evidence=evidence, **more)


def test_an_obstruction_needs_an_assessment_the_user_accepted(ledger: Ledger) -> None:
    """LG-7, LG-F8: the main agent's decision makes an issue obstructed only when it
    names the user's acceptance of an obstruction claimed on it, the excluded scope and
    its evidence; the claimer's or another agent's acceptance counts for nothing yet, and
    a later rejection takes the acceptance back. The user's decision needs none."""
    change(ledger, NEW["issue"], NEW["issue"], NEW["claim"])
    for issue in ("I-1", "I-2"):
        claim = claimed(ledger, issue, f"claim-{issue}")
        for author, op in ((MAIN, "main"), (DELEGATED, "delegated")):
            agents = assessed(ledger, issue, claim, f"{op}-{issue}", author)
            with pytest.raises(DocumentError, match="gate_not_met") as refused:
                change(ledger, obstructing(issue, agents), status(issue, 1, "obstructed"),
                       op=f"obstruct-{op}-{issue}")  # fmt: skip
            assert refused.value.details["clause"] == "assessment"
        user = assessed(ledger, issue, claim, f"user-{issue}")
    for missing in ({"fields": {"assessment": user}}, {"evidence": []}):
        with pytest.raises(DocumentError, match="names the assessment"):
            change(ledger, {**obstructing("I-2", user), **missing},
                   status("I-2", 1, "obstructed"), op=f"bare-{len(missing)}")  # fmt: skip
    first = ledger.work("p", "I-1").records[-2].record_id
    assert ledger.work("p", "I-1").records[0].assesses == first
    change(ledger, obstructing("I-1", ledger.work("p", "I-1").records[0].record_id),
           status("I-1", 1, "obstructed"), op="obstruct-I-1")  # fmt: skip
    assert ledger.entry("p", "I-1").status == "obstructed"
    claim = ledger.work("p", "I-2").records[-2].record_id
    assessed(ledger, "I-2", claim, "rejected", verdict="rejected")
    with pytest.raises(DocumentError, match="latest assessment of the claim"):
        change(ledger, obstructing("I-2", user), status("I-2", 1, "obstructed"), op="o-I-2")
    by_user = decide("I-2", fields={"excluded_scope": "Index calculus below 2^80."})
    change(ledger, by_user, status("I-2", 1, "obstructed"), op="by-user-I-2", author=USER)
    assert ledger.entry("p", "I-2").status == "obstructed"


def test_an_accepted_obstruction_holds_only_for_the_issue_it_was_made_on(
    ledger: Ledger,
) -> None:
    """The review (high, #371): the user accepted "no attack below 2^80 for parameter set
    A"; the main agent reworded the issue to "is there any attack on S?" and made it
    obstructed in one change, and later rewrote the obstructed issue. Both are refused:
    the acceptance holds for the statement and fields it was made on, and a decision that
    keeps an issue obstructed names no scope or assessment (the review of #440, N4)."""
    narrow = {**NEW["issue"], "title": "Below 2^80 for set A?",
              "statement": "Is there an attack below $2^{80}$ for parameter set A?"}  # fmt: skip
    change(ledger, narrow, NEW["claim"])
    claim = claimed(ledger, "I-1", "claim")
    user = assessed(ledger, "I-1", claim, "accepted")
    broad = {"entry": "I-1", "expected": 1, "title": "Any attack on S?",
             "statement": "Is there ANY attack on S?", "status": "obstructed"}  # fmt: skip
    with pytest.raises(DocumentError, match="statement and fields as they were assessed"):
        change(ledger, obstructing("I-1", user), broad, op="broad")
    reworded = {"entry": "I-1", "expected": 1, "statement": "Is there ANY attack on S?"}
    change(ledger, reworded, op="reword")  # the issue is open: anyone rewords it
    with pytest.raises(DocumentError, match="changed since the claim or its acceptance"):
        change(ledger, obstructing("I-1", user), status("I-1", 2, "obstructed"), op="later")
    change(ledger, {"entry": "I-1", "expected": 2, "statement": narrow["statement"]}, op="back")
    with pytest.raises(DocumentError, match="changed since the claim or its acceptance"):
        change(ledger, obstructing("I-1", user), status("I-1", 3, "obstructed"), op="back-o")
    fresh_claim = claimed(ledger, "I-1", "fresh-claim")
    fresh = assessed(ledger, "I-1", fresh_claim, "fresh")
    change(ledger, obstructing("I-1", fresh), status("I-1", 3, "obstructed"), op="fresh-o")
    assert ledger.entry("p", "I-1").status == "obstructed"
    rewrite = {"entry": "I-1", "expected": 4, "statement": "Is there ANY attack on S?"}
    with pytest.raises(DocumentError, match="names an excluded scope or an assessment only"):
        change(ledger, obstructing("I-1", fresh), rewrite, op="rewrite")


def test_an_agents_later_assessments_do_not_cancel_the_users(ledger: Ledger) -> None:
    """The review (low, #371): an agent's later assessment, or 1,024 of them, took the
    user's acceptance out of the window read. Only the user's latest counts."""
    change(ledger, NEW["issue"], NEW["claim"])
    claim = claimed(ledger, "I-1", "claim")
    user = assessed(ledger, "I-1", claim, "user")
    spam = {"entry": "I-1", "tried": "x", "ending": "assessment", "assesses": claim,
            "verdict": "rejected"}  # fmt: skip
    for batch in range(32):
        record(ledger, MAIN, f"spam-{batch}", *[spam] * 32)
    change(ledger, obstructing("I-1", user), status("I-1", 1, "obstructed"), op="o")
    assert ledger.entry("p", "I-1").status == "obstructed"


def test_the_users_later_rejection_flags_the_obstructed_issue(ledger: Ledger) -> None:
    """The review (low, #371): a rejection after the obstruction changed nothing. It
    flags the issue whose obstruction rests on the claim."""
    change(ledger, NEW["issue"], NEW["claim"])
    claim = claimed(ledger, "I-1", "claim")
    user = assessed(ledger, "I-1", claim, "user")
    change(ledger, obstructing("I-1", user), status("I-1", 1, "obstructed"), op="o")
    assert ledger.entry("p", "I-1").flags == []
    assessed(ledger, "I-1", claim, "agent-no", author=MAIN, verdict="rejected")
    assert ledger.entry("p", "I-1").flags == []
    assessed(ledger, "I-1", claim, "user-no", verdict="rejected")
    (flag,) = ledger.entry("p", "I-1").flags
    assert (flag.cause, flag.source["verdict"]) == ("evidence_changed", "rejected")


def test_the_user_assesses_a_claim_at_the_revision_read(ledger: Ledger) -> None:
    """The user's assessment through the ledger (the route's and the CLI's): at the
    revision read, the user's only."""
    change(ledger, NEW["issue"])
    claim = claimed(ledger, "I-1", "claim")
    with pytest.raises(DocumentError, match="not 2"):
        ledger.assess("p", "I-1", claim, "accepted", 2, USER, note="Read it.", operation_id="a")
    with pytest.raises(DocumentError, match="not_permitted_for_role"):
        ledger.assess("p", "I-1", claim, "accepted", 1, MAIN, note="Read it.", operation_id="a")
    made = ledger.assess("p", "I-1", claim, "accepted", 1, USER, note="Read it.", operation_id="a")
    (latest, *_) = ledger.work("p", "I-1").records
    assert (latest.record_id, latest.assesses, latest.verdict, latest.author["kind"]) == (
        made.work[0],
        claim,
        "accepted",
        "user",
    )


# The review of #277.


# The review of the frontier (H1).

PUBLISHED = (
    {**NEW["reference"], "key": "paper"},
    {
        **NEW["result"],
        "origin": "published",
        "evidence": [{"role": "supports", "type": "source_claim", "entry": "new:paper"}],
    },
)
"""REF-1, and R-1, a published result that cites it."""


def test_a_refuted_result_comes_back_only_by_a_decision(ledger: Ledger) -> None:
    """Probe H1: the user refutes R-1, a published result; the main agent recorded it
    again, or disputed it first, and R-1 was back on the frontier with nothing decided."""
    change(ledger, *PUBLISHED)
    change(ledger, decide("R-1"), status("R-1", 1, "refuted"), op="refute", author=USER)
    for value in ("recorded", "disputed"):
        with pytest.raises(DocumentError, match="R-1 leaves refuted only in a change"):
            change(ledger, status("R-1", 2, value), op=f"back-{value}")
        # The main agent's decision passes the gate, whatever status it brings R-1 to.
        with pytest.raises(DocumentError, match="gate_not_met") as refused:
            change(ledger, decide("R-1"), status("R-1", 2, value), op=f"main-{value}")
        assert refused.value.details["clause"] == "review"
    change(ledger, decide("R-1"), status("R-1", 2, "recorded"), op="user", author=USER)
    assert ledger.entry("p", "R-1").status == "recorded"


def test_a_withdrawn_entry_comes_back_only_by_a_decision(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], NEW["issue"])
    change(ledger, status("C-1", 1, "withdrawn"), status("I-1", 1, "withdrawn"), op="withdraw")
    for entry, value in (("C-1", "proposed"), ("I-1", "open")):
        with pytest.raises(DocumentError, match=f"{entry} leaves withdrawn only in a change"):
            change(ledger, status(entry, 2, value), op=f"back-{entry}")
    # The main agent brings an issue back by its decision, and a claim through the gate.
    change(ledger, decide("I-1"), status("I-1", 2, "open"), op="issue")
    assert ledger.entry("p", "I-1").status == "open"
    with pytest.raises(DocumentError, match="gate_not_met") as refused:
        change(ledger, decide("C-1"), status("C-1", 2, "proposed"), op="claim")
    assert refused.value.details["clause"] == "evidence"
    change(ledger, decide("C-1"), status("C-1", 2, "proposed"), op="user", author=USER)
    assert ledger.entry("p", "C-1").status == "proposed"


def test_a_disputed_result_keeps_what_it_was_recorded_on(ledger: Ledger) -> None:
    """Probe H1: the main agent disputes R-1, a recorded result, rewrites its measures
    while it is disputed, and records it again with measures nobody recorded."""
    change(ledger, *PUBLISHED, NEW["result"])
    change(ledger, status("R-1", 1, "disputed"), status("R-2", 1, "disputed"), op="dispute")
    faster = {"fields": {"measures": {"time": 90, "memory": 80}}}
    for rewrite in ({"statement": "Time $2^{90}$."}, faster):
        with pytest.raises(DocumentError, match="R-1 is disputed: its statement, fields"):
            change(ledger, {"entry": "R-1", "expected": 2, **rewrite}, op=f"r-{len(rewrite)}")
    # One never recorded or established is not held to what it was.
    change(ledger, {"entry": "R-2", "expected": 2, **faster}, op="r-2")
    # A decision that affects R-1 may rewrite it, as may a change that leaves disputed.
    change(ledger, decide("R-1"), {"entry": "R-1", "expected": 2, **faster}, op="decided")
    change(ledger, {"entry": "R-1", "expected": 3, "status": "recorded", **faster}, op="back")
    assert ledger.entry("p", "R-1").fields["measures"] == {"time": 90, "memory": 80}


def test_an_established_entry_keeps_what_it_was_established_on(ledger: Ledger) -> None:
    """Probe p3: the user establishes C-1; the main agent then rewrites its statement,
    fields or evidence, and C-1 stayed established."""
    change(ledger, NEW["claim"], NEW["assumption"])
    change(ledger, decide("C-1"), status("C-1", 1, "established"), op="user", author=USER)
    proof = {"role": "supports", "type": "proof", "entry": "A-1"}
    for n, rewrite in enumerate(
        (
            {"statement": "P = NP."},
            {"fields": {"kind": "theorem"}},
            {"evidence": [proof]},
            {"links": [{"kind": "assumes", "entry": "A-1"}]},
        )
    ):
        with pytest.raises(DocumentError, match="C-1 is established: its statement"):
            change(ledger, {"entry": "C-1", "expected": 2, **rewrite}, op=f"rewrite-{n}")
    # Its title, and links other than assumptions, are not what it was established on.
    change(ledger, {"entry": "C-1", "expected": 2, "title": "Fermat, established"}, op="title")
    # A change that also leaves the status may rewrite it.
    change(
        ledger,
        {"entry": "C-1", "expected": 3, "status": "disputed", "statement": "P = NP?"},
        op="dispute",
    )
    # A new decision of the user's re-decides it as it rewrites it; the main agent's
    # goes through the gate.
    change(ledger, decide("C-1"), status("C-1", 4, "established"), op="again", author=USER)
    with pytest.raises(DocumentError, match="gate_not_met"):
        change(ledger, decide("C-1"), {"entry": "C-1", "expected": 5, "evidence": [proof]}, op="m")
    change(
        ledger,
        decide("C-1"),
        {"entry": "C-1", "expected": 5, "evidence": [proof]},
        op="user-rewrite",
        author=USER,
    )
    view = ledger.entry("p", "C-1")
    assert (view.status, view.revision, len(view.evidence)) == ("established", 6, 1)


def test_any_proof_that_counts_needs_an_independent_check(ledger: Ledger) -> None:
    """Probe p1 in part: supports/proof needs a check as derivation/proof does."""
    change(ledger, NEW["claim"], NEW["assumption"])
    proof = {"role": "supports", "type": "proof", "entry": "A-1"}
    change(ledger, {"entry": "C-1", "expected": 1, "evidence": [proof]}, op="e-1")
    assert gate(ledger, "unchecked", 2).details["clause"] == "independent_check"


def test_a_review_or_a_set_aside_entry_is_not_evidence_that_counts(ledger: Ledger) -> None:
    from .test_ledger_evidence import review

    reviewed = review(ledger)
    change(ledger, NEW["claim"], NEW["claim"])
    by_review = {"role": "supports", "type": "finite_test", "review": reviewed}
    change(ledger, {"entry": "C-1", "expected": 1, "evidence": [by_review]}, op="e-1")
    assert gate(ledger, "review", 2).details["clause"] == "evidence"
    by_claim = {"role": "supports", "type": "source_claim", "entry": "C-2"}
    with pytest.raises(DocumentError, match="with a reference"):
        change(ledger, {"entry": "C-1", "expected": 2, "evidence": [by_claim]}, op="bad")
    claim = {"role": "supports", "type": "proof", "entry": "C-2"}
    change(ledger, {"entry": "C-1", "expected": 2, "evidence": [claim]}, op="e-2")
    assert gate(ledger, "claim", 3).details["clause"] == "independent_check"
    change(ledger, {"entry": "C-2", "expected": 1, "status": "withdrawn"}, op="withdrawn")
    assert gate(ledger, "set-aside", 3).details["clause"] == "evidence"


def test_a_check_counts_only_when_the_user_added_it_at_the_revision_they_checked(
    ledger: Ledger,
) -> None:
    """Probes p4, p9 and p10 of the reviews of #277: the user's task "Buy milk.", the
    user's issue that doubts a step and links to the claim, and an agent's entry the
    user only retitled were each cited as the independent check. A check counts only
    when the user added it themselves, at the revision they checked."""
    change(ledger, NEW["claim"], NEW["assumption"])
    proof = {"role": "derivation", "type": "proof", "entry": "A-1"}
    change(ledger, {"entry": "C-1", "expected": 1, "evidence": [proof]}, op="derive")
    doubt = {
        "type": "issue",
        "title": "Step 3",
        "statement": "I doubt step 3.",
        "links": [{"kind": "concerns", "entry": "C-1", "revision": 2}],
    }
    change(
        ledger, {"type": "task", "title": "Lunch", "statement": "Buy milk."}, doubt,
        op="user-entries", author=USER,
    )  # fmt: skip
    verified = {
        "type": "issue",
        "title": "Verified",
        "statement": "Every step of C-1 r2 verified.",
        "links": [{"kind": "concerns", "entry": "C-1", "revision": 2}],
    }
    change(ledger, verified, op="agent-verified")
    change(ledger, {"entry": "I-2", "expected": 1, "title": "Verified (user)"}, op="retitled",
           author=USER)  # fmt: skip
    for n, target in enumerate(("T-1", "I-1", "I-2")):
        cited = {"role": "independent_check", "type": "proof", "entry": target, "checked": 2}
        change(ledger, {"entry": "C-1", "expected": 2 + n, "evidence": [cited]}, op=f"c{n}")
        assert gate(ledger, f"g{n}", 3 + n).details["clause"] == "independent_check"
    # A check the user adds at a later revision than the one it names does not count.
    late = {
        "role": "independent_check",
        "type": "proof",
        "entry": "C-1",
        "revision": 2,
        "checked": 2,
    }
    change(ledger, {"entry": "C-1", "expected": 5, "evidence": [late]}, op="late", author=USER)
    assert gate(ledger, "late-gate", 6).details["clause"] == "independent_check"
    # The user's own check of the entry as it reads counts.
    checked = ledger.check("p", "C-1", 6, USER, operation_id="user-check", note="Steps 1-4.")
    assert checked.entries[0].revision == 7
    (record,) = [e for e in ledger.entry("p", "C-1").evidence if e.added == 7]
    assert (record.target_id, record.revision, record.checked, record.scope) == (
        ledger.entry("p", "C-1").entry_id,
        6,
        6,
        "Steps 1-4.",
    )
    assert gate(ledger, "checked", 7).details["clause"] == "review"


def test_only_the_user_records_a_check_of_the_entry_as_it_reads(ledger: Ledger) -> None:
    change(ledger, NEW["claim"])
    with pytest.raises(DocumentError, match="not_permitted_for_role"):
        ledger.check("p", "C-1", 1, MAIN, operation_id="agent")
    with pytest.raises(DocumentError, match="at revision 1, not 2") as stale:
        ledger.check("p", "C-1", 2, USER, operation_id="stale")
    assert stale.value.kind == "conflict"
    ledger.check("p", "C-1", 1, USER, operation_id="first")
    assert ledger.check("p", "C-1", 1, USER, operation_id="first").replayed


def test_contradicting_evidence_added_after_a_check_is_not_what_it_covered(
    ledger: Ledger,
) -> None:
    """Probe p12: a delegated agent adds a contradicting source after the check; the
    main agent established on the old check. Evidence of either sign is material."""
    change(ledger, NEW["claim"], NEW["assumption"], NEW["reference"])
    proof = {"role": "derivation", "type": "proof", "entry": "A-1"}
    change(ledger, {"entry": "C-1", "expected": 1, "evidence": [proof]}, op="derive")
    ledger.check("p", "C-1", 2, USER, operation_id="check")
    assert gate(ledger, "checked", 3).details["clause"] == "review"
    against = {"role": "contradicts", "type": "source_claim", "entry": "REF-1"}
    change(ledger, {"entry": "C-1", "expected": 3, "evidence": [against]}, op="against",
           author=DELEGATED)  # fmt: skip
    assert gate(ledger, "contradicted", 4).details["clause"] == "independent_check"
