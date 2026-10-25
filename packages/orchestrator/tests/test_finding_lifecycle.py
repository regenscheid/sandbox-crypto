# SPDX-License-Identifier: MIT
"""A finding's lifecycle in the review store (docs/specs/consistency-review.md, CR-40 to
CR-47; fixture CR-F16): five statuses, each change an attributed record of the finding's
history, the re-check, the author's dismissal, supersession and staleness, the gate's
reading of them, the author's tools, and the control store step that maps the eight
earlier statuses."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.controller import AUTHOR_SPECS, ReviewController
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.notices import FindingNotices
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.documents import USER, DocumentError, DocumentStore, store_path
from sandbox_science.orchestrator.ledger import gate

from .test_consistency import MAIN, anchor, context_for, dispatch, raise_finding, read_confirmation
from .test_consistency import store as store
from .test_review_ledger import clause, establish, finding_on, supported

R1 = "# Report\n\nP1.\n\nP2 says 16.\n\nP3.\n\nP4 says 7.\n\nP5.\n\nP6 says 9.\n"
R2 = "# Report\n\nP1.\n\nP2 says 15.\n\nP3.\n\nP4 says 7, roughly.\n\nP5.\n\nP6 says 9.\n"
R3 = "# Report\n\nP1.\n\nP2 says 15.\n\nP3.\n\nP4 says 7, roughly.\n\nP5.\n\nP6 says 8.\n"
# Blocks: b1 the heading, b2 to b7 the six paragraphs.
P2, P4, P6 = "b3", "b5", "b7"


def item(reviews: ReviewStore, review: str, kind: str, revision: int | None = None) -> str:
    """The evidence ID of the review's item of ``kind``, at ``revision`` when given."""
    for found in reviews.manifest("p", "s", review, limit=50)["evidence"]:
        if found["kind"] == kind and (
            revision is None or found["locator"].get("revision") == revision
        ):
            return str(found["evidence"])
    raise AssertionError(f"no {kind} item at revision {revision}")


def raise_on(
    reviews: ReviewStore, review: str, block: str, severity: str, category: str, op: str
) -> str:
    return raise_finding(
        reviews,
        review,
        severity=severity,
        category=category,
        statement=f"Paragraph {block} does not agree with the output.",
        anchors=[{"evidence": item(reviews, review, "document", 1), "block": block}],
        operation_id=op,
    )["finding_id"]


def decide(
    reviews: ReviewStore, review: str, finding: str, decision: str, anchors: list[Any], op: str
) -> dict[str, Any]:
    expected = Findings(reviews).get("p", "s", finding)["revision"]
    return Findings(reviews).decide(
        "p",
        "s",
        review,
        "owner",
        {
            "finding": finding,
            "expected": expected,
            "decision": decision,
            "explanation": f"Decided {decision} on the revision this review holds.",
            "anchors": anchors,
            "operation_id": op,
        },
    )


def history(reviews: ReviewStore, finding: str) -> list[tuple[str, str, str | None]]:
    return [
        (entry["status"], entry["actor_kind"], entry["answer"])
        for entry in Findings(reviews).get("p", "s", finding)["history"]
    ]


