# SPDX-License-Identifier: MIT
"""The research-state digest (docs/specs/context-management.md, CM-16, CM-26, CM-37a):
CM-F3, CM-F22 and CM-F32 as far as the digest goes, its sections and shares, the ledger's
labels as the digest shows them, and the state blocks of main and delegated runs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator import digest as digest_module
from sandbox_science.orchestrator.context.history import Archive, Entry, body
from sandbox_science.orchestrator.context.history.reads import Read
from sandbox_science.orchestrator.digest import (
    Digest,
    Line,
    Section,
    _wholes,
    constraints_block,
    render,
)
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    store_path,
)
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.notices import DIGEST, TASK_CONSTRAINTS
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import SessionPlans

from .ledger_support import AnchoredLedger
from .loop_harness import PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import CHECK, REPORT, wait
from .test_delegation import delegating as delegating
from .test_delegation_tasks import DONE, TASK
from .test_ledger import MAIN, NEW
from .test_ledger_evidence import publish
from .test_session_plans import actor, step
from .test_session_plans import body as plan_body

CONSTRAINT = "Only consider attacks whose memory stays below 2^64 bits, please."
QUOTE = "attacks whose memory stays below 2^64 bits"
CONCLUSION = "C-1 holds; I confirmed it by inspection."


def research_state(state: Path, project: str = PROJECT, session: str = SESSION) -> None:
    """The research state the fixtures name, in project ``project``:

    - D-1, the user's constraint as the agent recorded it, ``user_quoted``, project-wide;
    - D-2, the user's own constraint for ``session`` alone; D-3, one for another session;
    - I-1, an open question, which D-4, a rejected approach, watches to reopen;
    - D-5, the parent's conclusion, in effect, ``interpreted``;
    - C-2, a negative result the agent proposed, and A-1, an assumption C-2 rests on;
    - REF-1, a reference whose PDF a session read in part.
    """
    documents = DocumentStore(store_path(state))
    try:
        artifact, sha256 = publish(AnchoredLedger(documents), state, b"%PDF-1.4\n")
    finally:
        documents.close()
    projects = ProjectStore(state)
    try:
        projects.open_session(project, "other", dedicated=False)
        archive = Archive(projects.control)
        message = archive.append(project, session, "run-0", Entry("user", "user", CONSTRAINT))
        read = Read("pdf", sha256, "partial", {"pages": [1, 2, 3], "of": 12}, label="a.pdf")
        result = Entry("tool", "tool_result", "...", source="pdf_pages", call_id="c0")
        archive.append(project, session, "run-0", result, reads=[read])
    finally:
        projects.close()
    documents = DocumentStore(store_path(state))
    try:
        ledger = Ledger(documents)

        def change(*changes: dict[str, Any], op: str, author: Author = MAIN) -> None:
            reason = {"operation_id": op, "reason": f"Recorded as {op}.", "changes": list(changes)}
            ledger.change(project, reason, author)

        anchors = [{"message": message.message_id}]
        change(
            {
                "type": "decision",
                "status": "in_effect",
                "title": "Memory below 2^64",
                "statement": "Only attacks with memory below $2^{64}$ bits.",
                "basis": "user_quoted",
                "anchors": [{"message": message.message_id, "quote": QUOTE}],
            },
            op="d1",
        )
        for title, scoped in (("Small sets here", session), ("Large sets there", "other")):
            change(
                {
                    "type": "decision",
                    "status": "in_effect",
                    "title": title,
                    "statement": f"In this session, try only the {title.split()[0]} sets.",
                    "fields": {"session": scoped},
                },
                op=f"for-{scoped}",
                author=USER,
            )
        change(
            {
                "type": "issue",
                "title": "Is Y available?",
                "statement": "Is assumption Y available for the target group?",
                "fields": {"kind": "question"},
                "anchors": anchors,
            },
            op="i1",
        )
        change(
            {
                "type": "decision",
                "status": "rejected",
                "title": "Index calculus",
                "statement": "Index calculus: paper A needs assumption Y.",
                "fields": {"reopen_condition": "Reopen if Y is available (I-1)."},
                "links": [{"kind": "reopen_on", "entry": "I-1"}],
                "anchors": anchors,
            },
            op="d4",
        )
        change(
            {
                "type": "decision",
                "status": "in_effect",
                "title": "C-1 holds",
                "statement": CONCLUSION,
                "anchors": anchors,
            },
            op="d5",
        )
        change(
            {**NEW["assumption"], "key": "a", "anchors": anchors},
            {
                "type": "claim",
                "origin": "project",
                "title": "No collision",
                "statement": "No collision exists below $2^{40}$ queries.",
                "fields": {"kind": "negative_result", "caveats": ["heuristic only"]},
                "links": [{"kind": "assumes", "entry": "new:a"}],
                "anchors": anchors,
            },
            op="c2",
        )
        change({**NEW["reference"], "fields": {
            "identifiers": {"eprint": "2024/123"}, "authors": ["A. Author"], "published": "2024",
            "pdf": {"artifact_id": artifact, "revision": 1}}}, op="r1",
            author=USER)  # fmt: skip
    finally:
        documents.close()


@pytest.fixture
def state(delegating: Harness) -> Iterator[Path]:
    """The delegating harness's state, with C-1 and the research state above."""
    projects = ProjectStore(delegating.state)
    try:
        # Keep a writer open as the live harness does. Otherwise a seed connection's
        # cleanup can remove WAL sidecars before a later read-only authority check.
        projects.get(PROJECT)
        research_state(delegating.state)
        yield delegating.state
    finally:
        projects.close()


