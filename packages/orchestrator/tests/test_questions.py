# SPDX-License-Identifier: MIT
"""Questions to the user (plans-and-delegation spec, PD-23 to PD-26, PD-41): the store
that keeps them, the ``ask_user`` tool that waits for the answer, and a main run that
asks through ``cli.main`` against the scripted gateway."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import question_tool
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.documents import store_path
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.question_tool import CANCELLED, STOPPED, QuestionTool
from sandbox_science.orchestrator.questions import Answer, Question, Questions
from sandbox_science.orchestrator.tools import HARNESS_TOOL_NAMES, Toolbox

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env-1")
CURVES = [
    {"label": "Ed25519", "detail": "Faster, and the common choice"},
    {"label": "Ed448"},
]


@pytest.fixture
def control(tmp_path: Path) -> Iterator[ControlStore]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.open_session("p", "other", dedicated=False)
    yield projects.control
    projects.close()


@pytest.fixture
def questions(control: ControlStore) -> Questions:
    return Questions(control)


def ask(
    questions: Questions,
    operation: str = "op-1",
    text: str = "Which curve should the signatures use?",
    options: list[dict[str, str]] | None = None,
    *,
    multiple: bool = False,
    run: str = "run-1",
) -> Question:
    chosen = CURVES if options is None else options
    return questions.ask(CONTEXT, run, "call-1", operation, text, chosen, multiple=multiple)


def refused(error: pytest.ExceptionInfo[ControlError], kind: str = "invalid_request") -> str:
    assert error.value.kind == kind
    return error.value.message


# The store.


def test_a_question_is_kept_open_until_answered(questions: Questions) -> None:
    asked = ask(questions, text="Which curve?\r\nThe review needs one.")
    assert (asked.state, asked.answer, asked.closed_at) == ("open", None, None)
    assert asked.text == "Which curve?\nThe review needs one."  # line feeds for line ends
    assert [option.label for option in asked.options] == ["Ed25519", "Ed448"]
    assert asked.options[0].detail == "Faster, and the common choice"
    assert questions.open("p", "s") == [asked]
    assert questions.open("p", "other") == []
    assert questions.get("p", "s", asked.question_id) == asked


def test_the_same_operation_asks_once(questions: Questions) -> None:
    first = ask(questions)
    assert ask(questions) == first
    with pytest.raises(ControlError) as error:
        ask(questions, text="Another question?")
    assert "asked another question" in refused(error, "conflict")
    assert len(questions.open("p", "s")) == 1


def test_questions_are_listed_oldest_first(questions: Questions) -> None:
    ids = [ask(questions, f"op-{n}").question_id for n in range(3)]
    assert [q.question_id for q in questions.open("p", "s")] == ids


@pytest.mark.parametrize(
    ("text", "options", "multiple", "message"),
    [
        ("  ", None, False, "1 to 4000 characters"),
        ("x" * 4001, None, False, "1 to 4000 characters"),
        ("Which?", [{"label": "Only"}], False, "two to four options"),
        ("Which?", [{"label": str(n)} for n in range(5)], False, "two to four options"),
        ("Which?", [{"label": "Same"}, {"label": "Same "}], False, "a label of its own"),
        ("Which?", [], True, "only a question with options"),
        ("Which?", [{"label": "a"}, {"label": ""}], False, "an option's label"),
        ("Which?", [{"label": "a"}, {"label": "b", "colour": "red"}], False, "an option's"),
    ],
)
def test_a_question_has_text_and_two_to_four_distinct_options(
    questions: Questions,
    text: str,
    options: list[dict[str, str]],
    multiple: bool,
    message: str,
) -> None:
    with pytest.raises(ControlError) as error:
        ask(questions, text=text, options=options, multiple=multiple)
    assert message in refused(error)
    assert questions.open("p", "s") == []


@pytest.mark.parametrize(
    ("text", "label", "detail", "fits"),
    [
        ("x" * 4000, "l" * 120, "d" * 400, True),
        ("x" * 4001, "l", None, False),
        ("Which?", "l" * 121, None, False),
        ("Which?", "l", "d" * 401, False),
    ],
)
def test_the_size_boundaries_of_a_question(
    questions: Questions, text: str, label: str, detail: str | None, fits: bool
) -> None:
    first: dict[str, str] = {"label": label} | ({"detail": detail} if detail else {})
    options = [first, {"label": "other"}]
    if fits:
        asked = ask(questions, text=text, options=options)
        assert (len(asked.text), len(asked.options[0].label)) == (len(text), len(label))
    else:
        with pytest.raises(ControlError) as error:
            ask(questions, text=text, options=options)
        refused(error)


@pytest.mark.parametrize(("size", "fits"), [(16_000, True), (16_001, False)])
def test_an_answer_in_words_is_at_most_16000_characters(
    questions: Questions, size: int, fits: bool
) -> None:
    asked = ask(questions, options=[])
    words = Answer(kind="free", text="é" * size)  # characters, not bytes
    if fits:
        assert questions.answer("p", "s", asked.question_id, words).state == "answered"
    else:
        with pytest.raises(ControlError) as error:
            questions.answer("p", "s", asked.question_id, words)
        assert "1 to 16000 characters" in refused(error)


@pytest.mark.parametrize("hidden", [0x200B, 0x202E, 0xE0041])
@pytest.mark.parametrize("where", ["text", "label", "detail"])
def test_the_agent_asks_nothing_the_user_may_not_see(
    questions: Questions, hidden: int, where: str
) -> None:
    """Threat model A3: the question and its options are the agent's text."""
    sneaky = f"Pick{chr(hidden)} one"
    options = [{"label": "a", "detail": "first"}, {"label": "b"}]
    text = "Which?"
    if where == "text":
        text = sneaky
    else:
        options[0][where] = sneaky
    with pytest.raises(ControlError) as error:
        ask(questions, text=text, options=options)
    assert f"U+{hidden:04X}" in refused(error)


