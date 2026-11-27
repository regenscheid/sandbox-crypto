# SPDX-License-Identifier: MIT
"""Every review is given what the user said, answers to the agent's questions included,
and a response may rest on the user's word wherever it sits (CR-42, CR-48 as amended)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.consistency.controller import ReviewController
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import REQUEST_BYTES
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.context.history import Archive
from sandbox_science.orchestrator.context.history.archive import Archived, Entry
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.questions import Answer, Questions

from .test_consistency import MAIN, claim, context_for, dispatch
from .test_consistency import store as store

RUN = "author-1"
QUESTION = "The paper prints 673 but the formula gives 637. Which gate should I apply?"
OPTIONS = [
    {
        "label": "Treat 673 as a source typo; proceed",
        "detail": "Keep the printed value beside the measured 637.",
        "recommended": True,
    },
    {"label": "Keep the literal gate; stop"},
]


def ask_and_pick(reviews: ReviewStore, call: str = "call-ask") -> str:
    """The agent asks; the user picks the recommended option. The question's ID."""
    questions = Questions(reviews.documents.control)
    asked = questions.ask(context_for(reviews), RUN, call, f"op-{call}", QUESTION, OPTIONS)
    questions.answer(
        "p", "s", asked.question_id, Answer(kind="picked", picked=(OPTIONS[0]["label"],))
    )
    return asked.question_id


def archived(reviews: ReviewStore, entry: Entry) -> Archived:
    """``entry`` kept in the archive and observed into the review feed as a transcript
    item that names its archive message."""
    message = Archive(reviews.documents.control).append("p", "s", RUN, entry)
    reviews.observe(
        "p",
        "s",
        f"transcript:{message.message_id}",
        "transcript",
        {"run": RUN, "archive": {"id": message.message_id, "position": message.position}},
        {"role": entry.role, "content": entry.content},
    )
    return message


def request(reviews: ReviewStore, review: str) -> dict[str, Any]:
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    (found,) = [i for i in items if i["kind"] == "request"]
    return reviews.evidence("p", "s", review, found["evidence"])["payload"]


def test_every_review_is_given_the_users_answers_with_their_questions(store) -> None:
    """A checkpoint's request item lists the answer among the user's messages, in time
    order: the question, its options with the recommended one marked, and the pick."""
    reviews, _, now = store
    from .test_review_final import _post

    first = new_id("msg")
    _post(reviews, first, "Reproduce Table 1, then sweep SNOVA.")
    question = ask_and_pick(reviews)
    claim(reviews, "Proceeding with the one documented exception.", "c1")
    asked = request(reviews, dispatch(reviews, "checkpoint"))
    assert [m.get("message") for m in asked["messages"]] == [first, None]
    answer = asked["messages"][1]
    assert answer["answer_to"] == question
    assert (answer["question"], answer["answer"]) == (QUESTION, "picked")
    assert answer["picked"] == [OPTIONS[0]["label"]]
    assert answer["options"][0]["recommended"] is True
    assert answer["options"][0]["detail"] == OPTIONS[0]["detail"]
    assert (asked["omitted"], asked["omitted_answers"]) == (0, 0)
    assert "answer_to" in asked["about"]
    del now


def test_a_long_request_keeps_the_newest_entries_and_says_what_it_left_out(store) -> None:
    """Within ``REQUEST_BYTES`` the first message and the newest entries are kept; how
    many messages and answers were left out is said."""
    reviews, _, _ = store
    from .test_review_final import _post

    first = new_id("msg")
    _post(reviews, first, "The task.")
    ask_and_pick(reviews, "call-old")
    for n in range(5):
        _post(reviews, new_id("msg"), f"Message {n}: " + "x" * (REQUEST_BYTES // 4))
    newest = ask_and_pick(reviews, "call-new")
    claim(reviews)
    asked = request(reviews, dispatch(reviews, "checkpoint"))
    listed = asked["messages"]
    assert listed[0]["message"] == first
    assert listed[-1]["answer_to"] == newest
    assert asked["omitted_answers"] == 1
    assert asked["omitted"] > asked["omitted_answers"]


def test_a_session_with_only_an_answer_still_has_its_request(store) -> None:
    reviews, _, _ = store
    question = ask_and_pick(reviews)
    claim(reviews)
    asked = request(reviews, dispatch(reviews, "checkpoint"))
    assert [m["answer_to"] for m in asked["messages"]] == [question]


def _held_finding(
    reviews: ReviewStore,
) -> tuple[dict[str, Any], Archived, Archived, Archived]:
    """A checkpoint holds the agent's statement and a later agent message, but not the
    message between them, which the feed never brought it; its finding is anchored to
    the statement. The finding, and the statement's, the skipped and the later
    message's archive records."""
    statement = archived(
        reviews, Entry("assistant", "assistant", "The user approved the 673 exception.")
    )
    skipped = Archive(reviews.documents.control).append(
        "p", "s", RUN, Entry("assistant", "assistant", "I correct the earlier claim.")
    )
    later = archived(reviews, Entry("assistant", "assistant", "Continuing the sweep."))
    review = dispatch(reviews, "checkpoint")
    items = reviews.manifest("p", "s", review)["evidence"]
    (anchored,) = [
        i
        for i in items
        if i["kind"] == "transcript"
        and i["locator"].get("archive", {}).get("id") == statement.message_id
    ]
    finding = Findings(reviews).create(
        "p",
        "s",
        review,
        "owner",
        {
            "severity": "fail",
            "category": "evidence_strength",
            "statement": "No record shows the user approved the exception.",
            "anchors": [{"evidence": anchored["evidence"]}],
            "operation_id": "finding-1",
        },
    )
    reviews.finish("p", "s", review, "owner", "findings", "One finding.")
    return finding, statement, skipped, later


def test_a_fix_may_cite_the_users_answer_whatever_its_place(store) -> None:
    """The tool result that carried the user's pick, recorded before the reviewed
    statement and outside the review's snapshot, grounds a fix; it is queued for the
    re-check, which is given it and the request item that holds the answer."""
    reviews, _, _ = store
    question = ask_and_pick(reviews)
    answer = Archive(reviews.documents.control).append(
        "p",
        "s",
        RUN,
        Entry("tool", "tool_result", json.dumps({"picked": [OPTIONS[0]["label"]]}),
              source="ask_user", call_id="call-ask"),
    )  # fmt: skip
    finding, *_ = _held_finding(reviews)
    result = Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding["finding_id"],
            "expected": finding["revision"],
            "action": "fix",
            "explanation": "The user picked the recommended option (the answer cited).",
            "messages": [answer.message_id],
            "operation_id": "fix-1",
        },
        MAIN,
    )
    recheck = Findings(reviews).get("p", "s", finding["finding_id"])["recheck"]
    assert result["status"] == "open"
    assert [(m["id"], m.get("ground")) for m in recheck["messages"]] == [
        (answer.message_id, "answer")
    ]
    confirmation = dispatch(reviews, "confirmation")
    items = reviews.manifest("p", "s", confirmation, limit=50)["evidence"]
    assert any(i["locator"].get("archive", {}).get("id") == answer.message_id for i in items)
    assert request(reviews, confirmation)["messages"][0]["answer_to"] == question


