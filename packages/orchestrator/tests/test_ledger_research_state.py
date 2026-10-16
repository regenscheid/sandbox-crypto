# SPDX-License-Identifier: MIT
"""The research state the ledger keeps (docs/specs/context-management.md, CM-10, CM-11,
CM-48 to CM-48b): issue kinds, a decision's reopen condition with its ``reopen_on`` links
and their flags, caveats, decisions meant for one session, and the labels that keep a
failed attempt apart from a claimed impossibility and from one the ledger settled."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.delegation.prompt import CHECK_PROMPT, TASK_PROMPT
from sandbox_science.orchestrator.documents import USER, Author, DocumentError
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.loop import SYSTEM_PROMPT

from .test_consistency import store as store
from .test_ledger import DELEGATED, MAIN, NEW, change
from .test_ledger_decisions import assessed, claimed, decide, obstructing, status
from .test_ledger_delegated_check import CONFIRMED, checked
from .test_ledger_tools import ledger as ledger
from .test_ledger_tools import state as state
from .test_ledger_tools import tool, use

QUESTION = {
    "type": "issue",
    "title": "Is Y available?",
    "statement": "Is assumption Y available for the target group?",
    "fields": {"kind": "question"},
}
CONDITION = "Reopen if assumption Y is available for the target group (I-1)."
APPROACH = {
    "type": "decision",
    "status": "rejected",
    "title": "Index calculus",
    "statement": "Index calculus for the target group: paper A needs assumption Y.",
}
NEGATIVE = {
    "type": "claim",
    "origin": "project",
    "title": "No collision",
    "statement": "No collision exists below $2^{40}$ queries.",
    "fields": {"kind": "negative_result"},
}


def watching(*targets: str, condition: str = CONDITION, **more: Any) -> dict[str, Any]:
    """The rejected approach, its reopen condition watching ``targets``."""
    links = [{"kind": "reopen_on", "entry": target} for target in targets]
    return {**APPROACH, "fields": {"reopen_condition": condition}, "links": links, **more}


def fresh(ledger: Ledger, handle: str, n: int, flags: list[str] | None = None) -> None:
    """A delegation that checked ``handle`` at its current revision, from a brief that named
    ``flags``, started a second from now, and confirmed it, complete."""
    from datetime import UTC, datetime, timedelta

    from sandbox_science.orchestrator.delegation.records import (
        Delegation,
        DelegationRecords,
        Ending,
    )

    entry = ledger.entry("p", handle)
    check: dict[str, Any] = {"entry": entry.entry_id, "handle": handle, "revision": entry.revision}
    if flags:
        check["flags"] = flags
    made = Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": "20261016T120000000000Z-run-root",
            "run": f"20261016T1200{n:02d}000000Z-run-fresh-{n}",
            "kind": "check",
            "brief": {"task": "Check.", "note": "", "inputs": [], "check": check},
            "started_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
        }
    )
    records = DelegationRecords(ledger.documents.control)
    records.start("p", "s", made)
    outcome = {"result": {**CONFIRMED, "revision": entry.revision}, "read_entry": True,
               "read_proofs": True, "alias": "main", "models": ("scripted-main",)}  # fmt: skip
    records.end("p", "s", made.delegation_id, Ending("returned", "answered", "{}", **outcome))


def labels(ledger: Ledger) -> dict[str, tuple[str, str] | None]:
    """Each entry's label and its words, as a list shows them."""
    return {
        e.handle: None if e.standing is None else (e.standing.label, e.standing.text)
        for e in ledger.entries("p").entries
    }


# Issue kinds and caveats (CM-10, items 1 and 4).


def test_an_issue_has_a_kind_and_one_that_names_none_is_a_question(ledger: Ledger) -> None:
    change(ledger, NEW["issue"], {**NEW["issue"], "fields": {"kind": "hypothesis"}}, NEW["claim"])
    assert {e.handle: e.kind for e in ledger.entries("p").entries} == {
        "I-1": "question",
        "I-2": "hypothesis",
        "C-1": "observation",
    }
    assert ledger.entry("p", "I-1").fields == {}
    with pytest.raises(DocumentError, match="issue fields"):
        change(ledger, {**NEW["issue"], "fields": {"kind": "conjecture"}}, op="unknown")
    with pytest.raises(DocumentError, match="changes nothing"):
        change(ledger, {"entry": "I-1", "expected": 1, "fields": {"kind": "question"}}, op="same")


def test_a_contradiction_concerns_the_sides_that_disagree(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], NEW["claim"])
    contradiction = {**NEW["issue"], "fields": {"kind": "contradiction"}}
    one = [{"kind": "concerns", "entry": "C-1"}]
    both = [*one, {"kind": "concerns", "entry": "C-2"}]
    for n, links in enumerate(([], one, one * 2)):
        with pytest.raises(DocumentError, match="I-1 is a contradiction: it concerns"):
            change(ledger, {**contradiction, "links": links}, op=f"short-{n}")
    change(ledger, {**contradiction, "links": both}, op="both")
    assert ledger.entry("p", "I-1").kind == "contradiction"
    with pytest.raises(DocumentError, match="is a contradiction"):
        change(ledger, {"entry": "I-1", "expected": 1, "links": one}, op="drop")
    # A question becomes a contradiction in the change that names both sides.
    change(ledger, NEW["issue"], op="question")
    kind = {"entry": "I-2", "expected": 1, "fields": {"kind": "contradiction"}}
    with pytest.raises(DocumentError, match="is a contradiction"):
        change(ledger, kind, op="kind")
    change(ledger, {**kind, "links": both}, op="kind-and-sides")


