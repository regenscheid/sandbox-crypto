# SPDX-License-Identifier: MIT
"""The reviewer reads the ledger (docs/specs/consistency-review.md, CR-2, CR-9, CR-10):
each revision an agent of the session makes is an observation of its reviews, with the
entry as it reads at that revision; its text and new claims count toward the triggers;
the user's revisions and other sessions' are not the session's to review."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.ledger import ingest_ledger
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, Author
from sandbox_science.orchestrator.ledger import Ledger

from .test_consistency import MAIN, anchor, dispatch, feed
from .test_consistency import store as store

OTHER = Author(kind="agent", session_id="other", run_id="other-1", role="main")
CLAIM = {"type": "claim", "origin": "project", "title": "Fermat", "statement": "$a^{16}=1$."}


def change(reviews: ReviewStore, *changes: dict[str, Any], op: str, author: Author = MAIN) -> None:
    Ledger(reviews.documents).change(
        "p", {"operation_id": op, "reason": "Recorded.", "changes": list(changes)}, author
    )


def observations(reviews: ReviewStore) -> list[tuple[str, str, int, int, int]]:
    with reviews.documents.control.read() as tx:
        rows = tx.sql.execute(
            "SELECT source_key,kind,characters,structural,effects FROM review_feed "
            "WHERE kind='ledger' ORDER BY seq"
        ).fetchall()
    return [tuple(row) for row in rows]


def test_an_agents_ledger_revisions_are_observed_for_its_sessions_reviews(store) -> None:
    reviews, _, _ = store
    change(reviews, CLAIM, {"type": "task", "title": "Run", "statement": "Run it."}, op="op-1")
    change(reviews, {"entry": "C-1", "expected": 1, "title": "Fermat for 17"}, op="op-2")
    change(reviews, {"entry": "C-1", "expected": 2, "status": "disputed"}, op="op-3")
    change(reviews, {"entry": "T-1", "expected": 1, "title": "Run now"}, op="op-4", author=USER)
    change(reviews, {**CLAIM, "title": "Other"}, op="op-5", author=OTHER)
    assert ingest_ledger(reviews, "p", "s")["complete"]
    entry = Ledger(reviews.documents).entry("p", "C-1").entry_id
    task = Ledger(reviews.documents).entry("p", "T-1").entry_id
    assert observations(reviews) == [
        (f"ledger:{entry}:1", "ledger", len("Fermat") + len("$a^{16}=1$."), 1, 1),
        (f"ledger:{task}:1", "ledger", len("Run") + len("Run it."), 0, 1),
        (f"ledger:{entry}:2", "ledger", len("Fermat for 17"), 0, 1),
        (f"ledger:{entry}:3", "ledger", 0, 0, 1),
    ]
    # The other session's reviews see its own.
    assert ingest_ledger(reviews, "p", "other")["complete"]
    assert len(observations(reviews)) == 5
    # Reading again observes nothing new.
    assert ingest_ledger(reviews, "p", "s")["complete"]
    assert len(observations(reviews)) == 5


def test_the_reviewer_reads_the_entry_as_it_was_at_the_revision(store) -> None:
    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    ingest_ledger(reviews, "p", "s")
    review = dispatch(reviews)
    pinned = anchor(reviews, review, "ledger")
    change(reviews, {"entry": "C-1", "expected": 1, "statement": "Changed."}, op="op-2")
    read = reviews.read_evidence("p", "s", review, pinned["evidence"], owner="owner")
    entry = json.loads(read["text"])
    assert (entry["handle"], entry["revision"], entry["statement"]) == ("C-1", 1, "$a^{16}=1$.")
    assert entry["author"]["run_id"] == "author-1"
    item = reviews.evidence("p", "s", review, pinned["evidence"])
    assert item["locator"] == {
        "entry": entry["entry_id"],
        "handle": "C-1",
        "revision": 1,
        "run": "author-1",
    }
    assert reviews.run_effects("p", "s", "author-1")


def test_ledger_text_counts_toward_the_text_trigger(store) -> None:
    reviews, _, now = store
    change(reviews, {**CLAIM, "statement": "x" * 1990}, op="op-1")
    ingest_ledger(reviews, "p", "s")
    assert reviews.due("p", "s")  # a new claim is a structural block
    review = dispatch(reviews)
    reviews.finish("p", "s", review, "owner", "passed", "Checked.")
    now[0] += 1000
    change(reviews, {"type": "task", "title": "T", "statement": "y" * 1000}, op="op-2")
    ingest_ledger(reviews, "p", "s")
    assert not reviews.due("p", "s")
    change(reviews, {"entry": "T-1", "expected": 1, "statement": "z" * 1200}, op="op-3")
    ingest_ledger(reviews, "p", "s")
    assert reviews.due("p", "s")


def test_ledger_pages_and_their_cursor_commit_together(store, monkeypatch) -> None:
    reviews, _, _ = store
    for batch in range(8):
        change(reviews, *([{"type": "task", "title": "T", "statement": "s"}] * 30), op=f"b{batch}")
    first = ingest_ledger(reviews, "p", "s")
    assert (first["complete"], len(observations(reviews))) == (False, 200)
    assert ingest_ledger(reviews, "p", "s", drain=True)["complete"]
    assert len(observations(reviews)) == 240
    assert len(set(feed(reviews))) == len(feed(reviews))


def test_the_runs_review_controller_reads_the_ledger_at_its_safe_points(store) -> None:
    import asyncio

    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.consistency.controller import ReviewController
    from sandbox_science.orchestrator.projects import ProjectStore

    reviews, documents, _ = store
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=ProjectStore(documents.path.parent).get("p").default_environment_id,
    )
    controller = ReviewController(reviews, context, run_id="author-1", model=None, alias=None)
    change(reviews, CLAIM, op="op-1")
    asyncio.run(controller.observe([]))
    assert [row[0].split(":")[0] for row in observations(reviews)] == ["ledger"]
    assert controller.ledger_complete


def test_an_entry_too_large_for_an_item_keeps_its_parts_shortened_and_incomplete(store) -> None:
    """The review of #279: the observation shortened notes and scopes instead of
    dropping the fields, links and evidence, and says it is incomplete."""
    reviews, _, _ = store
    change(reviews, *([{"type": "task", "title": "T", "statement": "s"}] * 32), op="tasks")
    change(reviews, *([{"type": "task", "title": "U", "statement": "s"}] * 32), op="more")
    links = [
        {"kind": "concerns", "entry": f"T-{n}", "note": "\U0001d53d" * 4096} for n in range(1, 65)
    ]
    change(reviews, {"type": "issue", "title": "I", "statement": "s", "links": links}, op="i")
    ingest_ledger(reviews, "p", "s", drain=True)
    with reviews.documents.control.read() as tx:
        payload, complete = tx.sql.execute(
            "SELECT payload,complete FROM review_feed WHERE kind='ledger' "
            "AND json_extract(locator,'$.handle')='I-1'"
        ).fetchone()
    entry = json.loads(payload)
    assert (complete, entry["shortened"], len(entry["links"])) == (0, True, 64)
    assert all(len(link["note"]) <= 201 for link in entry["links"])


# Findings on entries (LG-25, CR-17, CR-28 as amended).


def latest(reviews: ReviewStore, review: str, handle: str = "C-1") -> dict[str, Any]:
    """An anchor on the latest revision of ``handle`` the review's snapshot holds."""
    items = [
        item
        for item in reviews.manifest("p", "s", review, limit=20)["evidence"]
        if item["kind"] == "ledger" and item["locator"]["handle"] == handle
    ]
    return {"evidence": max(items, key=lambda item: item["locator"]["revision"])["evidence"]}