def test_a_question_without_options_takes_an_answer_in_words(questions: Questions) -> None:
    asked = ask(questions, options=[])
    assert (asked.options, asked.multiple) == ((), False)
    with pytest.raises(ControlError) as error:
        questions.answer("p", "s", asked.question_id, Answer(kind="picked", picked=("a",)))
    assert "pick one of the question's options" in refused(error)
    text = "  Ed448, for the margin. " + chr(0x200B)  # the user's, kept as written
    done = questions.answer("p", "s", asked.question_id, Answer(kind="free", text=text))
    assert (done.state, done.reason) == ("answered", None)
    assert done.closed_at is not None
    assert done.answer == Answer(kind="free", text=text.strip())
    assert questions.open("p", "s") == []


@pytest.mark.parametrize(
    ("multiple", "picked", "kept"),
    [
        (False, ("Ed448",), ("Ed448",)),
        (True, ("Ed448", "Ed25519"), ("Ed25519", "Ed448")),  # in the question's order
    ],
)
def test_the_user_picks_options(
    questions: Questions, multiple: bool, picked: tuple[str, ...], kept: tuple[str, ...]
) -> None:
    asked = ask(questions, multiple=multiple)
    done = questions.answer("p", "s", asked.question_id, Answer(kind="picked", picked=picked))
    assert (done.state, done.answer) == ("answered", Answer(kind="picked", picked=kept))


@pytest.mark.parametrize(
    "answer",
    [
        Answer(kind="picked", picked=("Ed448", "Ed25519")),  # one only
        Answer(kind="picked", picked=("P-256",)),
        Answer(kind="picked", picked=("Ed448", "Ed448")),
        Answer(kind="picked"),
        Answer(kind="picked", picked=("Ed448",), text="and why"),
        Answer(kind="free", text="   "),
        Answer(kind="free", text="x" * 16_001),
        Answer(kind="free", text="Ed448", picked=("Ed448",)),
        Answer(kind="decide", text="you choose"),
        Answer(kind="cancel", text="never mind"),
    ],
)
def test_an_answer_fits_its_question(questions: Questions, answer: Answer) -> None:
    asked = ask(questions)
    with pytest.raises(ControlError) as error:
        questions.answer("p", "s", asked.question_id, answer)
    refused(error)
    assert questions.get("p", "s", asked.question_id).state == "open"


