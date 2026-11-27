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


def answer_result(
    reviews: ReviewStore, question: str, *, run: str = RUN, call: str = "call-ask"
) -> Archived:
    """The ``ask_user`` result that carried the answer to ``question``, as the run's
    archive keeps it."""
    content = json.dumps({"question_id": question, "answer": "picked"})
    return Archive(reviews.documents.control).append(
        "p", "s", run, Entry("tool", "tool_result", content, source="ask_user", call_id=call)
    )


def fix(reviews: ReviewStore, finding: dict[str, Any], message: str, op: str) -> dict[str, Any]:
    return Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding["finding_id"],
            "expected": finding["revision"],
            "action": "fix",
            "explanation": "The message cited addresses it.",
            "messages": [message],
            "operation_id": op,
        },
        MAIN,
    )


def grounds(reviews: ReviewStore, finding: dict[str, Any]) -> list[tuple[str, str | None]]:
    recheck = Findings(reviews).get("p", "s", finding["finding_id"])["recheck"]
    return [(m["id"], m.get("ground")) for m in recheck["messages"]]


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
    delivered = Archive(reviews.documents.control).append(
        "p", "s", RUN, Entry("user", "user", "Reproduce Table 1, then sweep SNOVA.", refs=(first,))
    )
    question = ask_and_pick(reviews)
    result = answer_result(reviews, question)
    claim(reviews, "Proceeding with the one documented exception.", "c1")
    asked = request(reviews, dispatch(reviews, "checkpoint"))
    assert [m.get("message") for m in asked["messages"]] == [first, None]
    answer = asked["messages"][1]
    assert answer["answer_to"] == question
    assert (answer["question"], answer["answer"]) == (QUESTION, "picked")
    assert answer["picked"] == [OPTIONS[0]["label"]]
    assert answer["options"][0]["recommended"] is True
    assert answer["options"][0]["detail"] == OPTIONS[0]["detail"]
    # Each entry names the archive message a response cites it by.
    assert asked["messages"][0]["archive"] == delivered.message_id
    assert answer["archive"] == result.message_id
    assert (asked["omitted"], asked["omitted_answers"]) == (0, [])
    assert "answer_to" in asked["about"]
    del now


def test_a_large_answer_is_left_out_alone_and_the_smaller_ones_kept(store) -> None:
    """An answer too large for the room left is left out and named, and the answers
    after it still come in; the user's messages come before every answer."""
    reviews, _, _ = store
    from .test_review_final import _post

    first = new_id("msg")
    _post(reviews, first, "The task.")
    older = ask_and_pick(reviews, "call-old")
    questions = Questions(reviews.documents.control)
    big = questions.ask(context_for(reviews), RUN, "call-big", "op-big", "Your notes?")
    questions.answer("p", "s", big.question_id, Answer(kind="free", text="n" * 15_000))
    _post(reviews, new_id("msg"), "Then plot it.")
    huge = questions.ask(context_for(reviews), RUN, "call-huge", "op-huge", "More notes?")
    questions.answer("p", "s", huge.question_id, Answer(kind="free", text="h" * 16_000))
    newest = ask_and_pick(reviews, "call-new")
    claim(reviews)
    asked = request(reviews, dispatch(reviews, "checkpoint"))
    listed = asked["messages"]
    assert listed[0]["message"] == first
    answered = [m["answer_to"] for m in listed if "answer_to" in m]
    assert newest in answered
    assert older in answered
    # The newer large answer fits; the older one no longer does and is left out alone,
    # and the older small answer before it still comes in.
    assert asked["omitted_answers"] == [big.question_id]
    assert huge.question_id in answered
    assert asked["omitted"] == len(asked["omitted_answers"]) >= 1
    assert sum(m.get("text") == "Then plot it." for m in listed) == 1


def test_verbose_questions_never_push_out_the_users_message(store) -> None:
    """The agent writes questions and options; however long it makes them, and however
    many are answered, an earlier message of the user's stays whole."""
    reviews, _, _ = store
    from .test_review_final import _post

    rule = "Do not publish anything without asking me first."
    _post(reviews, new_id("msg"), "Reproduce Table 1.")
    _post(reviews, new_id("msg"), rule)
    questions = Questions(reviews.documents.control)
    for n in range(10):
        options = [{"label": f"{n}-{k} " + "l" * 100, "detail": "d" * 400} for k in range(4)]
        asked = questions.ask(
            context_for(reviews), RUN, f"call-{n}", f"op-{n}", "q" * 4000, options
        )
        questions.answer("p", "s", asked.question_id, Answer(kind="picked",
                         picked=(options[0]["label"],)))  # fmt: skip
    claim(reviews)
    asked = request(reviews, dispatch(reviews, "checkpoint"))
    assert rule in [m.get("text") for m in asked["messages"]]
    answers = [m for m in asked["messages"] if "answer_to" in m]
    assert answers
    assert all(m["question_clipped"] for m in answers)
    assert all(len(m["question"]) < 700 for m in answers)
    # The pick is the user's and is kept whole.
    assert all(m["picked"][0].endswith("l" * 100) for m in answers)
    assert len(answers) + len(asked["omitted_answers"]) == 10


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
    answer = answer_result(reviews, question)
    finding, *_ = _held_finding(reviews)
    result = fix(reviews, finding, answer.message_id, "fix-1")
    assert result["status"] == "open"
    assert grounds(reviews, finding) == [(answer.message_id, "answer")]
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