def test_cr_f16_a_findings_lifecycle(store) -> None:
    reviews, documents, now = store
    findings = Findings(reviews)
    doc = documents.create(
        "p", name="report", title="Report", text=R1, author=MAIN, session_id="s"
    ).document_id
    reviews.ingest_documents("p", "s")
    first = dispatch(reviews)
    f1 = raise_on(reviews, first, P2, "fail", "contradicting_value", "f1")
    f2 = raise_on(reviews, first, P4, "warn", "evidence_strength", "f2")
    f3 = raise_on(reviews, first, P6, "warn", "method", "f3")
    reviews.finish("p", "s", first, "owner", "findings", "Three findings.")

    # The author writes r2, changing paragraphs 2 and 4, and tries to make F1 fixed.
    documents.edit_source("p", doc, base=1, text=R2, author=USER, reason="r2", operation_id="r2")
    with pytest.raises(DocumentError, match="cannot set a finding's status") as refused:
        findings.respond(
            "p",
            "s",
            {
                "finding": f1,
                "expected": 1,
                "action": "set",
                "status": "fixed",
                "explanation": "I fixed it.",
                "operation_id": "self-fix",
            },
            MAIN,
        )
    assert refused.value.kind == "policy_denied"

    # The next dispatch queues the re-checks of F1 and F2, whose paragraphs changed.
    assert findings.queue_rechecks("p", "s") == 2
    assert findings.get("p", "s", f3)["recheck"] is None
    recheck = dispatch(reviews, "confirmation")
    read_confirmation(reviews, recheck)
    new = item(reviews, recheck, "document", 2)
    old = item(reviews, first, "document", 1)
    # An anchor at the old revision is not in the new one.
    with pytest.raises(DocumentError, match="lies in the new revision"):
        decide(reviews, recheck, f1, "resolved", [{"evidence": old, "block": P2}], "old")
    answered = decide(reviews, recheck, f1, "resolved", [{"evidence": new, "block": P2}], "f1-r")
    assert answered["status"] == "fixed"
    # The re-check of F2 cannot decide at first: the review ends without its answer.
    reviews.finish("p", "s", recheck, "owner", "findings", "F2 is unclear.")
    second = findings.get("p", "s", f2)
    assert (second["status"], second["latest_recheck"]["answer"]) == ("open", "inconclusive")
    assert second["recheck"]["attempts"] == 1

    # A later dispatch tries it again, and the reviewer answers unresolved.
    now[0] += 200
    again = dispatch(reviews, "confirmation")
    read_confirmation(reviews, again)
    new = item(reviews, again, "document", 2)
    answered = decide(reviews, again, f2, "unresolved", [{"evidence": new, "block": P4}], "f2-u")
    f4 = answered["successor"]
    reviews.finish("p", "s", again, "owner", "findings", "F2 persists.")
    replaced, successor = findings.get("p", "s", f2), findings.get("p", "s", f4)
    assert (replaced["status"], replaced["superseded_by"]) == ("superseded", f4)
    assert (successor["status"], successor["supersedes"]) == ("open", f2)
    assert (successor["severity"], successor["category"]) == ("warn", "evidence_strength")
    assert successor["anchors"][0]["locator"]["revision"] == 2

    # The author dismisses F4 with a reason: shown, and unresolved for the gate.
    findings.respond(
        "p",
        "s",
        {
            "finding": f4,
            "expected": successor["revision"],
            "action": "dismiss",
            "explanation": "Paragraph 4 now says the value is approximate.",
            "operation_id": "dismiss-f4",
        },
        MAIN,
    )
    dismissed = findings.get("p", "s", f4)
    assert dismissed["status"] == "dismissed"
    assert dismissed["dismissal"]["by"] == "agent"
    assert dismissed["dismissal"]["reason"] == "Paragraph 4 now says the value is approximate."
    assert not gate.resolved(dismissed["status"], dismissed["dismissal"])

    # r3 changes paragraph 6, and the session closes before another review.
    documents.edit_source("p", doc, base=2, text=R3, author=USER, reason="r3", operation_id="r3")
    assert findings.stale("p", "s") == 1
    assert findings.get("p", "s", f3)["status"] == "stale"
    assert findings.get("p", "s", f1)["status"] == "fixed"
    fixed = findings.get("p", "s", f1)["latest_recheck"]
    assert (fixed["answer"], fixed["anchors"][0]["locator"]["revision"]) == ("resolved", 2)
    assert fixed["reviewer"] == {
        "alias": "reviewer",
        "resolved_model": None,
        "rubric": "sandbox-consistency-9",
    }

    # Every change is an attributed record of the finding's history.
    assert history(reviews, f1) == [
        ("open", "controller", None),
        ("open", "platform", None),
        ("fixed", "controller", "resolved"),
    ]
    assert history(reviews, f2) == [
        ("open", "controller", None),
        ("open", "platform", None),
        ("open", "controller", "inconclusive"),
        ("superseded", "controller", "unresolved"),
    ]
    assert history(reviews, f3) == [("open", "controller", None), ("stale", "platform", None)]
    assert history(reviews, f4) == [("open", "controller", None), ("dismissed", "agent", None)]
    with documents.control.read() as tx:
        db = tx.sql
        events = db.execute(
            "SELECT count(*) FROM control_events WHERE kind='finding.transitioned'"
        ).fetchone()[0]
        rows = db.execute("SELECT count(*) FROM review_finding_history").fetchone()[0]
        ledger = db.execute("SELECT count(*) FROM ledger_revisions").fetchone()[0]
    assert events == rows == 11
    # The ledger holds none of it.
    assert ledger == 0