def test_a_claim_or_result_carries_up_to_ten_one_line_caveats(ledger: Ledger) -> None:
    caveats = [
        "heuristic: assumes the reduced basis behaves like a random one",
        "estimated from 2^20 samples",
    ]
    result = {**NEW["result"], "fields": {**NEW["result"]["fields"], "caveats": caveats[1:]}}
    change(ledger, {**NEW["claim"], "fields": {"kind": "observation", "caveats": caveats}}, result)
    assert ledger.entry("p", "C-1").fields["caveats"] == caveats
    assert ledger.entry("p", "R-1").fields["caveats"] == caveats[1:]
    for n, wrong in enumerate((["x"] * 11, ["x" * 301], ["two\nlines"], [""], ["   "])):
        with pytest.raises(DocumentError, match="claim fields"):
            change(ledger, {**NEW["claim"], "fields": {"caveats": wrong}}, op=f"wrong-{n}")
    with pytest.raises(DocumentError, match=r"fields\.caveats\[0\]"):
        change(ledger, {**NEW["claim"], "fields": {"caveats": ["p = 17​ only"]}}, op="hidden")
    with pytest.raises(DocumentError, match="decision fields"):
        change(ledger, {**NEW["decision"], "fields": {"caveats": caveats}}, op="decision")
    # The coordinator's decision of 2026-10-15: a caveat added to a settled entry needs no
    # decision and unsettles nothing, since it only qualifies the entry; one removed
    # strengthens the entry, so it takes a decision. Caveats are not what the gate's
    # checks and reviews covered.
    change(ledger, decide("C-1"), status("C-1", 1, "established"), op="established", author=USER)
    more = {"kind": "observation", "caveats": [*caveats, "p = 17 only"]}
    change(ledger, {"entry": "C-1", "expected": 2, "fields": more}, op="add-caveat")
    assert ledger.entry("p", "C-1").status == "established"
    for n, fewer in enumerate((caveats, [*caveats[:1], "p = 17 only"])):
        # An agent removes no caveat, so no check covers less than the entry now says (the
        # review of #440, N5); the user removes one from a settled entry by a decision.
        fields = {"kind": "observation", "caveats": fewer}
        dropped = {"entry": "C-1", "expected": 3, "fields": fields}
        with pytest.raises(DocumentError, match="an agent adds caveats to C-1 and removes none"):
            change(ledger, decide("C-1"), dropped, op=f"drop-caveat-{n}")
        with pytest.raises(DocumentError, match="C-1 is established"):
            change(ledger, dropped, op=f"user-drop-caveat-{n}", author=USER)
    change(
        ledger,
        decide("C-1"),
        {"entry": "C-1", "expected": 3, "fields": {"kind": "observation", "caveats": caveats}},
        op="dropped-by-decision",
        author=USER,
    )


# Reopen conditions (CM-10, items 2 and 3).


def test_an_abandoned_approach_is_rejected_with_its_reopen_condition(ledger: Ledger) -> None:
    """CM-10, CM-48: the main agent records an approach it abandons at once, as a rejected
    decision with its reason and its reopen condition, which it states as an open question
    when the ledger does not hold it yet."""
    with pytest.raises(DocumentError, match="state what would justify reopening it"):
        change(ledger, APPROACH, op="bare")
    with pytest.raises(DocumentError, match="names what it watches with reopen_on"):
        change(ledger, {**APPROACH, "fields": {"reopen_condition": CONDITION}}, op="unwatched")
    made = change(ledger, {**QUESTION, "key": "y"}, watching("new:y"), op="rejected")
    assert [(e["handle"], e["status"]) for e in made["entries"]] == [
        ("I-1", "open"),
        ("D-1", "rejected"),
    ]
    view = ledger.entry("p", "D-1")
    assert (view.fields, [(k.kind, k.handle) for k in view.links]) == (
        {"reopen_condition": CONDITION},
        [("reopen_on", "I-1")],
    )
    assert [(k.kind, k.handle) for k in ledger.entry("p", "I-1").incoming] == [("reopen_on", "D-1")]
    for n, (wrong, message) in enumerate(
        (
            ({**APPROACH, "links": watching("I-1")["links"]}, "watch for its reopen condition"),
            (watching("I-1", condition="None"), "is none, so it watches nothing"),
            (watching("new:t"), "points to no task"),
            ({**NEW["claim"], "links": watching("I-1")["links"]}, "holds no reopen_on link"),
        )
    ):
        with pytest.raises(DocumentError, match=message):
            change(ledger, {**NEW["task"], "key": "t"}, wrong, op=f"wrong-{n}")
    # Nothing would reopen it: its condition says none.
    change(ledger, {**APPROACH, "fields": {"reopen_condition": "none"}}, op="never")
    # A condition watches an open issue.
    change(ledger, NEW["issue"], op="answered")
    change(ledger, status("I-2", 1, "resolved"), op="resolved")
    change(ledger, {**NEW["issue"], "fields": {"kind": "hypothesis"}}, op="hypothesis")
    for issue in ("I-2", "I-3"):
        with pytest.raises(DocumentError, match=f"{issue} is not an open question"):
            change(ledger, watching(issue), op=f"watches-{issue}")
    # A rejected decision stays rejected, and the user's rejection states its condition too.
    with pytest.raises(DocumentError, match="a rejected decision stays rejected"):
        change(ledger, status("D-1", 1, "in_effect"), op="again")
    change(ledger, {**APPROACH, "status": None}, op="proposed", author=USER)
    with pytest.raises(DocumentError, match="D-3 becomes rejected: state what"):
        change(ledger, status("D-3", 1, "rejected"), op="user-rejects", author=USER)


def test_a_delegated_agent_proposes_the_exclusion_of_an_approach(ledger: Ledger) -> None:
    """CM-48a, and CM-F32's last sentence (its state block is step e2's): a task's
    abandoned approach is a proposed decision to exclude it, with its reason and reopen
    condition, which only the main agent or the user puts in effect."""
    with pytest.raises(DocumentError, match="not_permitted_for_role"):
        change(ledger, {**QUESTION, "key": "y"}, watching("new:y"), op="rejects", author=DELEGATED)
    exclusion = watching("new:y", status=None, title="Exclude index calculus")
    made = change(ledger, {**QUESTION, "key": "y"}, exclusion, op="proposes", author=DELEGATED)
    assert made["entries"][1]["status"] == "proposed"
    with pytest.raises(DocumentError, match="not_permitted_for_role"):
        change(ledger, status("D-1", 1, "in_effect"), op="delegated-decides", author=DELEGATED)
    change(ledger, status("D-1", 1, "in_effect"), op="main-decides")
    assert ledger.entry("p", "D-1").status == "in_effect"


def test_what_a_reopen_condition_watches_flags_its_decision(ledger: Ledger) -> None:
    """CM-F8 (its ledger part), LG-28 as amended: when the issue a rejected decision's
    reopen_on names is resolved, or an assumption it names is revised, the decision is
    flagged and changes in nothing else; it is cleared with a reason, or reconsidered by a
    new decision, and stays rejected."""
    change(ledger, {**QUESTION, "key": "y"}, {**NEW["assumption"], "key": "a"},
           watching("new:y", "new:a"))  # fmt: skip
    assert change(ledger, {"entry": "I-1", "expected": 1, "title": "Y?"}, op="t")["flagged"] == []
    resolved = change(ledger, status("I-1", 2, "resolved"), op="resolved")
    assert resolved["flagged"] == ["D-1"]
    view = ledger.entry("p", "D-1")
    assert (view.status, view.revision, view.open_flags) == ("rejected", 1, 1)
    (flag,) = view.flags
    assert (flag.cause, flag.source["handle"], flag.source["status"]) == (
        "reopen_condition",
        "I-1",
        "resolved",
    )
    revised = change(ledger, {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}, op="a")
    assert revised["flagged"] == ["D-1"]
    assert sorted(f.source["handle"] for f in ledger.entry("p", "D-1").flags) == ["A-1", "I-1"]
    # A delegated agent clears no flag; the main agent reconsiders with a new decision that
    # names the old one, clearing its flags with the reason.
    clear = {"flags_cleared_on": [{"entry": "D-1", "cause": "reopen_condition"}]}
    with pytest.raises(DocumentError, match="not_permitted_for_role"):
        ledger.change("p", {"operation_id": "c", "reason": "Met.", **clear}, DELEGATED)
    again = {
        "type": "decision",
        "status": "in_effect",
        "title": "Index calculus again",
        "statement": "Assumption Y is available (I-1): try index calculus again.",
        "links": [{"kind": "affects", "entry": "D-1"}],
    }
    ledger.change(
        "p", {"operation_id": "again", "reason": "I-1 holds.", "changes": [again], **clear}, MAIN
    )
    assert (ledger.entry("p", "D-1").status, ledger.entry("p", "D-1").open_flags) == ("rejected", 0)
    assert ledger.entry("p", "D-2").status == "in_effect"