def test_the_user_leaves_it_to_the_agent_or_cancels(questions: Questions) -> None:
    decided = questions.answer("p", "s", ask(questions).question_id, Answer(kind="decide"))
    assert (decided.state, decided.answer) == ("answered", Answer(kind="decide"))
    cancelled = questions.answer(
        "p", "s", ask(questions, "op-2").question_id, Answer(kind="cancel")
    )
    assert (cancelled.state, cancelled.answer) == ("cancelled", Answer(kind="cancel"))
    assert cancelled.reason == "the user cancelled it"


def test_a_question_is_answered_once(questions: Questions) -> None:
    asked = ask(questions)
    first = Answer(kind="picked", picked=("Ed448",))
    done = questions.answer("p", "s", asked.question_id, first)
    assert questions.answer("p", "s", asked.question_id, first) == done  # a retry
    with pytest.raises(ControlError) as error:
        questions.answer("p", "s", asked.question_id, Answer(kind="decide"))
    assert "is answered, not open" in refused(error, "conflict")


def test_a_question_is_answered_only_in_its_session(questions: Questions) -> None:
    asked = ask(questions)
    with pytest.raises(ControlError) as error:
        questions.answer("p", "other", asked.question_id, Answer(kind="decide"))
    refused(error, "not_found")
    with pytest.raises(ControlError) as error:
        questions.get("p", "other", asked.question_id)
    refused(error, "not_found")


def test_questions_close_by_run_question_or_session(questions: Questions) -> None:
    mine = ask(questions, "op-1", run="run-1")
    theirs = ask(questions, "op-2", run="run-2")
    alone = ask(questions, "op-3", run="run-2")
    answered = ask(questions, "op-4", run="run-1")
    questions.answer("p", "s", answered.question_id, Answer(kind="decide"))
    closed = questions.close("p", "s", run="run-1", state="cancelled", reason="the run ended")
    assert closed == [mine.question_id]
    assert questions.close(
        "p", "s", question=alone.question_id, state="cancelled", reason="the run ended"
    ) == [alone.question_id]
    assert questions.close("p", "s", state="interrupted", reason="the service restarted") == [
        theirs.question_id
    ]
    gone = questions.get("p", "s", theirs.question_id)
    assert (gone.state, gone.reason, gone.answer) == (
        "interrupted",
        "the service restarted",
        None,
    )
    assert questions.get("p", "s", answered.question_id).state == "answered"
    with pytest.raises(ControlError) as error:
        questions.answer("p", "s", mine.question_id, Answer(kind="decide"))
    assert "is cancelled, not open" in refused(error, "conflict")


def test_a_closed_question_is_kept_as_it_closed(tmp_path: Path, questions: Questions) -> None:
    asked = ask(questions)
    questions.answer("p", "s", asked.question_id, Answer(kind="decide"))
    db = sqlite3.connect(store_path(tmp_path))
    try:
        for statement in (
            "UPDATE user_questions SET state = 'open', closed_at = NULL, answer = NULL",
            "UPDATE user_questions SET reason = 'changed'",
            "DELETE FROM user_questions",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)
    finally:
        db.close()
    assert questions.get("p", "s", asked.question_id).answer == Answer(kind="decide")


# The tool.