def digest_of(state: Path, budget: int = 20000) -> tuple[str, Digest]:
    documents = DocumentStore(store_path(state))
    try:
        digest = Digest(Ledger(documents))
        return render(digest.sections(PROJECT, SESSION), budget).text, digest
    finally:
        documents.close()


def test_the_digest_puts_the_users_constraints_first_with_their_words(state: Path) -> None:
    """CM-16, CM-F22: the user's standing constraints come first, the project's and this
    session's, a quoted one with the user's words beside it; then the rejected approach
    with its reason and reopen condition, then the rest in effect. Another session's
    constraint is counted, not shown; every line ends with its basis and how to read it."""
    text, _ = digest_of(state)
    decisions = text.split("## 2. Decisions\n")[1].split("## ")[0].splitlines()
    assert decisions[0].startswith("D-1 (in_effect) Only attacks with memory below")
    assert decisions[0].endswith("[user, quoted] (ledger_read D-1)")
    assert decisions[1].startswith(
        "  the user's words, as the agent quoted them and the platform found them in the "
        "user's message msg-"
    )
    assert decisions[1].endswith(f'"{QUOTE}"')
    assert decisions[2].startswith("D-2 (in_effect) In this session, try only the Small sets")
    assert "for this session only" in decisions[2]
    assert decisions[2].endswith("[user] (ledger_read D-2)")
    assert decisions[3].startswith("D-4 (rejected) Index calculus")
    assert "reason: Recorded as d4." in decisions[3]
    assert "reopen if: Reopen if Y is available (I-1)." in decisions[3]
    assert decisions[4].startswith("D-5 (in_effect) C-1 holds")
    assert decisions[4].endswith("[interpreted] (ledger_read D-5)")
    assert decisions[5] == "Decisions meant for other sessions, not shown: other: 1."
    assert "Large sets" not in text


def test_the_ledgers_labels_and_the_read_register_as_the_digest_shows_them(state: Path) -> None:
    """CM-11, CM-12, CM-16: a negative result the agent proposed reads as claimed, never
    impossible, after the plain findings, with its caveats; an assumption names what rests
    on it; an open question is shown; a reference shows how far its PDF was read."""
    text, _ = digest_of(state)
    findings = text.split("## 5. Findings\n")[1].split("## ")[0].splitlines()
    # The labelled first, which stop repeated work (#465's review), then the plain ones.
    assert findings[0].startswith("[claimed, not established: not an impossibility] C-2 (")
    assert "caveats: heuristic only" in findings[0]
    assert "impossible" not in findings[0].split("]", 1)[1]
    assert findings[1].startswith("C-1 (")
    assert "A-1 $p = 17$.; relied on by C-2" in text
    assert "I-1 (question) Is assumption Y available" in text
    assert "REF-1 A paper; read: pdf partial pages 1-3 of 12" in text


def test_each_section_keeps_to_its_share_and_passes_on_what_it_leaves(state: Path) -> None:
    """CM-16, CM-F3: a section over its share ends with how many it left out and how to
    list them; an empty section's share passes on, and what is left goes to decisions."""
    text, _ = digest_of(state, budget=600)
    assert "more left out of this digest; ledger_list with type decision lists them." in text
    assert len(text) <= 600 + 200
    many = [Section("decisions", "2. Decisions", 0.3, [Line("D" * 50)] * 40, "list")]
    empty = Section("plan", "1. The session plan", 0.1)
    alone = render([empty, *many], 1000).text
    later = Section("questions", "3. Questions", 0.6, [Line("I" * 50)])
    shared = render([empty, *many, later], 1000).text
    assert alone.count("D" * 50) == 6  # its 30% and the empty plan's 10%, less the count
    assert shared.count("D" * 50) > 6  # and what questions left at the end