# Decisions meant for one session (CM-10, item 6).


def test_a_decision_meant_for_one_session_names_it(ledger: Ledger) -> None:
    constraint = {
        "type": "decision",
        "status": "in_effect",
        "title": "Small parameter sets only",
        "statement": "In this session, try only the small parameter sets.",
        "fields": {"session": "s"},
    }
    change(ledger, constraint, {**constraint, "fields": {}})
    assert [(e.handle, e.session) for e in ledger.entries("p").entries] == [
        ("D-1", "s"),
        ("D-2", None),
    ]
    with pytest.raises(DocumentError, match="not_permitted_for_role: an agent scopes"):
        change(ledger, {**constraint, "fields": {"session": "s2"}}, op="other")
    change(ledger, {**constraint, "fields": {"session": "s2"}}, op="users", author=USER)
    with pytest.raises(DocumentError, match="no session nowhere in this project"):
        change(ledger, {**constraint, "fields": {"session": "nowhere"}}, op="none", author=USER)
    # An agent names its own session as this, which code fills in; the user names one.
    change(ledger, {**constraint, "fields": {"session": "this"}}, op="this")
    assert ledger.entry("p", "D-4").fields == {"session": "s"}
    with pytest.raises(DocumentError, match="names an agent's own session"):
        change(ledger, {**constraint, "fields": {"session": "this"}}, op="users-this", author=USER)
    # An agent moves no decision into, or out of, another session, even by a decision.
    other = Author(kind="agent", session_id="s2", run_id="r9", role="main", model="scripted")
    for n, session in enumerate(("this", None)):
        moved = {
            "entry": "D-1",
            "expected": 1,
            "fields": {} if session is None else {"session": session},
        }
        with pytest.raises(DocumentError, match="its own session, and no other"):
            change(ledger, decide("D-1"), moved, op=f"move-{n}", author=other)


# Failure is not impossibility (CM-11, CM-F25).


def test_impossibility_is_only_what_the_ledger_settled(ledger: Ledger) -> None:
    """CM-F25: a negative result proposed with the agent's own proof is claimed, a disputed
    one disputed, one established by the user's decision impossible with its scope and
    assumptions until an assumption changes, then flagged until the flag is cleared; an
    issue is impossible only within the excluded scope of the decision that obstructed it
    on an accepted assessment; failed attempts and finite evidence are attempts."""
    documents = ledger.documents
    doc = documents.create("p", name="nc", title="NC", text="# N\n\nProof.\n", author=MAIN)
    block = documents.mapped("p", doc.document_id, 1).block_ids[1]
    proof = {"role": "derivation", "type": "proof", "document": "nc", "revision": 1,
             "block": block, "scope": "queries below $2^{40}$"}  # fmt: skip
    change(ledger, {**NEW["assumption"], "key": "a"},
           {**NEGATIVE, "evidence": [proof]},
           NEGATIVE,
           {**NEGATIVE, "evidence": [proof], "links": [{"kind": "assumes", "entry": "new:a"}]},
           {**NEW["claim"], "evidence": [{"role": "supports", "type": "finite_test",
                                          "entry": "new:a", "scope": "p < 1000"}]},
           NEW["issue"], NEW["issue"], author=DELEGATED)  # fmt: skip
    change(ledger, status("C-2", 1, "disputed"), op="disputed")
    change(ledger, decide("C-3"), status("C-3", 1, "established"), op="settled", author=USER)
    claim = claimed(ledger, "I-1", "claim-1")
    accepted = assessed(ledger, "I-1", claim, "accept-1")
    change(ledger, obstructing("I-1", accepted), status("I-1", 1, "obstructed"), op="obstruct")
    claimed(ledger, "I-2", "claim-2")
    unscoped = decide("I-2")
    with pytest.raises(DocumentError, match="names the scope it excludes"):
        change(ledger, unscoped, status("I-2", 1, "obstructed"), op="unscoped", author=USER)
    by_user = decide("I-2", fields={"excluded_scope": "Below 2^40 queries."})
    change(ledger, by_user, status("I-2", 1, "obstructed"), op="by-user", author=USER)
    found = labels(ledger)
    assert found["C-1"] == ("claimed", "claimed, not established: not an impossibility")
    assert found["C-2"] == ("disputed", "disputed")
    assert found["C-3"] == (
        "impossible",
        "impossible, within its statement and its evidence's scope",
    )
    assert (found["C-4"] or ("",))[0] == "attempt"
    assert found["I-1"] == ("impossible", "impossible within its excluded scope, as D-2 decided")
    # The coordinator's decisions of 2026-10-15: the user's obstruction settles the issue
    # without an assessment (CM-13), and every new obstruction names its scope.
    assert found["I-2"] == ("impossible", "impossible within its excluded scope, as D-3 decided")
    assert found["A-1"] is found["D-1"] is None
    settled = ledger.entry("p", "C-3").standing
    assert settled is not None
    assert (settled.scope, settled.assumes) == ("queries below $2^{40}$", ["A-1"])
    issue = ledger.entry("p", "I-1").standing
    assert issue is not None
    assert (issue.scope, issue.by) == ("Index calculus over prime fields below 2^80.", "D-2")
    # Work records: the dismissal is an attempt; the claimed obstruction the decision
    # settled reads as settled, the other as claimed.
    on = {r.ending: r.standing for r in ledger.work("p", "I-1").records if r.standing}
    assert (on["limit"].label, on["limit"].text) == (
        "attempt",
        "an attempt that did not succeed: ideas ran out",
    )
    assert on["obstruction_claimed"].label == "impossible"
    (other,) = [r for r in ledger.work("p", "I-2").records if r.ending == "obstruction_claimed"]
    assert other.standing is not None
    assert other.standing.label == "claimed"
    # The assumption C-3 rests on changes: C-3 is unresolved until the flag is cleared.
    change(ledger, {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}, op="revised")
    assert labels(ledger)["C-3"] == (
        "flagged",
        "settled, but flagged (assumption revised, from A-1): unresolved until the flag is cleared",
    )
    ledger.clear_flags("p", "C-3", None, USER, reason="Holds for 19.", operation_id="cleared")
    assert (labels(ledger)["C-3"] or ("",))[0] == "impossible"
    # Only the current revision has a label.
    assert ledger.entry("p", "C-3", revision=1).standing is None