def finding_on(reviews: ReviewStore, field: str | None) -> tuple[str, str]:
    """A fail finding on C-1 as the review saw it last, at ``field``; the review and the
    finding."""
    from .test_consistency import raise_finding

    ingest_ledger(reviews, "p", "s")
    review = dispatch(reviews)
    pinned = latest(reviews, review)
    found = raise_finding(reviews, review, anchors=[{**pinned, "field": field}])
    reviews.finish("p", "s", review, "owner", "findings", "One finding.")
    return review, found["finding_id"]


def test_a_finding_anchors_to_an_entrys_field_and_the_entry_shows_it(store) -> None:
    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    _, finding = finding_on(reviews, "statement")
    view = Ledger(reviews.documents).entry("p", "C-1")
    assert [(f.finding_id, f.field, f.revision, f.status, f.severity) for f in view.findings] == [
        (finding, "statement", 1, "open", "fail")
    ]
    # A field anchor names a ledger entry revision only.
    from sandbox_science.orchestrator.documents import DocumentError

    from .test_consistency import claim, raise_finding

    claim(reviews, "Another claim.", "m2")
    review = dispatch(reviews)
    message = anchor(reviews, review, "transcript")
    with pytest.raises(DocumentError, match="field anchor needs a ledger entry"):
        raise_finding(
            reviews, review, anchors=[{**message, "field": "statement"}], operation_id="f2"
        )