async def until_open(questions: Questions, session: str = "s") -> Question:
    for _ in range(500):
        found = await asyncio.to_thread(questions.open, "p", session)
        if found:
            return found[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no question was asked")


@pytest.fixture
def tool(questions: Questions) -> QuestionTool:
    return QuestionTool(questions, CONTEXT, run="run-1", poll_seconds=0.01)


def answer_of(text: str) -> dict[str, Any]:
    return json.loads(text)


async def test_the_tool_returns_the_answer_to_the_call_that_asked(
    questions: Questions, tool: QuestionTool
) -> None:
    """PD-24: two calls wait at once, as two runs' calls may; each gets its own
    question's answer."""
    first = asyncio.create_task(
        tool.call({"question": "Which curve?", "options": CURVES}, operation="op-1", call_id="c1")
    )
    asked = await until_open(questions)
    another = QuestionTool(questions, CONTEXT, run="run-2", poll_seconds=0.01)
    second = asyncio.create_task(
        another.call({"question": "How many runs?"}, operation="op-2", call_id="c2")
    )
    while len(await asyncio.to_thread(questions.open, "p", "s")) < 2:
        await asyncio.sleep(0.01)
    other = (await asyncio.to_thread(questions.open, "p", "s"))[1]
    assert (asked.run, asked.tool_call) == ("run-1", "c1")
    assert (other.run, other.tool_call) == ("run-2", "c2")
    questions.answer("p", "s", other.question_id, Answer(kind="free", text="Three."))
    text, record = await second
    assert answer_of(text) == {
        "question_id": other.question_id,
        "answer": "own_words",
        "text": "Three.",
    }
    assert not first.done()
    questions.answer("p", "s", asked.question_id, Answer(kind="picked", picked=("Ed448",)))
    text, record = await first
    assert answer_of(text) == {
        "question_id": asked.question_id,
        "answer": "picked",
        "picked": ["Ed448"],
    }
    assert record["question"]["state"] == "answered"
    assert record["question"]["answer"] == {"kind": "picked", "picked": ["Ed448"]}
    # A replay of the call meets its answered question and returns at once.
    again, _ = await tool.call(
        {"question": "Which curve?", "options": CURVES}, operation="op-1", call_id="c1"
    )
    assert again == text


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (Answer(kind="decide"), "decide"),
        (Answer(kind="cancel"), "cancelled"),
    ],
)
async def test_the_tool_says_what_a_decide_or_a_cancel_means(
    questions: Questions, tool: QuestionTool, answer: Answer, expected: str
) -> None:
    waiting = asyncio.create_task(tool.call({"question": "Go on?"}, operation="o", call_id="c"))
    asked = await until_open(questions)
    questions.answer("p", "s", asked.question_id, answer)
    value = answer_of((await waiting)[0])
    assert value["answer"] == expected
    assert value["note"]


@pytest.mark.parametrize(
    ("cancelled", "state", "reason"),
    [(True, "cancelled", CANCELLED), (False, "interrupted", STOPPED)],
)
async def test_a_run_that_ends_while_it_waits_closes_its_question(
    questions: Questions, cancelled: bool, state: str, reason: str
) -> None:
    """The user's cancel (RS-11) closes it ``cancelled``; a run stopped otherwise, as by
    the run service stopping, ``interrupted``."""
    stop = asyncio.Event()
    tool = QuestionTool(questions, CONTEXT, run="run-1", stop=stop, poll_seconds=0.01)
    waiting = asyncio.create_task(tool.call({"question": "Go on?"}, operation="o", call_id="c"))
    asked = await until_open(questions)
    if cancelled:
        stop.set()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    closed = questions.get("p", "s", asked.question_id)
    assert (closed.state, closed.reason, closed.answer) == (state, reason, None)
    # Replayed, it reports the question closed with no answer.
    value = answer_of((await tool.call({"question": "Go on?"}, operation="o", call_id="c"))[0])
    assert value == {
        "question_id": asked.question_id,
        "answer": None,
        "state": state,
        "note": f"The question closed without an answer: {reason}.",
    }


async def decided(questions: Questions, tool: QuestionTool, operation: str) -> dict[str, Any]:
    """Ask under ``operation``, and let the agent decide once it is open."""
    waiting = asyncio.create_task(
        tool.call({"question": f"{operation}?"}, operation=operation, call_id=operation)
    )
    while not waiting.done():
        for found in await asyncio.to_thread(questions.open, "p", "s"):
            questions.answer("p", "s", found.question_id, Answer(kind="decide"))
        await asyncio.sleep(0.01)
    return answer_of((await waiting)[0])