def test_a_message_anchored_finding_is_rechecked_only_on_a_later_message_and_never_stale(
    store,
) -> None:
    from .test_consistency import claim

    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    finding = raise_finding(reviews, review)["finding_id"]
    reviews.finish("p", "s", review, "owner", "findings", "One.")
    assert Findings(reviews).queue_rechecks("p", "s") == 0
    assert Findings(reviews).stale("p", "s") == 0
    with pytest.raises(DocumentError, match="no message"):
        Findings(reviews).respond(
            "p",
            "s",
            {
                "finding": finding,
                "expected": 1,
                "action": "fix",
                "explanation": "My next answer corrects it.",
                "messages": ["msg-0000000000000000"],
                "operation_id": "fix",
            },
            MAIN,
        )
    assert Findings(reviews).get("p", "s", finding)["status"] == "open"


def test_a_fix_queues_a_recheck_and_leaves_the_finding_open(store) -> None:
    reviews, documents, _ = store
    from .test_consistency import claim

    claim(reviews)
    review = dispatch(reviews)
    finding = raise_finding(reviews, review)["finding_id"]
    reviews.finish("p", "s", review, "owner", "findings", "One.")
    doc = documents.create(
        "p", name="fixed", title="N", text="The count is 15.\n", author=MAIN, session_id="s"
    ).document_id
    fixed = Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding,
            "expected": 1,
            "action": "fix",
            "explanation": "Corrected the count.",
            "revisions": [[doc, 1]],
            "operation_id": "fix",
        },
        MAIN,
    )
    assert fixed["status"] == "open"
    found = Findings(reviews).get("p", "s", finding)
    assert found["recheck"]["cause"] == "fix"
    assert found["recheck"]["places"][0]["document"]["document"] == doc
    assert reviews.confirmation_targets("p", "s") == [(finding, found["revision"])]
    # A fix is never offered back to the agent that wrote it.
    assert FindingNotices(reviews).pending("p", "s", "author-2", set()) == []