def test_a_published_negative_result_is_impossible_once_confirmed(ledger: Ledger) -> None:
    """CM-11: a published negative result is called impossible, as its source states it,
    only once it is recorded and confirmed (LG-16); a caveat added since confirms it still,
    since the citation check's material leaves caveats out."""
    reference = {**NEW["reference"], "key": "paper"}
    cited = [{"role": "supports", "type": "source_claim", "entry": "new:paper"}]
    change(ledger, reference, {**NEGATIVE, "origin": "published", "evidence": cited})
    assert labels(ledger)["C-1"] == ("claimed", "claimed by its source: not an impossibility")
    ledger.check("p", "C-1", 1, USER, operation_id="checked")
    standing = ledger.entry("p", "C-1").standing
    assert standing is not None
    assert (standing.label, standing.text, standing.by) == (
        "impossible",
        "impossible, as its source states it",
        "user",
    )
    caveat = {"kind": "negative_result", "caveats": ["heuristic only"]}
    change(ledger, {"entry": "C-1", "expected": 2, "fields": caveat}, op="caveat")
    assert labels(ledger)["C-1"] == ("impossible", "impossible, as its source states it")


async def test_the_agents_reads_show_kinds_labels_and_scopes(ledger: Ledger) -> None:
    """CM-11 in the read tools: the agent's list, read and work pages show each entry's
    kind and label, a decision's session, and each work record's label."""
    caveated = {**NEGATIVE, "fields": {"kind": "negative_result", "caveats": ["finite test"]}}
    change(ledger, {**NEW["issue"], "fields": {"kind": "experiment"}}, caveated)
    ledger.change(
        "p",
        {"operation_id": "w", "reason": "Tried.", "work": [
            {"entry": "I-1", "tried": "Birthday search.", "ending": "limit", "limit": "time"}]},
        MAIN,
    )  # fmt: skip
    ledgers = tool(ledger)
    listed = await use(ledgers, "ledger_list")
    assert [(e["handle"], e["kind"], e["standing"]) for e in listed["entries"]] == [
        ("I-1", "experiment", None),
        ("C-1", "negative_result", {
            "label": "claimed", "text": "claimed, not established: not an impossibility",
            "scope": None, "assumes": [], "by": None}),
    ]  # fmt: skip
    read = await use(ledgers, "ledger_read", entry="C-1")
    assert (read["standing"]["label"], read["fields"]["caveats"]) == ("claimed", ["finite test"])
    work = await use(ledgers, "ledger_work", entry="I-1")
    assert work["records"][0]["standing"]["text"] == "an attempt that did not succeed: time ran out"


def test_the_change_tool_describes_the_new_fields() -> None:
    from sandbox_science.orchestrator.ledger.tool import SPECS

    (spec,) = [s for s in SPECS if s.name == "ledger_change"]
    change = spec.parameters["properties"]["changes"]["items"]["properties"]
    assert {"caveats", "reopen_condition", "session"} <= set(change["fields"]["properties"])
    assert "reopen_on" in change["links"]["items"]["properties"]["kind"]["enum"]
    clearing = spec.parameters["properties"]["flags_cleared_on"]["items"]["properties"]
    assert "reopen_condition" in clearing["cause"]["enum"]


def test_the_prompts_say_when_to_record_the_research_state() -> None:
    """CM-48 to CM-48b, as far as these fields go: the main run records constraints,
    abandoned approaches with their reopen conditions, failures, caveats and
    contradictions; a task proposes; a check reports them in its result."""
    for phrase in (
        "standing constraint",
        "fields.session",
        "rejected decision with its reason and its reopen condition",
        "work record ending limit",
        "call nothing impossible that the ledger has not settled",
        "kind contradiction",
        "caveats",
    ):
        assert phrase in SYSTEM_PROMPT, phrase
    for phrase in ("proposed decision to exclude it", "reopen condition", "kind contradiction"):
        assert phrase in TASK_PROMPT, phrase
    assert "Put in open_questions the caveats" in CHECK_PROMPT


# Migration.