def superseded(reviews: ReviewStore, finding: str) -> bool:
    from sandbox_science.orchestrator.consistency.findings import Findings

    Findings(reviews).supersede("p", "s")
    return Findings(reviews).get("p", "s", finding)["status"] == "superseded"


def test_a_field_anchor_is_replaced_only_when_that_field_changes(store) -> None:
    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    _, finding = finding_on(reviews, "statement")
    change(reviews, {"entry": "C-1", "expected": 1, "title": "Renamed"}, op="op-2")
    assert not superseded(reviews, finding)
    change(
        reviews, {"entry": "C-1", "expected": 2, "statement": "$a^{16} = 1$ in GF(17)."}, op="op-3"
    )
    assert superseded(reviews, finding)
    from sandbox_science.orchestrator.consistency.findings import Findings

    last = Findings(reviews).get("p", "s", finding)["history"][-1]
    entry = Ledger(reviews.documents).entry("p", "C-1").entry_id
    assert last["payload"]["ledger_replacements"] == [
        {"entry": entry, "handle": "C-1", "revision": 3, "field": "statement"}
    ]


def test_an_anchor_is_replaced_only_by_a_revision_that_changes_the_material(store) -> None:
    """Probe p2 of the review of #280: a whole-entry fail finding was superseded by a
    title-only revision. Only the statement, fields, assumptions and evidence that can
    count are what a finding, a check or a review is about (LG-17)."""
    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    _, finding = finding_on(reviews, None)
    assert not superseded(reviews, finding)
    change(reviews, {"entry": "C-1", "expected": 1, "status": "disputed"}, op="op-2")
    change(reviews, {"entry": "C-1", "expected": 2, "title": "Renamed"}, op="op-3")
    assert not superseded(reviews, finding)
    change(reviews, {"entry": "C-1", "expected": 3, "statement": "$a^{16} = 1$ in GF(17)."}, op="s")
    assert superseded(reviews, finding)


def test_a_field_anchor_off_the_material_is_not_replaced(store) -> None:
    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    _, finding = finding_on(reviews, "title")
    change(reviews, {"entry": "C-1", "expected": 1, "title": "Renamed"}, op="op-2")
    assert not superseded(reviews, finding)


def test_a_version_22_stores_findings_are_found_by_their_entries(store, tmp_path) -> None:
    """Version 23 keeps the entries each finding is anchored to; it fills them from the
    findings an older store holds."""
    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.documents import DocumentStore, store_path

    from .control_versions import older_store

    reviews, documents, _ = store
    change(reviews, CLAIM, op="op-1")
    _, finding = finding_on(reviews, "statement")
    documents.close()
    old = tmp_path / "old"
    old.mkdir()
    older_store(store_path(tmp_path), store_path(old), migrations.BASELINE + 20)
    migrated = DocumentStore(store_path(old))
    migrated.initialize()
    try:
        view = Ledger(migrated).entry("p", "C-1")
        assert [(f.finding_id, f.field) for f in view.findings] == [(finding, "statement")]
    finally:
        migrated.close()