@pytest.mark.parametrize("decision", ["accept_dismissal", "reject_dismissal"])
def test_the_next_review_accepts_or_rejects_a_dismissal_which_stands(store, decision) -> None:
    from .test_consistency import claim

    reviews, _, now = store
    claim(reviews)
    review = dispatch(reviews)
    finding = raise_finding(reviews, review)["finding_id"]
    reviews.finish("p", "s", review, "owner", "findings", "One.")
    long = {
        "finding": finding,
        "expected": 1,
        "action": "dismiss",
        "explanation": "x" * 2001,
        "operation_id": "too-long",
    }
    with pytest.raises(DocumentError, match="2,000 characters"):
        Findings(reviews).respond("p", "s", long, MAIN)
    Findings(reviews).respond(
        "p", "s", {**long, "explanation": "The output said 16.", "operation_id": "d"}, MAIN
    )
    # The dismissal is not offered to the author, and is in the next review's snapshot.
    assert FindingNotices(reviews).counts("p", "s") == {}
    claim(reviews, "More work.", "m2")
    now[0] += 200
    later = dispatch(reviews)
    pinned = [i for i in reviews.manifest("p", "s", later)["evidence"] if i["kind"] == "finding"]
    assert len(pinned) == 1
    decided = decide(reviews, later, finding, decision, [anchor(reviews, later)], "decide")
    assert decided["status"] == "dismissed"
    found = Findings(reviews).get("p", "s", finding)
    accepted = decision == "accept_dismissal"
    assert found["dismissal"]["review"]["decision"] == ("accepted" if accepted else "rejected")
    assert gate.resolved(found["status"], found["dismissal"]) is accepted
    assert reviews.run("p", "s", later)["dismissals_rejected"] == int(not accepted)
    # Decided once: it is not in the next snapshot.
    reviews.finish("p", "s", later, "owner", "passed", "Checked.")
    claim(reviews, "Even more work.", "m3")
    now[0] += 200
    last = dispatch(reviews)
    assert not [i for i in reviews.manifest("p", "s", last)["evidence"] if i["kind"] == "finding"]


def test_a_later_review_replaces_an_open_finding_on_the_same_work_only(store) -> None:
    from .test_consistency import claim

    reviews, documents, now = store
    documents.create("p", name="report", title="R", text=R1, author=MAIN, session_id="s")
    reviews.ingest_documents("p", "s")
    review = dispatch(reviews)
    old = raise_on(reviews, review, P2, "fail", "contradicting_value", "old")
    reviews.finish("p", "s", review, "owner", "findings", "One.")
    claim(reviews, "Elsewhere.", "m2")
    now[0] += 200
    later = dispatch(reviews)
    elsewhere = [{"evidence": item(reviews, later, "transcript")}]
    with pytest.raises(DocumentError, match="same work"):
        raise_finding(reviews, later, anchors=elsewhere, supersedes=old, operation_id="no")
    same = [{"evidence": item(reviews, review, "document", 1), "block": P4}]
    new = raise_finding(
        reviews,
        later,
        anchors=same,
        supersedes=old,
        statement="Better stated.",
        operation_id="yes",
    )["finding_id"]
    replaced = Findings(reviews).get("p", "s", old)
    assert (replaced["status"], replaced["superseded_by"]) == ("superseded", new)
    assert Findings(reviews).get("p", "s", new)["supersedes"] == old


def test_the_user_may_set_any_status_with_a_reason(store) -> None:
    from .test_consistency import claim

    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    finding = raise_finding(reviews, review)["finding_id"]
    reviews.finish("p", "s", review, "owner", "findings", "One.")
    revision = 1
    for status in ("stale", "fixed", "superseded", "dismissed", "open"):
        result = Findings(reviews).respond(
            "p",
            "s",
            {
                "finding": finding,
                "expected": revision,
                "action": "set",
                "status": status,
                "explanation": f"Set {status}.",
                "operation_id": f"set-{status}",
            },
            USER,
        )
        assert result["status"] == status
        revision = result["revision"]
    assert [e["actor_kind"] for e in Findings(reviews).get("p", "s", finding)["history"][1:]] == [
        "user"
    ] * 5
    with pytest.raises(DocumentError, match="invalid finding response"):
        Findings(reviews).respond(
            "p",
            "s",
            {
                "finding": finding,
                "expected": revision,
                "action": "set",
                "status": "claimed fixed",
                "explanation": "Old status.",
                "operation_id": "bad",
            },
            USER,
        )