def test_a_store_of_the_version_before_migrates_and_keeps_its_ledger(tmp_path: Path) -> None:
    """The version that added reopen_on links and reopen_condition flags keeps the links
    and flags a store held; its issues read as questions, and its decisions watch."""
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
        made = Ledger(documents)
        change(made, NEW["issue"], {**NEW["assumption"], "key": "a"},
               {**NEW["claim"], "links": [{"kind": "assumes", "entry": "new:a"}]})  # fmt: skip
        change(made, {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}, op="flag")
        work = [{"entry": "I-1", "tried": "Brute force.", "ending": "limit", "limit": "time"}]
        made.change("p", {"operation_id": "w", "reason": "Tried.", "work": work}, MAIN)
    finally:
        documents.close()
    target = tmp_path / "old"
    target.mkdir()
    before = migrations.VERSION - 1
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", before)
    with sqlite3.connect(target / "control.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (before,)
    ProjectStore(target).close()
    documents = DocumentStore(store_path(target))
    try:
        migrated = Ledger(documents)
        assert migrated.entry("p", "I-1").kind == "question"
        claim = migrated.entry("p", "C-1")
        assert [(k.kind, k.handle) for k in claim.links] == [("assumes", "A-1")]
        assert [f.cause for f in claim.flags] == ["assumption_revised"]
        change(migrated, watching("I-1"), op="watches")
        assert change(migrated, status("I-1", 1, "resolved"), op="r")["flagged"] == ["D-1"]
    finally:
        documents.close()
    with sqlite3.connect(target / "control.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
        # The rebuilt tables keep their triggers: links and flags are not rewritten.
        for sql, message in (
            ("UPDATE ledger_links SET note='x'", "ledger revisions are immutable"),
            ("DELETE FROM ledger_links", "ledger revisions are immutable"),
            ("UPDATE ledger_flags SET cause='spread'", "a ledger flag is only cleared, once"),
            ("DELETE FROM ledger_flags", "ledger flags are kept"),
            ("UPDATE ledger_work SET tried='x'", "work records are append-only"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match=message):
                db.execute(sql)


# The review of #440: who settles, and who may change a decision's research state.


def settle(ledger: Ledger, **more: Any) -> None:
    """C-1, I-1 and I-2 (a question); I-1 obstructed by the main agent's decision D-1 on
    the user's accepted assessment, excluding index calculus below 2^80."""
    change(ledger, NEW["claim"], NEW["issue"], {**QUESTION, "key": "q"})
    claim = claimed(ledger, "I-1", "claim-1")
    accepted = assessed(ledger, "I-1", claim, "accept-1")
    deciding = obstructing("I-1", accepted)
    deciding["fields"] = {**deciding["fields"], **more.pop("fields", {})}
    deciding["links"] = [*deciding["links"], *more.pop("links", [])]
    change(ledger, deciding, status("I-1", 1, "obstructed"), op="obstruct")


def test_only_the_decision_that_obstructed_an_issue_settles_its_scope(ledger: Ledger) -> None:
    """B1: a later decision cannot widen a settled impossibility: only the decision that
    made the issue obstructed names its scope and assessment, and an assessment counts
    only as the user's; once that decision is reversed, the issue is not settled."""
    settle(ledger)
    claim = ledger.work("p", "I-1").records[-1].record_id
    own = assessed(ledger, "I-1", claim, "own-accept", author=MAIN)
    wider = obstructing("I-1", own)
    wider["fields"]["excluded_scope"] = "Every attack on every group."
    for n, later in enumerate((wider, {**wider, "status": None}, {**wider, "links": []})):
        with pytest.raises(DocumentError, match="names an excluded scope or an assessment only"):
            change(ledger, later, op=f"widen-{n}")
    # Nor while the issue stays obstructed (N4), nor two decisions in one change (N3).
    retitled = {"entry": "I-1", "expected": 2, "title": "Renamed"}
    with pytest.raises(DocumentError, match="names an excluded scope or an assessment only"):
        change(ledger, wider, retitled, op="stays")
    standing = ledger.entry("p", "I-1").standing
    assert standing is not None
    assert (standing.label, standing.scope, standing.by) == (
        "impossible",
        "Index calculus over prime fields below 2^80.",
        "D-1",
    )
    # Reversed, D-1 settles nothing, though I-1 is still obstructed.
    reversal = {**decide("D-1"), "title": "Reverse D-1"}
    closed = {**status("D-1", 1, "reversed"), "fields": {"reopen_condition": "none"}}
    change(ledger, reversal, closed, op="reverse")
    assert ledger.entry("p", "D-1").fields["excluded_scope"].startswith("Index calculus")
    assert labels(ledger)["I-1"] == (
        "claimed",
        "obstructed, but the decision that made it so is not in effect: not settled",
    )


def test_a_settled_entry_whose_decision_is_flagged_is_unresolved(ledger: Ledger) -> None:
    """S1 (CM-11): when the decision that settled an issue or a negative result is flagged,
    such as by its own reopen condition, the entry reads as unresolved too."""
    settle(
        ledger,
        fields={"reopen_condition": "If I-2 resolves."},
        links=[{"kind": "reopen_on", "entry": "I-2"}],
    )
    assert (labels(ledger)["I-1"] or ("",))[0] == "impossible"
    assert change(ledger, status("I-2", 1, "resolved"), op="resolve")["flagged"] == ["D-1"]
    assert labels(ledger)["I-1"] == (
        "flagged",
        "settled, but its decision D-1 is flagged (reopen condition, from I-2): unresolved "
        "until the flag is cleared",
    )
    change(ledger, {**NEW["assumption"], "key": "a"}, NEGATIVE, op="negative")
    established = {**decide("C-2"), "links": [{"kind": "affects", "entry": "C-2"},
                                             {"kind": "assumes", "entry": "A-1"}]}  # fmt: skip
    change(ledger, established, status("C-2", 1, "established"), op="settled", author=USER)
    assert (labels(ledger)["C-2"] or ("",))[0] == "impossible"
    change(ledger, {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}, op="revised")
    assert (labels(ledger)["C-2"] or ("",))[0] == "flagged"
    assert "its decision D-2 is flagged" in (labels(ledger)["C-2"] or ("", ""))[1]


def test_a_decision_keeps_its_research_state_and_the_users_is_the_users(ledger: Ledger) -> None:
    """S3 (LG-30): a decision in effect, rejected or reversed keeps its statement, fields
    and links unless a decision put in effect changes them; an agent never changes them on
    a decision the user wrote, and reverses one only stating its missing reopen condition."""
    change(ledger, {**QUESTION, "key": "y"}, watching("new:y"), author=USER)
    weaker = {"entry": "D-1", "expected": 1, "fields": {"reopen_condition": "none"}, "links": []}
    reworded = {"entry": "D-1", "expected": 1, "statement": "Index calculus is fine."}
    for n, rewrite in enumerate((weaker, reworded)):
        for m, changes in enumerate(([rewrite], [decide("D-1"), rewrite])):
            with pytest.raises(DocumentError, match="not_permitted_for_role: D-1 is the user's"):
                change(ledger, *changes, op=f"rewrite-{n}-{m}")
    assert ledger.entry("p", "D-1").fields == {"reopen_condition": CONDITION}
    # The user's constraint, reversed by the main agent's decision, which states its
    # missing reopen condition; its other fields stay.
    constraint = {
        "type": "decision",
        "status": "in_effect",
        "title": "Small sets only",
        "statement": "Try only the small parameter sets.",
        "fields": {"session": "s"},
    }
    change(ledger, constraint, op="constraint", author=USER)
    reworded = {**status("D-2", 1, "reversed"), "fields": {"reopen_condition": "none"},
                "statement": "Try any parameter set."}  # fmt: skip
    with pytest.raises(DocumentError, match="D-2 is the user's decision"):
        change(ledger, decide("D-2"), reworded, op="never")
    closed = {**status("D-2", 1, "reversed"), "fields": {"reopen_condition": "none"}}
    change(ledger, decide("D-2"), closed, op="reverse")
    assert ledger.entry("p", "D-2").fields == {"reopen_condition": "none", "session": "s"}
    # The agent's own decision in effect changes only by a decision.
    change(ledger, {**constraint, "fields": {}}, op="agents")
    with pytest.raises(DocumentError, match="D-4 is in_effect: its statement"):
        change(ledger, {"entry": "D-4", "expected": 1, "statement": "Any set."}, op="reword")
    change(ledger, decide("D-4"), {"entry": "D-4", "expected": 1, "statement": "Any set."},
           op="reworded")  # fmt: skip
    assert ledger.entry("p", "D-4").statement == "Any set."


@pytest.mark.parametrize("tidy", [{"fields": {}}, {"links": []}])
def test_the_users_withdrawn_acceptance_unsettles_the_issue(
    ledger: Ledger, tidy: dict[str, Any]
) -> None:
    """N1, N2: the label reads the user's latest assessment of the claim the obstruction
    rests on, and the flag the user's rejection raises reads the decision the label reads,
    though the main agent dropped its fields or its link since. The main agent clears no
    flag of the settled issue, and the user's clearing settles nothing again."""
    settle(ledger)
    claim = [r for r in ledger.work("p", "I-1").records if r.ending == "obstruction_claimed"]
    stripped = {"entry": "D-1", "expected": 1, **tidy}
    change(ledger, {**decide("D-1"), "title": "Tidy D-1"}, stripped, op="strip")
    assessed(ledger, "I-1", claim[0].record_id, "reject", verdict="rejected")
    assert ledger.entry("p", "I-1").open_flags == 1
    unsettled = (
        "claimed",
        "obstructed by D-1, whose assessment the user does not accept now: not settled",
    )
    assert labels(ledger)["I-1"] == unsettled
    clearing = {"operation_id": "clr", "reason": "Holds.", "flags_cleared_on": [{"entry": "I-1"}]}
    with pytest.raises(DocumentError, match="only the user clears a flag on a settled issue"):
        ledger.change("p", clearing, MAIN)
    ledger.change("p", clearing, USER)
    assert labels(ledger)["I-1"] == unsettled


def test_one_decision_obstructs_and_each_needs_a_fresh_acceptance(ledger: Ledger) -> None:
    """N3 and the coordinator's decision (b): one decision makes an issue obstructed in a
    change, and an acceptance settles only the obstruction it was given for, even after
    the issue was reopened."""
    change(ledger, NEW["claim"], NEW["issue"])
    first = assessed(ledger, "I-1", claimed(ledger, "I-1", "claim-x"), "accept-x")
    other = decide("I-1", title="Other", fields={"excluded_scope": "Everything."})
    with pytest.raises(DocumentError, match="one decision makes I-1 obstructed"):
        change(ledger, obstructing("I-1", first), other, status("I-1", 1, "obstructed"), op="two")
    change(ledger, obstructing("I-1", first), status("I-1", 1, "obstructed"), op="obstruct")
    change(ledger, status("I-1", 2, "open"), op="reopen")
    with pytest.raises(DocumentError, match="recorded before the issue was last obstructed"):
        change(ledger, obstructing("I-1", first), status("I-1", 3, "obstructed"), op="again")
    fresh = assessed(ledger, "I-1", claimed(ledger, "I-1", "claim-y"), "accept-y")
    change(ledger, obstructing("I-1", fresh), status("I-1", 3, "obstructed"), op="fresh")
    assert (labels(ledger)["I-1"] or ("",))[0] == "impossible"


def test_the_main_agent_reopens_an_obstructed_issue_with_its_reason(ledger: Ledger) -> None:
    """The owner, 2026-10-15: weakening a settled state needs no authority. The main
    agent reopens an obstructed issue by itself, with its reason, which the ledger keeps as
    a work record on the issue citing the decision that had obstructed it; a delegated
    agent reopens nothing, and the user's reopening needs no record."""
    settle(ledger)
    with pytest.raises(DocumentError, match="not_permitted_for_role"):
        change(ledger, status("I-1", 2, "open"), op="delegated", author=DELEGATED)
    reason = "The excluded scope assumed a prime field; the target is an extension field."
    ledger.change(
        "p",
        {"operation_id": "reopen", "reason": reason, "changes": [status("I-1", 2, "open")]},
        MAIN,
    )
    (record,) = [r for r in ledger.work("p", "I-1").records if r.ending == "reopened"]
    assert (record.decision, record.reason, record.author["role"]) == ("D-1", reason, "main")
    assert record.tried == f"Reopened I-1, which D-1 had made obstructed: {reason}"
    assert ledger.entry("p", "I-1").standing is None
    with pytest.raises(DocumentError, match="records a reopening itself"):
        ledger.change(
            "p",
            {"operation_id": "own", "reason": "r", "work": [
                {"entry": "I-1", "tried": "Reopened.", "ending": "reopened"}]},
            MAIN,
        )  # fmt: skip
    # A rejected approach is reconsidered by a new decision, the old one kept as it was.
    change(ledger, {**QUESTION, "key": "y"}, watching("new:y"), op="rejected", author=USER)
    again = {**decide("D-2"), "title": "Try it again", "statement": "Y is available now."}
    change(ledger, again, op="reconsidered")
    assert (ledger.entry("p", "D-2").status, ledger.entry("p", "D-3").status) == (
        "rejected",
        "in_effect",
    )


def test_title_and_evidence_of_a_decision_and_the_users_own_edits(ledger: Ledger) -> None:
    """N6 and the coordinator's decision (d): an agent changes neither the title nor the
    evidence of the user's decision, and the evidence of its own decision in effect only
    by a decision; the user edits the user's own decisions directly, and someone else's
    in effect only by a decision."""
    constraint = {"type": "decision", "status": "in_effect", "title": "Small sets only",
                  "statement": "Try only the small parameter sets."}  # fmt: skip
    change(ledger, NEW["claim"], op="claim")
    change(ledger, constraint, op="users", author=USER)
    proof = [{"role": "supports", "type": "proof", "entry": "C-1"}]
    for n, rewrite in enumerate(({"title": "Any sets welcome"}, {"evidence": proof})):
        with pytest.raises(DocumentError, match="D-1 is the user's decision"):
            change(ledger, {"entry": "D-1", "expected": 1, **rewrite}, op=f"agent-{n}")
    change(ledger, {"entry": "D-1", "expected": 1, "statement": "Small ones."}, op="u", author=USER)
    change(ledger, constraint, op="agents")
    for n, author in enumerate((MAIN, USER)):
        with pytest.raises(DocumentError, match="D-2 is in_effect: its statement, fields, links"):
            change(ledger, {"entry": "D-2", "expected": 1, "evidence": proof}, op=f"e-{n}",
                   author=author)  # fmt: skip
    change(ledger, decide("D-2"), {"entry": "D-2", "expected": 1, "evidence": proof}, op="e")


def test_clearing_a_flag_on_a_settled_entry_needs_the_user_or_a_fresh_check(
    ledger: Ledger,
) -> None:
    """The owner's rule applied to LG-29 (2026-10-15): clearing a flag on a settled entry
    settles it again, so the main agent does it only after a delegation's check of the entry
    at its current revision, started since the flag, that counts; the user clears it
    freely, and the main agent clears flags on unsettled entries with a reason."""
    resting = {**NEGATIVE, "links": [{"kind": "assumes", "entry": "new:a"}]}
    change(ledger, {**NEW["assumption"], "key": "a"}, resting, resting)
    change(ledger, decide("C-1"), status("C-1", 1, "established"), op="settled", author=USER)
    change(ledger, {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}, op="revised")
    assert (labels(ledger)["C-1"] or ("",))[0] == "flagged"
    clear = {"reason": "Holds for 19.", "flags_cleared_on": [{"entry": "C-1"}]}
    with pytest.raises(DocumentError, match="C-1 is settled, and clearing its flag"):
        ledger.change("p", {"operation_id": "main", **clear}, MAIN)
    # A proposed entry's flag is the main agent's to clear.
    unsettled = {"reason": "Holds.", "flags_cleared_on": [{"entry": "C-2"}]}
    ledger.change("p", {"operation_id": "proposed", **unsettled}, MAIN)
    # A check that began before the flag is not fresh; one that began since counts.
    entry = ledger.entry("p", "C-1")
    checked(ledger, entry.entry_id, revision=entry.revision, n=1)
    with pytest.raises(DocumentError, match="C-1 is settled"):
        ledger.change("p", {"operation_id": "stale", **clear}, MAIN)
    flag = ledger.entry("p", "C-1").flags[0].flag_id
    fresh(ledger, "C-1", n=2)
    with pytest.raises(DocumentError, match="from a brief that named the flag"):
        ledger.change("p", {"operation_id": "unnamed", **clear}, MAIN)
    fresh(ledger, "C-1", n=3, flags=[flag])
    ledger.change("p", {"operation_id": "fresh", **clear}, MAIN)
    assert (labels(ledger)["C-1"] or ("",))[0] == "impossible"


def test_the_main_agent_clears_no_flag_of_a_settling_decision(ledger: Ledger) -> None:
    """The decision that settled an issue is settled too: its own reopen flag, which leaves
    the issue unresolved (S1), is the user's to clear; a rejected decision's is the main
    agent's."""
    settle(
        ledger,
        fields={"reopen_condition": "If I-2 resolves."},
        links=[{"kind": "reopen_on", "entry": "I-2"}],
    )
    change(ledger, status("I-2", 1, "resolved"), op="resolve")
    clear = {"reason": "Not met.", "flags_cleared_on": [{"entry": "D-1"}]}
    with pytest.raises(DocumentError, match="D-1 was settled when the flag was raised"):
        ledger.change("p", {"operation_id": "main", **clear}, MAIN)
    ledger.change("p", {"operation_id": "user", **clear}, USER)
    assert (labels(ledger)["I-1"] or ("",))[0] == "impossible"


# The final re-check of #440: the owner's rule in general. A strengthening change by an
# agent counts only checks, reviews and acceptances recorded after the entry was last
# weakened.


def test_no_strengthening_rests_on_evidence_from_before_the_last_weakening(
    ledger: Ledger,
) -> None:
    """For each kind of strengthening, evidence recorded before the entry's latest weakening
    does not count, and the same evidence recorded since does: the gate's check after a
    dispute or a flag, a confirmation after a dispute, and an acceptance after a reopening
    (a flag cleared on a settled entry, the fourth, is tested above)."""
    from .test_ledger_delegated_check import proved

    def check_holds() -> bool:
        return next(
            c for c in ledger.gate("p", "C-1").clauses if c.clause == "independent_check"
        ).holds

    proved(ledger)
    ledger.check("p", "C-1", 2, USER, operation_id="check-1")
    assert check_holds()
    change(ledger, status("C-1", 3, "disputed"), op="dispute")
    change(ledger, status("C-1", 4, "proposed"), op="undispute")
    assert not check_holds()
    ledger.check("p", "C-1", 5, USER, operation_id="check-2")
    assert check_holds()
    change(ledger, {**NEW["assumption"], "key": "a"}, op="assumption")
    change(ledger, {"entry": "C-1", "expected": 6, "links": [{"kind": "assumes", "entry": "A-1"}]},
           op="assumes")  # fmt: skip
    ledger.check("p", "C-1", 7, USER, operation_id="check-3")
    assert check_holds()
    change(ledger, {"entry": "A-1", "expected": 1, "title": "p is 17"}, op="retitle")
    change(ledger, {"entry": "A-1", "expected": 2, "statement": "$p = 19$."}, op="flag")
    ledger.clear_flags("p", "C-1", None, USER, reason="Holds.", operation_id="cleared")
    assert not check_holds()
    # A published negative result: confirmed by the user's check, then disputed and
    # recorded again by the main agent, it is a claim of its source until checked again.
    cited = [{"role": "supports", "type": "source_claim", "entry": "new:paper"}]
    change(ledger, {**NEW["reference"], "key": "paper"},
           {**NEGATIVE, "origin": "published", "evidence": cited}, op="published")  # fmt: skip
    ledger.check("p", "C-3", 1, USER, operation_id="confirmed")
    assert (labels(ledger)["C-3"] or ("",))[0] == "impossible"
    change(ledger, status("C-3", 2, "disputed"), op="p-dispute")
    change(ledger, status("C-3", 3, "recorded"), op="p-record")
    assert (labels(ledger)["C-3"] or ("",))[0] == "claimed"
    ledger.check("p", "C-3", 4, USER, operation_id="reconfirmed")
    assert (labels(ledger)["C-3"] or ("",))[0] == "impossible"


def test_a_reestablished_entry_needs_a_review_since_its_dispute(store: Any) -> None:
    """The probe of the final re-check: C-1, established by the main agent through the
    gate, is flagged; the main agent clears nothing of it, settled when flagged, even once
    it disputes it, and establishes it again only on a review that started since."""
    import time

    from .test_consistency import MAIN as REVIEWED
    from .test_review_ledger import change as rchange
    from .test_review_ledger import establish, review_passes

    reviews, _, now = store
    ledger = Ledger(reviews.documents)
    reference = {**NEW["reference"], "fields": {"identifiers": {"eprint": "2024/123"},
                                                "published": "2024-02-01"}}  # fmt: skip
    rchange(reviews, NEGATIVE, reference, op="made")
    cited = [{"role": "supports", "type": "source_claim", "entry": "REF-1"}]
    rchange(reviews, {"entry": "C-1", "expected": 1, "evidence": cited}, op="supported")
    review_passes(reviews)
    establish(reviews, "established", 2)
    rchange(reviews, {"entry": "REF-1", "expected": 1, "statement": "A. Author, v2."}, op="v2")
    clear: dict[str, Any] = {"reason": "Same.", "flags_cleared_on": [{"entry": "C-1"}]}
    with pytest.raises(DocumentError, match="C-1 is settled"):
        ledger.change("p", {"operation_id": "c0", **clear}, REVIEWED)
    with pytest.raises(DocumentError, match="C-1 is settled"):
        ledger.change(
            "p",
            {"operation_id": "dc", **clear, "changes": [status("C-1", 3, "disputed")]},
            REVIEWED,
        )
    rchange(reviews, status("C-1", 3, "disputed"), op="disputed")
    with pytest.raises(DocumentError, match="C-1 is settled"):
        ledger.change("p", {"operation_id": "c1", **clear}, REVIEWED)
    ledger.change("p", {"operation_id": "c2", **clear}, USER)
    with pytest.raises(DocumentError, match="gate_not_met") as refused:
        establish(reviews, "again", 4)
    assert refused.value.details["clause"] == "review"
    now[0] = time.time() + 60
    rchange(reviews, {"entry": "C-1", "expected": 4, "title": "No collision, again"}, op="t")
    review_passes(reviews)
    establish(reviews, "reviewed", 5)
    assert ledger.entry("p", "C-1").status == "established"


def test_flags_on_a_settled_issue_decision_or_refutation(ledger: Ledger) -> None:
    """Only the user clears a flag on a settled issue or decision, whatever was checked,
    and even once the issue is reopened; a refuted claim is settled too, so its flag needs
    the user or a fresh check."""
    settle(
        ledger,
        fields={"reopen_condition": "If I-2 resolves."},
        links=[{"kind": "reopen_on", "entry": "I-2"}],
    )
    change(ledger, status("I-2", 1, "resolved"), op="resolve")
    flag = ledger.entry("p", "D-1").flags[0].flag_id
    fresh(ledger, "D-1", n=1, flags=[flag])
    clear = {"reason": "Not met.", "flags_cleared_on": [{"entry": "D-1"}]}
    with pytest.raises(DocumentError, match="only the user clears a flag on a settled issue"):
        ledger.change("p", {"operation_id": "checked", **clear}, MAIN)
    change(ledger, status("I-1", 2, "open"), op="reopen")
    with pytest.raises(DocumentError, match="only the user clears a flag on a settled issue"):
        ledger.change("p", {"operation_id": "reopened", **clear}, MAIN)
    accepted = next(r for r in ledger.work("p", "I-1").records if r.ending == "assessment")
    with pytest.raises(DocumentError, match="recorded before the issue was last obstructed"):
        change(ledger, obstructing("I-1", accepted.record_id, title="Again"),
               status("I-1", 3, "obstructed"), op="again")  # fmt: skip
    resting = {**NEW["claim"], "links": [{"kind": "assumes", "entry": "new:a"}]}
    change(ledger, {**NEW["assumption"], "key": "a"}, resting, op="refutable")
    change(ledger, decide("C-2"), status("C-2", 1, "refuted"), op="refuted", author=USER)
    change(ledger, {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}, op="flag")
    with pytest.raises(DocumentError, match="C-2 is settled"):
        ledger.change(
            "p", {"operation_id": "r", "reason": "x", "flags_cleared_on": [{"entry": "C-2"}]}, MAIN
        )


def test_an_acceptance_from_before_the_users_obstruction_obstructs_nothing(
    ledger: Ledger,
) -> None:
    """(b): the user obstructed I-1 narrowly, naming no assessment though the user had
    accepted a claim; once the main agent reopens it, that acceptance does not obstruct it
    again, more widely; a scope is never blank (c); and the user changes an obstruction's
    scope by a new obstruction, not by editing it (d)."""
    change(ledger, NEW["claim"], NEW["issue"])
    accepted = assessed(ledger, "I-1", claimed(ledger, "I-1", "claim-1"), "accept-1")
    with pytest.raises(DocumentError, match="excluded_scope"):
        change(ledger, decide("I-1", fields={"excluded_scope": "   "}),
               status("I-1", 1, "obstructed"), op="blank", author=USER)  # fmt: skip
    narrow = decide("I-1", fields={"excluded_scope": "Below 2^40 queries only."})
    change(ledger, narrow, status("I-1", 1, "obstructed"), op="by-user", author=USER)
    with pytest.raises(DocumentError, match="names an excluded scope or an assessment only"):
        change(ledger, {"entry": "D-1", "expected": 1, "fields": {"excluded_scope": "All."}},
               op="widen", author=USER)  # fmt: skip
    change(ledger, {"entry": "D-1", "expected": 1, "statement": "Reworded."}, op="r", author=USER)
    change(ledger, status("I-1", 2, "open"), op="reopen")
    wide = obstructing("I-1", accepted, title="Wider")
    wide["fields"]["excluded_scope"] = "Every attack on every group."
    with pytest.raises(DocumentError, match="recorded before the issue was last obstructed"):
        change(ledger, wide, status("I-1", 3, "obstructed"), op="again")


# The re-check's frictions, after #440.


def test_a_caveat_after_an_old_flag_leaves_a_result_confirmed(ledger: Ledger) -> None:
    """Adding a caveat weakens nothing: a published result the user confirmed stays
    confirmed when an agent adds a caveat after a flag that came and went."""
    cited = [{"role": "supports", "type": "source_claim", "entry": "new:paper"}]
    resting = {**NEGATIVE, "origin": "published", "evidence": cited,
               "links": [{"kind": "assumes", "entry": "new:a"}]}  # fmt: skip
    change(ledger, {**NEW["reference"], "key": "paper"}, {**NEW["assumption"], "key": "a"}, resting)
    ledger.check("p", "C-1", 1, USER, operation_id="confirmed")
    change(ledger, {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}, op="flag")
    ledger.clear_flags("p", "C-1", None, USER, reason="Holds.", operation_id="cleared")
    caveat = {"kind": "negative_result", "caveats": ["heuristic only"]}
    change(ledger, {"entry": "C-1", "expected": 2, "fields": caveat}, op="caveat")
    assert labels(ledger)["C-1"] == ("impossible", "impossible, as its source states it")


def test_the_users_move_back_to_recorded_restores_the_users_confirmation(ledger: Ledger) -> None:
    """The user's authority: a published result the user confirmed, disputed, and moved
    back to recorded by the user, is confirmed again without a new check; moved back by
    the main agent, it is not."""
    cited = [{"role": "supports", "type": "source_claim", "entry": "new:paper"}]
    published = {**NEGATIVE, "origin": "published", "evidence": cited}
    change(ledger, {**NEW["reference"], "key": "paper"}, published, published)
    for handle in ("C-1", "C-2"):
        ledger.check("p", handle, 1, USER, operation_id=f"confirmed-{handle}")
        change(ledger, status(handle, 2, "disputed"), op=f"disputed-{handle}")
    change(ledger, status("C-1", 3, "recorded"), op="user-back", author=USER)
    change(ledger, status("C-2", 3, "recorded"), op="main-back")
    assert (labels(ledger)["C-1"] or ("",))[0] == "impossible"
    assert (labels(ledger)["C-2"] or ("",))[0] == "claimed"


def test_a_reversed_decisions_flags_are_the_main_agents(ledger: Ledger) -> None:
    """CM-10: once the decision that obstructed an issue is reversed, it settles nothing,
    and the main agent clears its reopen flags with a reason."""
    settle(
        ledger,
        fields={"reopen_condition": "If I-2 resolves."},
        links=[{"kind": "reopen_on", "entry": "I-2"}],
    )
    change(ledger, status("I-2", 1, "resolved"), op="resolve")
    clear = {"reason": "Reversed.", "flags_cleared_on": [{"entry": "D-1"}]}
    with pytest.raises(DocumentError, match="only the user clears a flag"):
        ledger.change("p", {"operation_id": "early", **clear}, MAIN)
    reversal = {**decide("D-1"), "title": "Reverse D-1"}
    change(ledger, reversal, status("D-1", 1, "reversed"), status("I-1", 2, "open"), op="reverse")
    ledger.change("p", {"operation_id": "cleared", **clear}, MAIN)
    assert ledger.entry("p", "D-1").open_flags == 0