# The gate's review clause (LG-17).


def establish(reviews: ReviewStore, op: str, expected: int) -> None:
    decision = {
        "type": "decision",
        "status": "in_effect",
        "title": "Establish",
        "statement": "s",
        "links": [{"kind": "affects", "entry": "C-1"}],
    }
    change(
        reviews, decision, {"entry": "C-1", "expected": expected, "status": "established"}, op=op
    )


def clause(reviews: ReviewStore, op: str, expected: int) -> str:
    from sandbox_science.orchestrator.documents import DocumentError

    with pytest.raises(DocumentError, match="gate_not_met") as refused:
        establish(reviews, op, expected)
    return str(refused.value.details["clause"])


def review_passes(reviews: ReviewStore) -> None:
    ingest_ledger(reviews, "p", "s")
    review = dispatch(reviews)
    reviews.finish("p", "s", review, "owner", "passed", "Checked.")


def supported(reviews: ReviewStore) -> None:
    """C-1 at revision 2 with evidence that counts and needs no check: what a source
    states, cited from a reference."""
    reference = {
        "type": "reference",
        "title": "A paper",
        "statement": "A. Author, *A paper*, ePrint 2024/123.",
        "fields": {"identifiers": {"eprint": "2024/123"}, "published": "2024-02-01"},
    }
    change(reviews, CLAIM, reference, op="made")
    evidence = [{"role": "supports", "type": "source_claim", "entry": "REF-1"}]
    change(reviews, {"entry": "C-1", "expected": 1, "evidence": evidence}, op="supported")


def test_the_main_agent_establishes_once_a_completed_review_covered_the_entry(store) -> None:
    reviews, _, now = store
    supported(reviews)
    assert clause(reviews, "unreviewed", 2) == "review"
    ingest_ledger(reviews, "p", "s")
    review = dispatch(reviews)
    assert clause(reviews, "running", 2) == "review"
    reviews.finish("p", "s", review, "owner", "passed", "Checked.")
    # A later statement is not what the review covered.
    change(reviews, {"entry": "C-1", "expected": 2, "statement": "$a^{16} = 1$, p = 17."}, op="s")
    assert clause(reviews, "changed", 3) == "review"
    now[0] += 1000
    review_passes(reviews)
    establish(reviews, "covered", 3)
    assert Ledger(reviews.documents).entry("p", "C-1").status == "established"


def test_an_unresolved_fail_finding_on_the_entry_holds_the_gate(store) -> None:
    from sandbox_science.orchestrator.consistency.findings import Findings

    reviews, _, _ = store
    supported(reviews)
    _, finding = finding_on(reviews, "statement")
    assert clause(reviews, "finding", 2) == "finding"
    revision = Findings(reviews).get("p", "s", finding)["revision"]
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding,
            "expected": revision,
            "action": "dismiss",
            "explanation": "The statement is right.",
            "operation_id": "dismiss",
        },
        USER,
    )
    establish(reviews, "withdrawn", 2)
    assert Ledger(reviews.documents).entry("p", "C-1").status == "established"


def test_a_failed_review_covers_nothing(store) -> None:
    reviews, _, _ = store
    supported(reviews)
    ingest_ledger(reviews, "p", "s")
    review = dispatch(reviews)
    reviews.finish("p", "s", review, "owner", "failed", "The reviewer failed.")
    assert clause(reviews, "failed", 2) == "review"


# The review of #276-#281: each probe must now fail to make an agent's work look
# verified.

DELEGATED = Author(kind="agent", session_id="s", run_id="d-1", role="delegated")


def with_decision(reviews: ReviewStore, op: str, entry_change: dict[str, Any]) -> None:
    decision = {
        "type": "decision",
        "status": "in_effect",
        "title": "Establish",
        "statement": "s",
        "links": [{"kind": "affects", "entry": "C-1"}],
    }
    change(reviews, decision, entry_change, op=op)