def test_a_delegated_agent_changes_no_finding_and_the_author_never_rebuts(store) -> None:
    from .test_consistency import claim

    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    finding = raise_finding(reviews, review)["finding_id"]
    response = {
        "finding": finding,
        "expected": 1,
        "action": "dismiss",
        "explanation": "No.",
        "operation_id": "d",
    }
    delegated = MAIN.model_copy(update={"role": "delegated"})
    with pytest.raises(DocumentError, match="only the user"):
        Findings(reviews).respond("p", "s", response, delegated)
    with pytest.raises(DocumentError, match="invalid finding response"):
        Findings(reviews).respond("p", "s", {**response, "action": "rebut"}, MAIN)
    (respond,) = [spec for spec in AUTHOR_SPECS if spec.name == "review_respond"]
    assert respond.parameters["properties"]["action"] == {"enum": ["fix", "dismiss"]}


async def test_the_list_tool_returns_findings_by_status_and_counts_are_of_open_ones(
    store,
) -> None:
    from .test_consistency import claim

    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    kept = raise_finding(reviews, review)["finding_id"]
    gone = raise_finding(
        reviews,
        review,
        category="method",
        operation_id="two",
    )["finding_id"]
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": gone,
            "expected": 1,
            "action": "dismiss",
            "explanation": "Not a problem.",
            "operation_id": "d",
        },
        MAIN,
    )
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    _, opened = await controller.author_call("review_findings", {}, "op")
    _, dismissed = await controller.author_call("review_findings", {"status": "dismissed"}, "op")
    assert [f["finding_id"] for f in opened["result"]["findings"]] == [kept]
    assert [f["finding_id"] for f in dismissed["result"]["findings"]] == [gone]
    with pytest.raises(DocumentError, match="unknown finding status"):
        await controller.author_call("review_findings", {"status": "withdrawn"}, "op")
    assert FindingNotices(reviews).counts("p", "s") == {"fail:open": 1}


# The gate (CR-41; ledger, LG-17).


def respond(reviews: ReviewStore, finding: str, body: dict[str, Any], author=USER) -> None:
    revision = Findings(reviews).get("p", "s", finding)["revision"]
    Findings(reviews).respond("p", "s", {"finding": finding, "expected": revision, **body}, author)


def test_the_gate_reads_an_agents_dismissal_as_unresolved_and_the_users_as_resolved(
    store,
) -> None:
    reviews, _, _ = store
    supported(reviews)
    _, finding = finding_on(reviews, "statement")
    respond(reviews, finding, {"action": "dismiss", "explanation": "It holds.",
                               "operation_id": "agent"}, MAIN)  # fmt: skip
    assert clause(reviews, "agent", 2) == "finding"
    respond(reviews, finding, {"action": "dismiss", "explanation": "It holds.",
                               "operation_id": "user"})  # fmt: skip
    establish(reviews, "user", 2)


@pytest.mark.parametrize("status", ["open", "stale"])
def test_the_gate_holds_open_and_stale_fail_findings(store, status) -> None:
    reviews, _, _ = store
    supported(reviews)
    _, finding = finding_on(reviews, "statement")
    if status != "open":
        respond(reviews, finding, {"action": "set", "status": status,
                                   "explanation": "Set.", "operation_id": "s"})  # fmt: skip
    assert clause(reviews, "held", 2) == "finding"


def test_a_superseded_fail_finding_counts_as_its_successor(store) -> None:
    from .test_consistency import claim

    reviews, _, now = store
    supported(reviews)
    _, finding = finding_on(reviews, "statement")
    claim(reviews, "Elsewhere.", "m9")
    now[0] += 200
    review = dispatch(reviews)
    successor = raise_finding(
        reviews,
        review,
        severity="fail",
        category="method",
        statement="Elsewhere.",
        operation_id="successor",
    )["finding_id"]
    reviews.finish("p", "s", review, "owner", "findings", "One.")
    respond(reviews, finding, {"action": "set", "status": "superseded", "successor": successor,
                               "explanation": "Replaced.", "operation_id": "s"})  # fmt: skip
    with reviews.documents.control.read() as tx:
        assert gate.successor_state(tx.sql, finding)[:2] == (successor, "open")
    assert clause(reviews, "successor-open", 2) == "finding"
    respond(reviews, successor, {"action": "set", "status": "fixed", "explanation": "Fixed.",
                                 "operation_id": "f"})  # fmt: skip
    establish(reviews, "successor-fixed", 2)