def test_a_fix_may_cite_a_message_its_review_was_not_given(store) -> None:
    """What the review held is what it was given, not how far the feed reached: a later
    message in a range it skipped grounds a fix; one it held does not, and the refusal
    says to dismiss the finding and cite the message instead."""
    reviews, _, _ = store
    finding, _, skipped, later = _held_finding(reviews)
    findings = Findings(reviews)
    with pytest.raises(DocumentError, match="dismiss the finding") as refused:
        findings.respond(
            "p",
            "s",
            {
                "finding": finding["finding_id"],
                "expected": finding["revision"],
                "action": "fix",
                "explanation": "Corrected later.",
                "messages": [later.message_id],
                "operation_id": "fix-held",
            },
            MAIN,
        )
    assert "was among what the finding's review held" in refused.value.message
    # A later message the review was not given, though it held one after it.
    assert skipped.position < later.position
    result = findings.respond(
        "p",
        "s",
        {
            "finding": finding["finding_id"],
            "expected": finding["revision"],
            "action": "fix",
            "explanation": "Corrected in the message cited.",
            "messages": [skipped.message_id],
            "operation_id": "fix-skipped",
        },
        MAIN,
    )
    assert result["status"] == "open"


def test_a_conflict_names_the_findings_revision_and_status(store) -> None:
    reviews, _, _ = store
    finding, *_ = _held_finding(reviews)
    with pytest.raises(DocumentError) as refused:
        Findings(reviews).respond(
            "p",
            "s",
            {
                "finding": finding["finding_id"],
                "expected": finding["revision"] + 1,
                "action": "dismiss",
                "explanation": "The user approved it.",
                "operation_id": "dismiss-stale",
            },
            MAIN,
        )
    revision = finding["revision"]
    assert refused.value.kind == "conflict"
    assert f"it is now at revision {revision}, open" in refused.value.message
    assert f"answer that revision (expected {revision})" in refused.value.message
    assert refused.value.details == {"revision": revision, "status": "open"}


def test_the_recheck_of_a_dismissal_is_given_the_users_answer(store) -> None:
    """The next review decides the author's dismissal that cites the user's answer with
    that answer in its request item, so it can accept it (CR-43)."""
    reviews, _, _ = store
    question = ask_and_pick(reviews)
    finding, *_ = _held_finding(reviews)
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding["finding_id"],
            "expected": finding["revision"],
            "action": "dismiss",
            "explanation": "The user picked the recommended option, which approves it.",
            "operation_id": "dismiss-1",
        },
        MAIN,
    )
    claim(reviews, "The sweep continues.", "c2")
    review = dispatch(reviews, "checkpoint")
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    held = [i for i in items if i["kind"] == "finding"]
    assert [i["locator"]["finding"] for i in held] == [finding["finding_id"]]
    assert request(reviews, review)["messages"][0]["answer_to"] == question


@pytest.mark.parametrize(("args", "queued"), [({}, True), ({"": ""}, True), ({"x": 1}, False)])
async def test_request_review_takes_no_arguments(store, args: dict[str, Any], queued: bool) -> None:
    """An empty-keyed placeholder, which some models send for a tool without arguments,
    is ignored; any other argument is refused with what to send instead."""
    reviews, _, _ = store
    controller = ReviewController(
        reviews, context_for(reviews), run_id=RUN, model=None, alias="reviewer"
    )
    if queued:
        encoded, _ = await controller.author_call("request_review", args, "op-1")
        assert json.loads(encoded)["queued"] is True
    else:
        with pytest.raises(DocumentError, match=r"takes no arguments: call it with \{\}"):
            await controller.author_call("request_review", args, "op-1")