def refused_clause(reviews: ReviewStore, op: str, entry_change: dict[str, Any]) -> str:
    from sandbox_science.orchestrator.documents import DocumentError

    with pytest.raises(DocumentError, match="gate_not_met") as refused:
        with_decision(reviews, op, entry_change)
    return str(refused.value.details["clause"])


def test_p1_evidence_added_with_the_decision_is_not_what_the_review_covered(store) -> None:
    reviews, _, _ = store
    reference = {
        "type": "reference",
        "title": "A paper",
        "statement": "A. Author, *A paper*.",
        "fields": {"identifiers": {"eprint": "2024/123"}, "published": "2024-02-01"},
    }
    change(reviews, CLAIM, {"type": "assumption", "title": "a", "statement": "a"}, reference,
           op="made")  # fmt: skip
    review_passes(reviews)
    proof = {"role": "supports", "type": "proof", "entry": "A-1"}
    established = {"entry": "C-1", "expected": 1, "status": "established"}
    assert refused_clause(reviews, "proof", {**established, "evidence": [proof]}) == (
        "independent_check"
    )
    cited = {"role": "supports", "type": "source_claim", "entry": "REF-1"}
    assert refused_clause(reviews, "cited", {**established, "evidence": [cited]}) == "review"
    assert Ledger(reviews.documents).entry("p", "C-1").status == "proposed"


def test_p2_a_title_edit_does_not_dissolve_a_fail_finding(store) -> None:
    reviews, _, _ = store
    supported(reviews)
    _, finding = finding_on(reviews, None)
    change(reviews, {"entry": "C-1", "expected": 2, "title": "Renamed"}, op="retitle")
    assert not superseded(reviews, finding)
    assert clause(reviews, "est", 3) == "finding"


def test_p2b_a_superseded_fail_finding_holds_until_a_later_review_covered_the_entry(
    store,
) -> None:
    reviews, _, now = store
    supported(reviews)
    _, finding = finding_on(reviews, "evidence")
    second = {
        "type": "reference",
        "title": "Another paper",
        "statement": "B. Author, *Another paper*.",
        "fields": {"identifiers": {"eprint": "2024/456"}, "published": "2024-03-01"},
    }
    change(reviews, second, op="b", author=DELEGATED)
    more = {"role": "supports", "type": "source_claim", "entry": "REF-2", "scope": "Lemma 2."}
    change(reviews, {"entry": "C-1", "expected": 2, "evidence": [more]}, op="more",
           author=DELEGATED)  # fmt: skip
    # A review covers the new revision before the finding is superseded: it does not
    # resolve the finding, since it started before the supersession.
    now[0] += 1000
    review_passes(reviews)
    now[0] += 1000
    assert superseded(reviews, finding)
    assert clause(reviews, "est", 3) == "finding"
    # A review that started after the supersession, and covered C-1 as it is, does.
    change(reviews, {"entry": "C-1", "expected": 3, "title": "Fermat for 17"}, op="t")
    now[0] += 1000
    review_passes(reviews)
    establish(reviews, "est-2", 4)
    assert Ledger(reviews.documents).entry("p", "C-1").status == "established"


def test_p5_a_review_with_partial_coverage_or_a_shortened_entry_covers_nothing(store) -> None:
    reviews, _, now = store
    supported(reviews)
    change(reviews, *([CLAIM] * 32), op="c1")
    change(reviews, *([CLAIM] * 32), op="c2")
    links = [
        {"kind": "builds_on", "entry": f"C-{n}", "note": "\U0001d53d" * 4096} for n in range(2, 66)
    ]
    change(reviews, {"entry": "C-1", "expected": 2, "title": "Built", "links": links}, op="l")
    ingest_ledger(reviews, "p", "s", drain=True)
    review = dispatch(reviews)
    from .test_consistency import raise_finding

    raise_finding(reviews, review, severity="note", anchors=[anchor(reviews, review, "ledger")])
    reviews.finish("p", "s", review, "owner", "findings", "One note.")
    assert clause(reviews, "shortened", 3) == "review"
    # Nor does a review whose coverage was incomplete, though its items are whole.
    change(reviews, {"entry": "C-1", "expected": 3, "links": []}, op="unlinked")
    ingest_ledger(reviews, "p", "s", drain=True)
    now[0] += 1000
    partial = reviews.dispatch(
        "p", "s", "checkpoint", author_run="author-1", owner="owner", alias="reviewer",
        coverage_complete=False,
    )  # fmt: skip
    assert partial is not None
    reviews.finish("p", "s", partial, "owner", "findings", "Partial.")
    assert clause(reviews, "partial", 4) == "review"