def test_the_gate_reads_each_status(store) -> None:
    assert gate.resolved("fixed", None)
    assert gate.resolved("dismissed", {"by": "user", "review": None})
    assert gate.resolved("dismissed", {"by": "agent", "review": {"decision": "accepted"}})
    assert not gate.resolved("dismissed", {"by": "agent", "review": {"decision": "rejected"}})
    assert not gate.resolved("dismissed", {"by": "agent", "review": None})
    assert not gate.resolved("open", None)
    assert not gate.resolved("stale", None)
    assert not gate.resolved("superseded", None, "controller")
    assert gate.resolved("superseded", None, "user")


# The control store step (CR-47).


_OLD = (
    "open",
    "claimed fixed",
    "confirmed fixed",
    "withdrawn",
    "challenged",
    "unaddressed",
    "inconclusive",
    "superseded",
)


def _older(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A control store in the version before the lifecycle step, with a finding of each
    of the eight earlier statuses and their transitions."""
    with monkeypatch.context() as old:
        before = migrations.MIGRATIONS.index(migrations._finding_lifecycle)
        old.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:before])
        old.setattr(migrations, "VERSION", migrations.BASELINE + before - 1)
        documents = DocumentStore(path)
        documents.initialize()
        documents.close()
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO review_runs(review_id,project,session,kind,author_run,owner,state,"
        "start_seq,end_seq,created,alias,resolved_model,rubric,config,config_revision,"
        "classifier,coverage_complete) VALUES('rev-000000000000000000000001','p','s',"
        "'terminal','author-1','owner','findings',1,1,1,'reviewer','model-x',"
        "'sandbox-consistency-9','{}','c','{}',1)"
    )
    rebut = {"action": "rebut", "explanation": "It holds.", "anchors": [{"evidence": "e"}]}
    fix = {"action": "fix", "explanation": "Fixed.", "revisions": [{"document": "doc-x",
           "revision": 2, "sha256": "0" * 64}], "anchors": []}  # fmt: skip
    review = "review:rev-000000000000000000000001"
    raised = ("open", review, "Raised.", {})
    fixed = ("claimed fixed", "session:s", "Fixed.", fix)
    rebutted = ("open", "session:s", "It holds.", rebut)
    histories: dict[str, list[tuple[str, str, str, dict[str, Any]]]] = {
        "open": [raised],
        "claimed fixed": [raised, fixed],
        "confirmed fixed": [
            raised,
            fixed,
            ("confirmed fixed", review, "It agrees.", {"anchors": [{"evidence": "e2"}]}),
        ],
        "withdrawn": [raised, rebutted,
                      ("withdrawn", review, "Accepted.", {"decision": "accept_rebuttal"})],
        "challenged": [raised, rebutted,
                       ("challenged", review, "Rejected.", {"decision": "reject_rebuttal"})],
        "unaddressed": [raised, ("unaddressed", "platform:review-bound", "Bound.", {})],
        "inconclusive": [("inconclusive", review, "Cannot decide.", {})],
        "superseded": [raised, ("superseded", "platform:supersession", "Replaced.", {})],
    }  # fmt: skip
    for number, status in enumerate(_OLD, 1):
        finding = f"finding-{number:024x}"
        rows = histories[status]
        db.execute(
            "INSERT INTO review_findings(finding_id,project,session,review_id,severity,category,"
            "statement,anchors,remedy,status,revision,reflags,dedupe,supersedes,created) "
            "VALUES(?,'p','s','rev-000000000000000000000001','fail','method','A problem.',"
            "'[]',NULL,?,?,0,?,NULL,1)",
            (finding, status, len(rows), f"dedupe-{number}"),
        )
        for revision, (to, actor, reason, payload) in enumerate(rows, 1):
            db.execute(
                "INSERT INTO review_transitions(finding_id,revision,status,actor,reason,payload,"
                "created) VALUES(?,?,?,?,?,?,?)",
                (finding, revision, to, actor, reason, json.dumps(payload), 10 * revision),
            )
    db.commit()
    db.close()


def _found(path: Path) -> dict[str, dict[str, Any]]:
    db = sqlite3.connect(path)
    try:
        rows = db.execute(
            "SELECT finding_id,status,at_bound,recheck,dismissal,latest_recheck "
            "FROM review_findings ORDER BY finding_id"
        ).fetchall()
        history = db.execute(
            "SELECT finding_id,revision,status,former,actor,actor_kind,reason,created "
            "FROM review_finding_history ORDER BY seq"
        ).fetchall()
    finally:
        db.close()
    found: dict[str, dict[str, Any]] = {}
    for (finding, status, bound, recheck, dismissal, latest), old in zip(rows, _OLD, strict=True):
        found[old] = {
            "id": finding,
            "status": status,
            "at_bound": bound,
            "recheck": None if recheck is None else json.loads(recheck),
            "dismissal": None if dismissal is None else json.loads(dismissal),
            "latest_recheck": None if latest is None else json.loads(latest),
            "history": [row[1:] for row in history if row[0] == finding],
        }
    return found


def test_the_step_maps_each_earlier_status_and_keeps_the_history(tmp_path, monkeypatch) -> None:
    path = tmp_path / "control.sqlite3"
    _older(path, monkeypatch)
    documents = DocumentStore(path)
    documents.initialize()
    documents.close()
    found = _found(path)
    assert {old: f["status"] for old, f in found.items()} == {
        "open": "open",
        "claimed fixed": "open",
        "confirmed fixed": "fixed",
        "withdrawn": "dismissed",
        "challenged": "open",
        "unaddressed": "open",
        "inconclusive": "open",
        "superseded": "stale",
    }
    # A claimed fix's re-check is queued, holding the revisions it named.
    queued = found["claimed fixed"]["recheck"]
    assert (queued["cause"], queued["response"], queued["attempts"]) == ("fix", 2, 0)
    assert queued["places"] == [
        {"document": {"document": "doc-x", "revision": 2, "sha256": "0" * 64}}
    ]
    # A confirmed fix carries the confirmation as its resolved re-check, by its reviewer.
    resolved = found["confirmed fixed"]["latest_recheck"]
    assert (resolved["answer"], resolved["review"]) == ("resolved", "rev-000000000000000000000001")
    assert resolved["reviewer"] == {"alias": "reviewer", "resolved_model": "model-x",
                                    "rubric": "sandbox-consistency-9"}  # fmt: skip
    # An accepted rebuttal is the author's dismissal the review accepted.
    accepted = found["withdrawn"]["dismissal"]
    assert (accepted["by"], accepted["reason"], accepted["review"]["decision"]) == (
        "agent", "It holds.", "accepted")  # fmt: skip
    # A challenged finding keeps the rebuttal as a rejected dismissal, at its bound.
    challenged = found["challenged"]
    assert (challenged["at_bound"], challenged["dismissal"]["review"]["decision"]) == (
        1,
        "rejected",
    )
    assert found["unaddressed"]["at_bound"] == 1
    assert found["open"]["at_bound"] == 0
    # An inconclusive finding carries its last re-check, marked inconclusive.
    assert found["inconclusive"]["latest_recheck"]["answer"] == "inconclusive"
    assert found["inconclusive"]["latest_recheck"]["why"] == "Cannot decide."
    # The transitions are the history's first rows, actors, times and reasons kept.
    assert found["challenged"]["history"] == [
        (1, "open", None, "review:rev-000000000000000000000001", "controller", "Raised.", 10.0),
        (2, "open", None, "session:s", "agent", "It holds.", 20.0),
        (3, "open", "challenged", "review:rev-000000000000000000000001", "controller",
         "Rejected.", 30.0),
    ]  # fmt: skip
    assert found["superseded"]["history"][-1][1:5] == (
        "stale", "superseded", "platform:supersession", "platform")  # fmt: skip
    assert found["withdrawn"]["history"][-1][1:3] == ("dismissed", "withdrawn")


def test_the_step_changes_nothing_when_it_runs_again(tmp_path, monkeypatch) -> None:
    path = tmp_path / "control.sqlite3"
    _older(path, monkeypatch)
    documents = DocumentStore(path)
    documents.initialize()
    documents.close()
    first = _found(path)
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        migrations._finding_lifecycle(migrations.Schema(db))
        db.execute("COMMIT")
    finally:
        db.close()
    assert _found(path) == first
    # And the store, at its version, opens without running it.
    documents = DocumentStore(path)
    documents.initialize()
    documents.close()
    assert _found(path) == first


def test_the_store_refuses_an_earlier_status(store) -> None:
    reviews, documents, _ = store
    from .test_consistency import claim

    claim(reviews)
    review = dispatch(reviews)
    finding = raise_finding(reviews, review)["finding_id"]
    with documents.control.write() as tx, pytest.raises(sqlite3.IntegrityError, match="five"):
        tx.sql.execute(
            "UPDATE review_findings SET status='claimed fixed' WHERE finding_id=?", (finding,)
        )


def test_the_session_close_command_makes_findings_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``session close`` is where a session closes (CR-44): a finding whose reviewed block
    has a newer revision and no re-check becomes stale."""
    from types import SimpleNamespace

    from sandbox_science.orchestrator import cli, cli_documents
    from sandbox_science.orchestrator.projects import ProjectStore

    state = tmp_path / "state"
    projects = ProjectStore(state)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.close()
    documents = DocumentStore(store_path(state))
    reviews = ReviewStore(documents, clock=lambda: 1000.0)
    doc = documents.create(
        "p", name="report", title="Report", text=R1, author=MAIN, session_id="s"
    ).document_id
    reviews.ingest_documents("p", "s")
    review = dispatch(reviews)
    finding = raise_on(reviews, review, P6, "warn", "method", "f")
    reviews.finish("p", "s", review, "owner", "findings", "One.")
    documents.edit_source("p", doc, base=1, text=R3, author=USER, reason="r", operation_id="r")
    documents.close()

    class Worker:
        def __init__(self, *_: object) -> None: ...

        async def __aenter__(self) -> Worker:
            return self

        async def __aexit__(self, *_: object) -> None: ...

        async def close_session(self, _: object) -> SimpleNamespace:
            return SimpleNamespace(kernels=[])

    monkeypatch.setattr(cli_documents, "WorkerClient", Worker)
    token = tmp_path / "token"
    token.write_text("test-secret-token\n")
    closing = ["session", "close", "--state-dir", str(state), "--project", "p", "--session", "s",
               "--worker", "http://127.0.0.1:9", "--token-file", str(token)]  # fmt: skip
    assert cli.main(closing) == 0
    assert "1 review finding is now stale" in capsys.readouterr().out
    documents = DocumentStore(store_path(state))
    try:
        assert Findings(ReviewStore(documents)).get("p", "s", finding)["status"] == "stale"
    finally:
        documents.close()


def test_the_step_refuses_a_status_it_does_not_know(tmp_path, monkeypatch) -> None:
    path = tmp_path / "control.sqlite3"
    _older(path, monkeypatch)
    db = sqlite3.connect(path)
    db.execute(
        "UPDATE review_findings SET status='bogus' WHERE finding_id=?", (f"finding-{1:024x}",)
    )
    db.commit()
    db.close()
    documents = DocumentStore(path)
    with pytest.raises(DocumentError, match="'bogus', which no step maps") as refused:
        documents.initialize()
    documents.close()
    assert refused.value.kind == "unavailable"
