# SPDX-License-Identifier: MIT
"""The owner's decision of 2026-10-16 after context-management lab round 1 (CM-P1): every
session declined a rejected approach with the right reason and never cited the decision's
handle (D-3). Kept as a prompt, and added as a reviewer check, which is only a suggestion
(docs/specs/consistency-review.md, CR-4 and CR-18 as amended; docs/specs/
context-management.md, decided question 12).

A. The main agent's and a delegated task's prompts ask for the handle, in fixed text.
B. The reviewer's rubric asks for a ``handle_citation`` finding, which is a ``note``: it
   is not counted, offered, re-checked, fixed or dismissed by the author, never reaches
   its re-flag bound, and does not stop a pass.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import (
    ADVISORY_CATEGORIES,
    Category,
    ReviewConfig,
)
from sandbox_science.orchestrator.consistency.notices import FindingNotices
from sandbox_science.orchestrator.consistency.rubric import RUBRIC, RUBRIC_VERSION
from sandbox_science.orchestrator.consistency.runner import SPECS, ReviewerRunner
from sandbox_science.orchestrator.delegation.prompt import CHECK_PROMPT, TASK_PROMPT, TASK_VERSION
from sandbox_science.orchestrator.documents import USER, DocumentError
from sandbox_science.orchestrator.loop import MAIN_VERSION, SYSTEM_PROMPT, run_task
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .scripted import ScriptedModel
from .test_consistency import (
    MAIN,
    anchor,
    claim,
    dispatch,
    raise_finding,
    reviewed_document,
    revise,
)
from .test_consistency import store as store

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
CITE = "cite the entry's handle, such as D-3, A-1, C-4 or I-3."
STATEMENT = "The answer declines the Weil descent because of D-3 but does not name it."


def advisory(reviews: Any, review: str, **overrides: Any) -> dict[str, Any]:
    fields = {
        "severity": "note",
        "category": "handle_citation",
        "statement": STATEMENT,
        "remedy": "Cite D-3.",
        "operation_id": "cite-1",
        **overrides,
    }
    return raise_finding(reviews, review, **fields)


# A. The prompts.


def test_the_main_and_task_prompts_ask_for_the_handle_in_fixed_text() -> None:
    assert (MAIN_VERSION, TASK_VERSION) == ("sandbox-main-19", "sandbox-delegated-task-17")

    assert SYSTEM_PROMPT.count(CITE) == 1
    assert "relies on a ledger entry, or declines or reopens something because of one" in (
        " ".join(SYSTEM_PROMPT.split())
    )
    assert TASK_PROMPT.count(CITE) == 1
    # A check names its entry's handle in its brief, and answers about that one entry.
    assert CITE not in CHECK_PROMPT


@pytest.mark.asyncio
async def test_the_instruction_is_in_the_system_message_of_every_run_and_does_not_vary() -> None:
    sent = []
    for run in ("run-1", "run-2"):
        model = ScriptedModel.from_json([{"text": "Done."}])
        async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
            await run_task(model, Toolbox(client, CONTEXT), "Task.", run_id=run, max_turns=1)
        system = next(m for m in model.requests[0] if m["role"] == "system")
        assert CITE in str(system["content"])
        sent.append(system["content"])
    # Prompt-cache stability (CM-32): nothing about the instruction depends on the call.
    assert sent[0] == sent[1]


# B. The reviewer's suggestion.


def test_the_rubric_and_its_tool_ask_for_a_note_of_the_category() -> None:
    assert RUBRIC_VERSION == "sandbox-consistency-9"
    assert "handle_citation" in RUBRIC
    assert "severity note" in RUBRIC
    assert "It is a suggestion only" in RUBRIC
    finding = next(spec for spec in SPECS if spec.name == "review_finding")
    assert "handle_citation" in finding.parameters["properties"]["category"]["enum"]
    assert ADVISORY_CATEGORIES == ("handle_citation",)


def test_it_is_stored_as_a_note_whatever_the_reviewer_called_it(store) -> None:
    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    category: Category = "handle_citation"
    found = advisory(reviews, review, severity="fail", category=category)
    current = Findings(reviews).get("p", "s", found["finding_id"])
    assert (current["severity"], current["category"], current["status"]) == (
        "note",
        "handle_citation",
        "open",
    )
    assert current["remedy"] == "Cite D-3."


def test_it_is_not_counted_offered_or_a_reason_to_hold(store) -> None:
    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    advisory(reviews, review)
    # Not among the unresolved findings that make a conversation turn a terminal one,
    # nor in the counts a run is told; and listed for the user and the author's tool.
    assert reviews.finding_states("p", "s") == {}
    notices = FindingNotices(reviews)
    assert notices.counts("p", "s") == {}
    assert len(Findings(reviews).listing("p", "s")["findings"]) == 1
    # Not offered as a notice, even where a project interrupts on notes.
    reviews.configure("p", "s", ReviewConfig(interrupt_severity="note"))
    again = advisory(reviews, review, operation_id="cite-2")
    assert again["status"] == "open"
    assert notices.pending("p", "s", "author-1", set()) == []
    # While an ordinary note beside it is counted and offered, as before.
    raise_finding(reviews, review, severity="note", statement="A plain note.")
    assert notices.counts("p", "s") == {"note:open": 1}
    offered = notices.pending("p", "s", "author-1", set())
    assert [n["statement"] for n in offered] == ["A plain note."]


def test_it_takes_no_fix_or_dismissal_from_the_author_and_is_never_rechecked(store) -> None:
    reviews, _, _ = store
    claim(reviews)
    doc, review, anchors = reviewed_document(reviews)
    found = advisory(reviews, review, anchors=anchors)
    # A fix names a later revision of what the suggestion reviewed, and is still refused.
    fixed = revise(reviews, doc, "The answer declines the Weil descent because of D-3.\n")
    for action in ("fix", "dismiss"):
        with pytest.raises(DocumentError, match="needs no fix or dismissal"):
            Findings(reviews).respond(
                "p",
                "s",
                {
                    "finding": found["finding_id"],
                    "expected": 1,
                    "action": action,
                    "explanation": "Named it now.",
                    **(
                        {"revisions": fixed}
                        if action == "fix"
                        else {"evidence": [anchor(reviews, review)]}
                    ),
                    "operation_id": f"respond-{action}",
                },
                MAIN,
            )
    assert reviews.confirmation_targets("p", "s") == []
    # The user may still dismiss it.
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": found["finding_id"],
            "expected": 1,
            "action": "dismiss",
            "explanation": "Not needed here.",
            "operation_id": "dismiss",
        },
        USER,
    )
    dismissed = Findings(reviews).get("p", "s", found["finding_id"])
    assert (dismissed["status"], dismissed["dismissal"]["by"]) == ("dismissed", "user")


def test_repeated_flags_never_put_it_at_its_bound(store) -> None:
    reviews, _, _ = store
    reviews.configure("p", "s", ReviewConfig(max_reflags=1))
    claim(reviews)
    review = dispatch(reviews)
    first = advisory(reviews, review)
    again = advisory(reviews, review, operation_id="cite-2", statement="Still no handle named.")
    assert again["finding_id"] == first["finding_id"]
    current = Findings(reviews).get("p", "s", first["finding_id"])
    assert (current["status"], current["at_bound"]) == ("open", False)
    # An ordinary finding at the same limit stays open at its re-flag bound.
    other = raise_finding(reviews, review, operation_id="other-1")
    raise_finding(reviews, review, operation_id="other-2", statement="Still wrong.")
    bounded = Findings(reviews).get("p", "s", other["finding_id"])
    assert (bounded["status"], bounded["at_bound"]) == ("open", True)


def suggestion_call(evidence: str, operation: str = "cite-1") -> dict[str, Any]:
    return {
        "name": "review_finding",
        "arguments": {
            "severity": "note",
            "category": "handle_citation",
            "statement": STATEMENT,
            "anchors": [{"evidence": evidence}],
            "remedy": "Cite D-3.",
            "operation_id": operation,
        },
    }


async def review_with(reviews: Any, ordinary: bool = False) -> str:
    """A review whose scripted reviewer reads, records the suggestion (and, if asked, an
    ordinary finding) and finishes ``passed``."""
    claim(reviews)
    review = dispatch(reviews)
    evidence = anchor(reviews, review)["evidence"]
    checked = {
        "name": "review_checked",
        "arguments": {"statement": "The transcript is present.", "evidence": [evidence]},
    }
    wrong = {
        "name": "review_finding",
        "arguments": {
            "severity": "fail",
            "category": "contradicting_value",
            "statement": "The output records 15, not 16.",
            "anchors": [{"evidence": evidence}],
            "operation_id": "ordinary",
        },
    }
    model = ScriptedModel.from_json(
        [
            {"tool_calls": [{"name": "review_read", "arguments": {"evidence": evidence}}]},
            {"tool_calls": [checked, suggestion_call(evidence), *([wrong] if ordinary else [])]},
            {
                "text": json.dumps(
                    {"outcome": "passed", "summary": "Nothing inconsistent.", "cap_reached": False}
                )
            },
        ]
    )
    await ReviewerRunner(reviews, "p", "s", review, "owner", model).run()
    return review


@pytest.mark.asyncio
async def test_a_review_with_only_the_suggestion_still_passes(store) -> None:
    reviews, _, _ = store
    review = await review_with(reviews)
    assert reviews.run("p", "s", review)["state"] == "passed"
    (listed,) = Findings(reviews).listing("p", "s")["findings"]
    assert (listed["category"], listed["severity"]) == ("handle_citation", "note")


@pytest.mark.asyncio
async def test_an_ordinary_finding_beside_it_still_ends_the_review_with_findings(store) -> None:
    reviews, _, _ = store
    review = await review_with(reviews, ordinary=True)
    assert reviews.run("p", "s", review)["state"] == "findings"
    assert reviews.finding_states("p", "s") == {"open": 1}