def test_changing_an_evidence_links_role_is_not_what_the_review_covered(store) -> None:
    """A review covered C-1 citing its reference as contradicting it; the agent then
    makes the same citation support it. The material is the counting links with their
    roles, so the review no longer covers C-1."""
    reviews, _, _ = store
    reference = {
        "type": "reference",
        "title": "A paper",
        "statement": "A. Author, *A paper*.",
        "fields": {"identifiers": {"eprint": "2024/123"}, "published": "2024-02-01"},
    }
    change(reviews, CLAIM, reference, op="made")
    against = {"role": "contradicts", "type": "source_claim", "entry": "REF-1"}
    change(reviews, {"entry": "C-1", "expected": 1, "evidence": [against]}, op="against")
    review_passes(reviews)
    (held,) = Ledger(reviews.documents).entry("p", "C-1").evidence
    turned = {
        "entry": "C-1",
        "expected": 2,
        "evidence": [{**against, "role": "supports"}],
        "evidence_removed": [held.evidence_id],
    }
    change(reviews, turned, op="turned")
    assert clause(reviews, "turned-gate", 3) == "review"


# The verification of #281: a confirmed fix or an accepted rebuttal holds only for the
# material it was decided for.

ANOTHER = {
    "type": "reference",
    "title": "Another paper",
    "statement": "B. Author, *Another paper*.",
    "fields": {"identifiers": {"eprint": "2024/456"}, "published": "2024-03-01"},
}


def cite(reviews: ReviewStore, op: str, expected: int, cited: str, dropped: str) -> None:
    """C-1 cites ``cited`` in place of ``dropped``."""
    (held,) = [
        link
        for link in Ledger(reviews.documents).entry("p", "C-1").evidence
        if link.handle == dropped
    ]
    evidence = [{"role": "supports", "type": "source_claim", "entry": cited}]
    change(reviews, {"entry": "C-1", "expected": expected, "evidence": evidence,
                     "evidence_removed": [held.evidence_id]}, op=op)  # fmt: skip


def decided(reviews: ReviewStore, finding: str, action: str, decision: str) -> None:
    """The main agent's ``action`` on ``finding``, citing C-1 as a review since saw it,
    and the reviewer's ``decision`` on it: a confirmation review decides a claimed fix,
    and the next review a rebuttal."""
    from sandbox_science.orchestrator.consistency.findings import Findings

    from .test_consistency import read_confirmation

    ingest_ledger(reviews, "p", "s")
    since = dispatch(reviews)
    reviews.finish("p", "s", since, "owner", "passed", "Checked.")
    findings = Findings(reviews)
    revision = findings.get("p", "s", finding)["revision"]
    findings.respond(
        "p",
        "s",
        {
            "finding": finding,
            "expected": revision,
            "action": action,
            "explanation": "C-1 cites REF-2, which states it.",
            "evidence": [latest(reviews, since)],
            "operation_id": action,
        },
        MAIN,
    )
    confirmation = dispatch(reviews, "confirmation" if action == "fix" else "terminal")
    read_confirmation(reviews, confirmation)
    findings.confirm(
        "p",
        "s",
        confirmation,
        "owner",
        {
            "finding": finding,
            "expected": revision + 1,
            "decision": decision,
            "explanation": "REF-2 states it.",
            "anchors": [anchor(reviews, confirmation, "ledger")],
            "operation_id": decision,
        },
    )
    reviews.finish("p", "s", confirmation, "owner", "passed", "Decided.")