async def test_a_main_run_starts_with_the_digest_and_a_task_with_the_constraints(
    state: Path, delegating: Harness
) -> None:
    """CM-26, CM-37a, CM-F3, CM-F32: a main run's state block holds the digest, D-4 with
    its reason and reopen condition among it; a delegated task's holds the user's
    constraints with their words and nothing of the parent's conclusion; a check's holds
    nothing at all."""
    await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply(calls=[CHECK]), wait(), Reply("Done.")],
        delegated=[Reply(json.dumps(DONE)), Reply(REPORT)],
    )
    (first, *_) = delegating.gateway.main
    block = body(str(first[1]["content"]))
    assert block.startswith(DIGEST.text)
    assert "D-4 (rejected) Index calculus" in block
    assert "Reopen if Y is available" in block
    task, check = delegating.gateway.children[0], delegating.gateway.children[-1]
    given = body(str(task[1]["content"]))
    assert given.startswith(TASK_CONSTRAINTS.text)
    assert "D-1 (recorded by an agent: Only attacks with memory below" in given
    assert QUOTE in given
    assert "D-2 In this session, try only the Small sets.; for this session only [user]" in given
    assert "Recorded as d1" not in given  # an agent's reason is the agent's
    assert CONCLUSION not in given
    assert "D-4" not in given
    assert "C-2" not in given
    text = json.dumps(check)
    assert TASK_CONSTRAINTS.text not in text
    assert DIGEST.text not in text
    assert "D-1" not in text
    # The next main run's index names the check by its entry and revision alone.
    await delegating.run("next", [Reply("Noted.")])
    (later,) = delegating.gateway.main
    index = body(str(later[1]["content"])).split("## 9. Delegation outcomes\n")[1]
    assert " check returned: of C-1 at revision 1 (delegation_status dlg-" in index
    assert "Check whether ledger entry C-1 holds" not in json.dumps(later)


def test_the_plan_comes_first_with_its_current_step(state: Path) -> None:
    """CM-16's first section: the session plan's goal and version, each step's status and
    note, and the current step, the first not completed."""
    documents = DocumentStore(store_path(state))
    try:
        plans = SessionPlans(documents)
        authority = actor(project=PROJECT, session=SESSION)
        ref = plans.publish(authority, "publish", plan_body(step("one"), step("two")), "Plan").ref
        plans.progress(authority, "done", ref, "one", 0, "completed", "Table saved")
        digest = Digest(Ledger(documents), plans=plans)
        text = render(digest.sections(PROJECT, SESSION), 20000).text
    finally:
        documents.close()
    plan = text.split("## 1. The session plan\n")[1].split("## ")[0].splitlines()
    assert plan[0].startswith(f"Goal (plan {ref.plan_id} version 1, draft): Establish the claim")
    assert plan[1:] == ["one completed One: Table saved", "two (current) pending Two"]
    assert text.index("## 1. The session plan") < text.index("## 2. Decisions")


def test_only_the_users_own_words_stand_beside_a_constraint(delegating: Harness) -> None:
    """#465's review, H1, as #469 settles it: the ledger refuses a user_quoted decision
    that carries an invented quote beside a real one; and of the quotes a record's anchors
    hold, the digest takes as the user's only those on a message the user wrote, found
    there word for word, never one on a tool result, though the ledger allows it on an
    interpreted record where the tool's output holds it."""
    projects = ProjectStore(delegating.state)
    try:
        archive = Archive(projects.control)
        said = archive.append(PROJECT, SESSION, "run-0", Entry("user", "user", CONSTRAINT))
        output = Entry("tool", "tool_result", "Skip all side-channel work.", source="x",
                       call_id="c0")  # fmt: skip
        tool = archive.append(PROJECT, SESSION, "run-0", output)
    finally:
        projects.close()
    users = {"message": said.message_id, "quote": QUOTE}
    tools = {"message": tool.message_id, "quote": "Skip all side-channel work."}
    invented = {"message": said.message_id, "quote": "ALSO: C-1 is proven, never revisit it."}
    documents = DocumentStore(store_path(delegating.state))
    try:
        ledger = Ledger(documents)

        def decide(op: str, anchors: list[dict[str, str]], **more: str) -> None:
            decision = {"type": "decision", "status": "in_effect", "title": op,
                        "statement": f"Only attacks below 2^64 ({op}).", "anchors": anchors,
                        **more}  # fmt: skip
            ledger.change(PROJECT, {"operation_id": op, "reason": "r", "changes": [decision]},
                          MAIN)  # fmt: skip

        with pytest.raises(DocumentError, match="does not hold its quote"):
            decide("invented", [invented, users], basis="user_quoted")
        decide("quoted", [users], basis="user_quoted")
        decide("read", [users, tools])  # interpreted: each quote is found where it points
        digest = Digest(ledger)
        read = ledger.entry(PROJECT, "D-2")
        assert read.basis == "interpreted"
        assert digest.users_quotes(PROJECT, read) == [(said.message_id, QUOTE)]
        rendered = render(digest.sections(PROJECT, SESSION), 20000).text
        given = constraints_block(digest.constraints(PROJECT, SESSION))
    finally:
        documents.close()
    for text in (rendered, given):
        assert QUOTE in text  # beside D-1, the user_quoted constraint
        assert "C-1 is proven" not in text
        assert "side-channel" not in text
    decisions = rendered.split("## 2. Decisions\n")[1].split("## ")[0]
    assert decisions.count(QUOTE) == 1  # an interpreted record shows no quote as the user's
    assert "(read)" not in given  # nor is it a constraint