async def test_a_run_asks_one_question_per_reply_and_ten_in_all(
    questions: Questions, tool: QuestionTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    run: Any = None  # the hook reads nothing of the run
    assert (await decided(questions, tool, "op-1"))["answer"] == "decide"
    with pytest.raises(ControlError) as error:
        await tool.call({"question": "And?"}, operation="op-2", call_id="op-2")
    assert refused(error).startswith("ask one question per reply")
    assert (await decided(questions, tool, "op-1"))["answer"] == "decide"  # a replay
    await tool.model_returned(run)
    assert (await decided(questions, tool, "op-2"))["answer"] == "decide"
    monkeypatch.setattr(question_tool, "MAX_ASKED", 2)
    await tool.model_returned(run)
    with pytest.raises(ControlError) as error:
        await tool.call({"question": "More?"}, operation="op-3", call_id="op-3")
    assert refused(error, "policy_denied").startswith("limit_reached")
    assert tool.asked == {"op-1", "op-2"}


class _NoWorker:
    """The toolbox's worker, which the question tool never reaches."""


async def test_only_a_main_runs_toolbox_offers_ask_user(
    questions: Questions, tool: QuestionTool
) -> None:
    """PD-41: a delegated run is not offered the tool, and its call is refused."""
    assert "ask_user" in HARNESS_TOOL_NAMES
    client: Any = _NoWorker()
    main = Toolbox(client, CONTEXT, languages=(), questions=tool)
    assert "ask_user" in [spec.name for spec in main.specs]
    bad = ToolCall("c1", "ask_user", json.dumps({"question": "Which?", "options": CURVES[:1]}))
    failed = json.loads((await main.call(bad, "op-1")).text)["error"]
    assert failed["kind"] == "invalid_request"
    assert "two to four options" in failed["message"]
    with pytest.raises(ValueError, match="cannot ask the user"):
        Toolbox(client, CONTEXT, languages=(), questions=tool, delegated=True)
    child = Toolbox(client, CONTEXT, languages=(), delegated=True)
    assert "ask_user" not in [spec.name for spec in child.specs]
    asking = ToolCall("c2", "ask_user", json.dumps({"question": "Which?"}))
    error = json.loads((await child.call(asking, "op-2")).text)["error"]
    assert (error["kind"], error["message"].split(":")[0]) == (
        "policy_denied",
        "not_permitted_for_role",
    )
    assert questions.open("p", "s") == []


# A main run.


async def test_a_main_run_waits_for_the_users_answer(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PD-23, PD-24: the run asks, waits in the call, and reads the answer as its result;
    the question is kept, naming the run and the call."""
    monkeypatch.setattr(question_tool, "POLL_SECONDS", 0.02)
    documents = harness.documents()
    questions = Questions(documents.control)

    async def answer() -> Question:
        found: list[Question] = []
        while not found:
            await asyncio.sleep(0.02)
            found = await asyncio.to_thread(questions.open, PROJECT, SESSION)
        (asked,) = found
        picked = Answer(kind="picked", picked=("Ed448",))
        return await asyncio.to_thread(
            questions.answer, PROJECT, SESSION, asked.question_id, picked
        )

    try:
        answering = asyncio.create_task(answer())
        record = await harness.run(
            "asker",
            [
                Reply(calls=[call("ask_user", question="Which curve?", options=CURVES)]),
                Reply("Using Ed448, as you chose."),
            ],
        )
        answered = await answering
    finally:
        documents.close()
    assert record.final_text == "Using Ed448, as you chose."
    assert "ask_user" in [tool["function"]["name"] for tool in harness.gateway.tools["main"][0]]
    (result,) = [m for m in harness.gateway.main[1] if m["role"] == "tool"]
    assert json.loads(str(result["content"])) == {
        "question_id": answered.question_id,
        "answer": "picked",
        "picked": ["Ed448"],
    }
    assert answered.run == run_name(record.started_at, record.run_id)
    assert answered.tool_call == result["tool_call_id"]
    (kept,) = [entry for entry in record.tools if entry["tool"] == "ask_user"]
    assert kept["question"] == answered.model_dump(
        mode="json", exclude={"project", "session", "text", "options"}, exclude_none=True
    )
    assert answered.answer == Answer(kind="picked", picked=("Ed448",))
