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
    assert not {"clipped", "summarised", "grouped"} & set(asked)
    assert "answer_to" in asked["about"]
    del now


def _bound(asked: dict[str, Any]) -> int:
    from sandbox_science.orchestrator.consistency.models import REQUEST_BYTES

    size = len(json.dumps(asked, ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= REQUEST_BYTES
    return size


def test_three_thousand_messages_still_make_a_bounded_request_and_a_review(store) -> None:
    """However many messages the user wrote, the request item stays within its bound,
    the checkpoint starts, the oldest are a range that names its first and last, and the
    reviewer can list and read every one of them."""
    from .test_review_final import _post

    reviews, _, _ = store
    ids = [new_id("msg") for _ in range(3000)]
    for n, message in enumerate(ids):
        _post(reviews, message, f"Note {n}: keep the cells small.")
    claim(reviews)
    review = dispatch(reviews, "checkpoint")
    asked = request(reviews, review)
    _bound(asked)
    group = asked["messages"][0]
    assert (group["first"], group["read_with"]) == (ids[0], "review_request_read")
    assert group["group"] == asked["grouped"] > 0
    assert asked["messages"][-1]["message"] == ids[-1]
    listed = reviews.read_request("p", "s", review, page=0)
    assert [e["id"] for e in listed["entries"]] == ids[:50]
    read = reviews.read_request("p", "s", review, entry=ids[0], owner="owner")
    assert read["text"] == "Note 0: keep the cells small."


def test_a_short_early_instruction_is_never_clipped_before_longer_ones(store) -> None:
    from .test_review_final import _post

    reviews, _, _ = store
    _post(reviews, new_id("msg"), "Reproduce Table 1.")
    _post(reviews, new_id("msg"), "Never use synthetic data.")
    for n in range(200):
        _post(reviews, new_id("msg"), f"Note {n}: " + "n" * 300)
    claim(reviews)
    asked = request(reviews, dispatch(reviews, "checkpoint"))
    _bound(asked)
    texts = [m.get("text") for m in asked["messages"]]
    assert "Never use synthetic data." in texts
    assert all(m.get("clipped") for m in asked["messages"] if len(str(m.get("text"))) > 250)


def test_an_answer_in_the_users_words_is_never_pushed_out_by_later_questions(store) -> None:
    """An answer in the user's own words is a message in effect: forty later questions
    the user left to the agent are summarised first, and it stays whole."""
    reviews, _, _ = store
    questions = Questions(reviews.documents.control)
    first = questions.ask(context_for(reviews), RUN, "call-0", "op-0", "May I publish?")
    words = "No. Never publish without asking me first."
    questions.answer("p", "s", first.question_id, Answer(kind="free", text=words))
    for n in range(1, 41):
        asked = questions.ask(
            context_for(reviews), RUN, f"call-{n}", f"op-{n}", "q" * 4000,
            [{"label": f"Option {k}", "detail": "d" * 400, "recommended": k == 0}
             for k in range(4)],
        )  # fmt: skip
        questions.answer("p", "s", asked.question_id, Answer(kind="decide"))
    claim(reviews)
    asked_item = request(reviews, dispatch(reviews, "checkpoint"))
    _bound(asked_item)
    (kept,) = [m for m in asked_item["messages"] if m.get("answer_to") == first.question_id]
    assert kept["text"] == words
    assert asked_item["summarised"] == 40


def test_the_reviewer_reads_only_the_users_own_entries_whole(store) -> None:
    """``review_request_read`` reads a message the user wrote or an answer with its
    question and options; the platform's chat is not the user's and is refused; each read
    is recorded as one of the request item's."""
    from sandbox_science.orchestrator.notices import PLATFORM_AUTHORS
    from sandbox_science.orchestrator.runstate import RunState

    from .test_review_final import _post

    reviews, _, _ = store
    chat, platform = new_id("msg"), new_id("msg")
    _post(reviews, chat, "x" * 20_000)
    (author,) = PLATFORM_AUTHORS
    RunState(reviews.documents.control).post("p", "s", platform, "Resume.", author=author)
    question = ask_and_pick(reviews)
    claim(reviews)
    review = dispatch(reviews, "checkpoint")
    first = reviews.read_request("p", "s", review, entry=chat, owner="owner")
    assert (len(first["text"]), first["next_start"], first["total_characters"]) == (
        16000,
        16000,
        20000,
    )
    answer = json.loads(reviews.read_request("p", "s", review, entry=question)["text"])
    assert answer["options"][0]["detail"] == OPTIONS[0]["detail"]
    with pytest.raises(DocumentError, match="no message or answer of the user's"):
        reviews.read_request("p", "s", review, entry=platform)
    with reviews.documents.control.read() as tx:
        reads = tx.sql.execute(
            "SELECT r.locator FROM review_reads r JOIN review_evidence e USING(evidence_id) "
            "WHERE r.review_id=? AND e.kind='request'",
            (review,),
        ).fetchall()
    assert [json.loads(row[0])["request_entry"] for row in reads] == [chat]


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


def test_a_recheck_labels_a_cited_brief_and_a_fix_claiming_the_users_word_needs_it(
    store,
) -> None:
    """A later message a fix cites comes into its re-check labelled by who wrote it; a fix
    that says it rests on the user's word but cites no message of the user's is refused."""
    reviews, _, _ = store
    finding, *_ = _held_finding(reviews)
    brief = Archive(reviews.documents.control).append(
        "p", "s", "child-9", Entry("user", "task", "The user approved it."), parent=RUN
    )
    with pytest.raises(DocumentError, match="no message it cites is the user's"):
        Findings(reviews).respond(
            "p",
            "s",
            {
                "finding": finding["finding_id"],
                "expected": finding["revision"],
                "action": "fix",
                "explanation": "The user approved the exception, as the brief says.",
                "messages": [brief.message_id],
                "operation_id": "fix-claims-user",
            },
            MAIN,
        )
    fix(reviews, finding, brief.message_id, "fix-brief")
    confirmation = dispatch(reviews, "confirmation")
    (item,) = [
        i
        for i in reviews.manifest("p", "s", confirmation, limit=50)["evidence"]
        if i["locator"].get("archive", {}).get("id") == brief.message_id
    ]
    payload = reviews.evidence("p", "s", confirmation, item["evidence"])["payload"]
    assert payload["written_by"] == "agent"


async def test_a_review_that_cannot_start_is_a_failed_review_the_summary_shows(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch that raises is recorded as a failed review with why, its work named
    unreviewed (``dispatch_failed``), and the next attempt waits; never a log line only."""
    import sqlite3

    reviews, _, _ = store
    claim(reviews)
    reviews.request("p", "s")

    def refuse(*args: object, **kwargs: object) -> str:
        raise sqlite3.IntegrityError("CHECK constraint failed: length(payload) <= 262144")

    monkeypatch.setattr(reviews, "dispatch", refuse)
    controller = ReviewController(
        reviews, context_for(reviews), run_id=RUN, model=None, alias="reviewer"
    )
    await controller.safe_point([{"role": "user", "content": "go"}])
    (failed,) = reviews.listing("p", "s")["reviews"]
    assert failed["state"] == "failed"
    assert "IntegrityError" in failed["summary"]
    coverage = reviews.coverage("p", "s")
    assert coverage["unreviewed_reasons"] == {"dispatch_failed": 1}
    assert not reviews.due("p", "s")