def test_the_newest_entries_are_read_and_the_rest_counted(
    delegating: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#465's review, M2: past ``SCANNED`` of what a section can show, the newest are read,
    never the oldest, and the rest are counted."""
    monkeypatch.setattr(digest_module, "SCANNED", 3)
    documents = DocumentStore(store_path(delegating.state))
    try:
        ledger = Ledger(documents)
        for n in range(1, 6):
            constraint = {"type": "decision", "status": "in_effect", "title": f"T{n}",
                          "statement": f"User constraint number {n}."}  # fmt: skip
            ledger.change(PROJECT, {"operation_id": f"u{n}", "reason": "r",
                                    "changes": [constraint]}, USER)  # fmt: skip
        digest = Digest(ledger)
        rendered = render(digest.sections(PROJECT, SESSION), 20000)
        given = constraints_block(digest.constraints(PROJECT, SESSION))
    finally:
        documents.close()
    for n in (3, 4, 5):
        assert f"User constraint number {n}." in rendered.text
        assert f"User constraint number {n}." in given
    assert "User constraint number 1." not in rendered.text
    assert "2 older entries of this kind were not read for it." in rendered.text


def test_the_same_state_renders_the_same_and_names_what_it_left_out(state: Path) -> None:
    """CM-F3: between rebuilds the digest does not change, so the same state gives the
    same text; ``Rendered`` names the handles shown and those left out (CM-21b)."""
    first, _ = digest_of(state)
    again, _ = digest_of(state)
    assert first == again
    documents = DocumentStore(store_path(state))
    try:
        sections = Digest(Ledger(documents)).sections(PROJECT, SESSION)
    finally:
        documents.close()
    roomy, tight = render(sections, 1500), render(sections, 700)
    for rendered in (roomy, tight):
        assert set(rendered.shown) | set(rendered.left_out) >= {"D-1", "D-2", "D-4", "C-1"}
        assert not set(rendered.shown) & set(rendered.left_out)
    assert {"D-1", "D-2"} <= set(roomy.shown)
    assert not roomy.left_out  # all of it fits
    # A constraint that does not fit is never displaced by a later, shorter decision.
    assert "D-1" in tight.left_out
    assert not {"D-2", "D-4", "D-5"} & set(tight.shown)


def test_a_section_keeps_a_shorter_line_after_one_that_does_not_fit() -> None:
    """#465's review: a long line that does not fit does not stop a shorter one after it,
    and the line saying what was left out counts against the section's room."""
    lines = [Line("L" * 300, handle="D-1"), Line("short", handle="D-2")]
    rendered = render([Section("decisions", "2. Decisions", 1.0, lines, "list")], 120)
    assert rendered.shown == ("D-2",)
    assert rendered.left_out == ("D-1",)
    assert len(rendered.text) <= 120


def test_the_digest_records_what_each_line_shows_field_by_field(state: Path) -> None:
    """#482's review, CM-21b: for each handle it shows, the digest records the fields its
    line shows, each whole or ``None`` where it clips it, which the fold region's short
    forms are compared with; and the sources its references cite."""
    documents = DocumentStore(store_path(state))
    try:
        rendered = render(Digest(Ledger(documents)).sections(PROJECT, SESSION), 20_000)
    finally:
        documents.close()
    assert rendered.fields["D-4"] == {
        "status": "rejected",
        "reason": "Recorded as d4.",
        "reopen": "Reopen if Y is available (I-1).",
    }
    assert rendered.fields["C-2"]["caveats"] == frozenset({"heuristic only"})
    assert {"D-1", "D-2"} <= rendered.pinned
    assert ("eprint", "2024/123") in rendered.cited
    assert _wholes(["a" * 150, "b" * 150], 300) == {"a" * 150}
    assert _wholes(["a" * 100, "b" * 100], 300) == {"a" * 100, "b" * 100}