def test_p8_a_fix_confirmed_for_other_material_does_not_hold_when_the_entry_returns(
    store,
) -> None:
    """Probe p8: R1 fails C-1 r2 on its evidence; the agent cites REF-2 instead (r3),
    claims a fix, and a confirmation review confirms it; the agent then cites REF-1
    again (r4, r2's material). R1 raised a fail on that material, so it does not cover
    it; and the fix was confirmed for r3's material, so a review since does not resolve
    the finding for r4's."""
    reviews, _, now = store
    supported(reviews)
    _, finding = finding_on(reviews, "evidence")
    change(reviews, ANOTHER, op="another")
    cite(reviews, "swap", 2, "REF-2", "REF-1")
    decided(reviews, finding, "fix", "confirm")
    from sandbox_science.orchestrator.consistency.findings import Findings

    assert Findings(reviews).get("p", "s", finding)["status"] == "confirmed fixed"
    now[0] += 1000
    cite(reviews, "back", 3, "REF-1", "REF-2")
    assert clause(reviews, "back-gate", 4) == "review"
    review_passes(reviews)
    assert clause(reviews, "reviewed-gate", 4) == "finding"
    assert Ledger(reviews.documents).entry("p", "C-1").status == "proposed"


def test_p8b_a_rebuttal_accepted_for_other_material_does_not_hold_when_the_entry_returns(
    store,
) -> None:
    reviews, _, now = store
    supported(reviews)
    _, finding = finding_on(reviews, "evidence")
    change(reviews, ANOTHER, op="another")
    cite(reviews, "swap", 2, "REF-2", "REF-1")
    decided(reviews, finding, "rebut", "accept_rebuttal")
    from sandbox_science.orchestrator.consistency.findings import Findings

    assert Findings(reviews).get("p", "s", finding)["status"] == "withdrawn"
    now[0] += 1000
    cite(reviews, "back", 3, "REF-1", "REF-2")
    assert clause(reviews, "back-gate", 4) == "finding"
    review_passes(reviews)
    assert clause(reviews, "reviewed-gate", 4) == "finding"


def test_the_review_that_raised_a_fail_does_not_cover_the_material_it_failed(store) -> None:
    """The user confirms a fix of R1's fail finding made outside C-1, which keeps its
    material: R1 does not cover it, a review since does, and the confirmation holds for
    the material it was decided for."""
    from sandbox_science.orchestrator.consistency.findings import Findings

    reviews, _, now = store
    supported(reviews)
    _, finding = finding_on(reviews, "evidence")
    revision = Findings(reviews).get("p", "s", finding)["revision"]
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding,
            "expected": revision,
            "action": "confirm",
            "explanation": "The paper's erratum fixed it.",
            "operation_id": "confirm",
        },
        USER,
    )
    assert clause(reviews, "raised", 2) == "review"
    change(reviews, {"entry": "C-1", "expected": 2, "title": "Fermat for 17"}, op="t")
    now[0] += 1000
    review_passes(reviews)
    establish(reviews, "covered", 3)
    assert Ledger(reviews.documents).entry("p", "C-1").status == "established"


def test_a_superseded_fail_finding_holds_when_the_entry_returns_to_its_material(store) -> None:
    """A fail finding of any status holds the material it was raised on: superseded
    when C-1 left it, it holds again when C-1 returns, though a review since covered
    C-1 as it reads."""
    reviews, _, now = store
    supported(reviews)
    _, finding = finding_on(reviews, "evidence")
    change(reviews, ANOTHER, op="another")
    cite(reviews, "swap", 2, "REF-2", "REF-1")
    assert superseded(reviews, finding)
    cite(reviews, "back", 3, "REF-1", "REF-2")
    now[0] += 1000
    review_passes(reviews)
    assert clause(reviews, "back-gate", 4) == "finding"


# A fix of a ledger finding cites the changed entry revision (CR-22, CR-24).


