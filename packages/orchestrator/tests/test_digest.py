# SPDX-License-Identifier: MIT
"""The research-state digest (docs/specs/context-management.md, CM-16, CM-26, CM-37a):
CM-F3, CM-F22 and CM-F32 as far as the digest goes, its sections and shares, the ledger's
labels as the digest shows them, and the state blocks of main and delegated runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.digest import Digest, Line, Section, render
from sandbox_science.orchestrator.documents import USER, Author, DocumentStore, store_path
from sandbox_science.orchestrator.history import Archive, Entry, body
from sandbox_science.orchestrator.history.reads import Read
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
def state(delegating: Harness) -> Path:
    """The delegating harness's state, with C-1 and the research state above."""
    research_state(delegating.state)
    return delegating.state


def digest_of(state: Path, budget: int = 20000) -> tuple[str, Digest]:
    documents = DocumentStore(store_path(state))
    try:
        digest = Digest(Ledger(documents))
        return render(digest.sections(PROJECT, SESSION), budget), digest
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
    assert decisions[1].startswith("  the user's own words, copied by the platform (msg-")
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
    assert findings[0].startswith("C-1 (")
    assert findings[1].startswith("[claimed, not established: not an impossibility] C-2 (")
    assert "caveats: heuristic only" in findings[1]
    assert "impossible" not in findings[1].split("]", 1)[1]
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
    alone = render([empty, *many], 1000)
    later = Section("questions", "3. Questions", 0.6, [Line("I" * 50)])
    shared = render([empty, *many, later], 1000)
    assert alone.count("D" * 50) == 7  # its 30% and the empty plan's 10%
    assert shared.count("D" * 50) > 7  # and what questions left at the end


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
    assert "D-1 (in_effect)" in given
    assert QUOTE in given
    assert "D-2 (in_effect)" in given
    assert CONCLUSION not in given
    assert "D-4" not in given
    assert "C-2" not in given
    text = json.dumps(check)
    assert TASK_CONSTRAINTS.text not in text
    assert DIGEST.text not in text
    assert "D-1" not in text


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
        text = render(digest.sections(PROJECT, SESSION), 20000)
    finally:
        documents.close()
    plan = text.split("## 1. The session plan\n")[1].split("## ")[0].splitlines()
    assert plan[0].startswith(f"Goal (plan {ref.plan_id} version 1, draft): Establish the claim")
    assert plan[1:] == ["one completed One: Table saved", "two (current) pending Two"]
    assert text.index("## 1. The session plan") < text.index("## 2. Decisions")