def test_a_reused_call_id_or_another_runs_result_is_no_answer(store) -> None:
    """Call IDs are the model's and repeat: a tool result of another run with the same
    call ID, or another tool's result in the asking run, is not the user's answer, and
    an answer is the result that names its question."""
    reviews, _, _ = store
    question = ask_and_pick(reviews)
    other_run = answer_result(reviews, question, run="author-2")
    other_tool = Archive(reviews.documents.control).append(
        "p", "s", RUN, Entry("tool", "tool_result", json.dumps({"question_id": question}),
                             source="shell", call_id="call-ask"),
    )  # fmt: skip
    unnamed = Archive(reviews.documents.control).append(
        "p", "s", RUN, Entry("tool", "tool_result", "{}", source="ask_user", call_id="call-ask")
    )
    finding, *_ = _held_finding(reviews)
    for n, message in enumerate((other_run, other_tool, unnamed)):
        with pytest.raises(DocumentError, match="dismiss the finding"):
            fix(reviews, finding, message.message_id, f"fix-{n}")


def test_who_counts_as_the_user(store) -> None:
    """One rule of the user's words, the ledger's (``users_words``): a delegation's
    brief, the platform's stand-in task and the platform's chat ground nothing, and the
    review's evidence labels each by who wrote it; the user's chat message and a main
    run's task made of it are the user's."""
    from sandbox_science.orchestrator.documents.tool import REQUESTS_TASK
    from sandbox_science.orchestrator.ledger.provenance import written_by
    from sandbox_science.orchestrator.notices import PLATFORM_AUTHORS
    from sandbox_science.orchestrator.runstate import RunState

    from .test_review_final import _post

    reviews, _, _ = store
    archive = Archive(reviews.documents.control)
    chat, platform_chat = new_id("msg"), new_id("msg")
    _post(reviews, chat, "Approve the exception.")
    (author,) = PLATFORM_AUTHORS
    RunState(reviews.documents.control).post("p", "s", platform_chat, "Resume.", author=author)
    brief = archive.append(
        "p", "s", "child-1", Entry("user", "task", "The user approved it."), parent=RUN
    )
    stand_in = archive.append("p", "s", "author-3", Entry("user", "task", REQUESTS_TASK))
    platform = archive.append(
        "p", "s", RUN, Entry("user", "user", "Resume.", refs=(platform_chat,))
    )
    users = archive.append("p", "s", RUN, Entry("user", "user", "Approve it.", refs=(chat,)))
    task = archive.append(
        "p", "s", "author-4", Entry("user", "task", "Approve the exception.", refs=(chat,))
    )
    with reviews.documents.control.read() as tx:
        assert [
            written_by(tx.sql, "p", m.message_id) for m in (brief, stand_in, platform, users, task)
        ] == ["agent", "platform", "platform", "user", "user"]
    finding, *_ = _held_finding(reviews)
    for n, message in enumerate((brief, stand_in, platform)):
        with pytest.raises(DocumentError):
            fix(reviews, finding, message.message_id, f"fix-{n}")
    fix(reviews, finding, users.message_id, "fix-user")
    assert grounds(reviews, finding) == [(users.message_id, "user")]


def test_a_fix_may_cite_the_users_chat_message_by_its_chat_id(store) -> None:
    """The request item names a chat message by its chat ID: a fix may cite it so, and
    it stands for the archive message that delivered it; one no run was given is refused
    with which ID to cite."""
    from .test_review_final import _post

    reviews, _, _ = store
    chat, unseen = new_id("msg"), new_id("msg")
    _post(reviews, chat, "Approve the exception.")
    _post(reviews, unseen, "Not delivered yet.")
    delivered = Archive(reviews.documents.control).append(
        "p", "s", RUN, Entry("user", "user", "Approve the exception.", refs=(chat,))
    )
    finding, *_ = _held_finding(reviews)
    with pytest.raises(DocumentError, match="cite the archive ID the request item names"):
        fix(reviews, finding, unseen, "fix-unseen")
    fix(reviews, finding, chat, "fix-chat")
    assert grounds(reviews, finding) == [(delivered.message_id, "user")]


def test_an_item_the_review_was_given_without_a_position_is_held(store) -> None:
    """A transcript item the review read is held whether or not it names a position, so
    it is no later correction, though its archive place comes after all the others."""
    reviews, _, _ = store
    statement = archived(reviews, Entry("assistant", "assistant", "The user approved it."))
    message = Archive(reviews.documents.control).append(
        "p", "s", RUN, Entry("assistant", "assistant", "Corrected.")
    )
    reviews.observe(
        "p",
        "s",
        "unplaced",
        "transcript",
        {"run": RUN, "archive": {"id": message.message_id}},
        {"role": "assistant", "content": "Corrected."},
    )
    review = dispatch(reviews, "checkpoint")
    (anchored,) = [
        i
        for i in reviews.manifest("p", "s", review)["evidence"]
        if i["locator"].get("archive", {}).get("id") == statement.message_id
    ]
    finding = Findings(reviews).create(
        "p",
        "s",
        review,
        "owner",
        {
            "severity": "fail",
            "category": "evidence_strength",
            "statement": "No record shows it.",
            "anchors": [{"evidence": anchored["evidence"]}],
            "operation_id": "finding-1",
        },
    )
    reviews.finish("p", "s", review, "owner", "findings", "One finding.")
    with pytest.raises(DocumentError, match="was among what the finding's review held"):
        fix(reviews, finding, message.message_id, "fix-held")