def fix(finding: str, expected: int, *cited: list[Any], op: str = "fix") -> dict[str, Any]:
    return {
        "finding": finding,
        "expected": expected,
        "action": "fix",
        "explanation": "The statement now says what the check shows.",
        "entry_revisions": list(cited),
        "operation_id": op,
    }


def test_a_fix_cites_the_changed_entry_revision_and_a_confirmation_reads_it(store) -> None:
    from sandbox_science.orchestrator.consistency.findings import Findings
    from sandbox_science.orchestrator.documents import DocumentError

    from .test_consistency import read_confirmation

    reviews, _, _ = store
    change(reviews, CLAIM, CLAIM, op="made")
    _, finding = finding_on(reviews, "statement")
    api = Findings(reviews)
    revision = api.get("p", "s", finding)["revision"]
    # The cited revision must be a later revision of an entry the finding is on.
    with pytest.raises(DocumentError, match="not later than") as early:
        api.respond("p", "s", fix(finding, revision, ["C-1", 1], op="early"), MAIN)
    assert early.value.kind == "invalid_request"
    change(reviews, {"entry": "C-1", "expected": 1, "statement": "$a^{16} = 1$ in GF(17)."},
           op="fixed")  # fmt: skip
    change(reviews, {"entry": "C-2", "expected": 1, "title": "Other"}, op="other")
    with pytest.raises(DocumentError, match="unrelated to this finding") as unrelated:
        api.respond("p", "s", fix(finding, revision, ["C-2", 2], op="other"), MAIN)
    assert unrelated.value.kind == "policy_denied"
    claimed = api.respond("p", "s", fix(finding, revision, ["C-1", 2]), MAIN)
    assert claimed["status"] == "claimed fixed"
    assert api.respond("p", "s", fix(finding, revision, ["C-1", 2]), MAIN)["replayed"]
    entry = Ledger(reviews.documents).entry("p", "C-1").entry_id
    history = api.get("p", "s", finding)["history"]
    assert history[-1]["payload"]["entry_revisions"] == [
        {"entry": entry, "handle": "C-1", "revision": 2}
    ]
    # The confirmation review pins the changed revision, and confirms only once read.
    confirm = dispatch(reviews, "confirmation")
    changed = [
        item
        for item in reviews.manifest("p", "s", confirm, limit=20)["evidence"]
        if item["kind"] == "ledger" and item["locator"]["revision"] == 2
    ]
    assert [item["locator"]["handle"] for item in changed] == ["C-1"]
    decision = {
        "finding": finding,
        "expected": claimed["revision"],
        "decision": "confirm",
        "explanation": "The statement now agrees with the check.",
        "anchors": [{"evidence": changed[0]["evidence"]}],
        "operation_id": "confirm",
    }
    with pytest.raises(DocumentError, match="read the pinned"):
        api.confirm("p", "s", confirm, "owner", decision)
    read_confirmation(reviews, confirm)
    assert api.confirm("p", "s", confirm, "owner", decision)["status"] == "confirmed fixed"


def test_a_fix_with_entry_revisions_keeps_the_receipts_of_fixes_without_them(store) -> None:
    """A response without entry revisions has the digest it had before the field was
    added, so an old receipt replays."""
    from sandbox_science.orchestrator.consistency.findings import Findings
    from sandbox_science.orchestrator.consistency.models import FindingResponse

    reviews, _, _ = store
    change(reviews, CLAIM, op="made")
    _, finding = finding_on(reviews, "statement")
    response = FindingResponse.model_validate(
        {
            "finding": finding,
            "expected": 1,
            "action": "rebut",
            "explanation": "The statement is right.",
            "operation_id": "rebut",
        }
    )
    assert "entry_revisions" not in response.model_dump(mode="json", exclude_defaults=True)
    first = Findings(reviews).respond("p", "s", response.model_dump(mode="json"), MAIN)
    again = Findings(reviews).respond(
        "p", "s", {**response.model_dump(mode="json"), "entry_revisions": []}, MAIN
    )
    assert again == {**first, "replayed": True}
