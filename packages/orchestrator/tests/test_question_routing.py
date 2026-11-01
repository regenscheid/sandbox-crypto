# SPDX-License-Identifier: MIT
"""Questions to the user and the run service (plans-and-delegation spec, PD-23 to PD-26):
a chat message answers the open question of the run that holds the session, and a
question closes when no run can receive its answer: the run cancelled, the service
restarted under it, or the session closed."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import question_tool
from sandbox_science.orchestrator.context.history import body
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.question_tool import CANCELLED, QuestionTool
from sandbox_science.orchestrator.questions import (
    INTERRUPTED,
    ORPHANED,
    RUN_ENDED,
    SESSION_CLOSED,
    Answer,
    Question,
    Questions,
)
from sandbox_science.orchestrator.runstate import ChatMessage, RunState
from sandbox_science.testing import FakeClock, eventually

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import ended, post, runs, serving, states

CURVES = [{"label": "Ed25519"}, {"label": "Ed448"}]
ASK = call("ask_user", question="Which curve should sign the release?", options=CURVES)


def opened(tmp_path: Path) -> tuple[ProjectStore, ExecutionContext]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("p", "s", dedicated=False)
    return projects, ExecutionContext(
        project_id="p", session_id="s", environment_id=session.environment_id
    )


def asked(questions: Questions, context: ExecutionContext, run: str, operation: str) -> Question:
    return questions.ask(context, run, f"call-{operation}", operation, f"{operation}?", CURVES)


def say(state: RunState, message_id: str, text: str, answers: str | None) -> ChatMessage | Question:
    return state.say("p", "s", message_id, text, author="user", answers=answers)


def changed(error: pytest.ExceptionInfo[ControlError], now: str | None) -> None:
    """The message was refused: the question it answers changed, and is now ``now``."""
    assert error.value.kind == "conflict"
    assert error.value.details == {"reason": "question_changed", "question": now}


def test_a_message_answers_the_oldest_question_of_the_run_that_holds_the_session(
    tmp_path: Path,
) -> None:
    projects, context = opened(tmp_path)
    state, questions = RunState(projects.control), Questions(projects.control)
    stale = asked(questions, context, "run-0", "op-0")  # its run holds the session no more
    state.acquire("p", "s", "run-1", "holder")
    first, second = (asked(questions, context, "run-1", op) for op in ("op-1", "op-2"))

    answered = say(state, "m-1", "  Ed448, for the margin.\n", first.question_id)
    assert isinstance(answered, Question)
    assert (answered.question_id, answered.state, answered.message) == (
        first.question_id,
        "answered",
        "m-1",
    )
    assert answered.answer == Answer(kind="free", text="Ed448, for the margin.")
    # A retry is the answer given, whatever the chat shows now.
    assert say(state, "m-1", "  Ed448, for the margin.\n", None) == answered
    with pytest.raises(ControlError) as error:
        say(state, "m-1", "Ed25519", first.question_id)
    assert (error.value.kind, error.value.details) == ("conflict", {})
    with pytest.raises(ControlError) as error:  # its ID is not a queued message's
        state.post("p", "s", "m-1", "  Ed448, for the margin.\n", author="user")
    assert error.value.message == "message 'm-1' answered a question"

    later = say(state, "m-2", "Both, then.", second.question_id)
    assert isinstance(later, Question)
    assert later.question_id == second.question_id
    queued = say(state, "m-3", "And the date?", None)
    assert isinstance(queued, ChatMessage)
    assert queued.state == "queued"
    assert [m.message_id for m in state.messages("p", "s")] == ["m-3"]  # answers are not queued
    assert questions.get("p", "s", stale.question_id).state == "open"
    projects.close()


def test_a_message_names_the_question_it_answers_or_nothing_is_written(
    tmp_path: Path,
) -> None:
    """The message answers the question the user was shown: a button, a cancel or another
    message that closed it first, or a question that opened unseen, refuses it, and it
    stays the user's draft."""
    projects, context = opened(tmp_path)
    state, questions = RunState(projects.control), Questions(projects.control)
    state.acquire("p", "s", "run-1", "holder")
    question = asked(questions, context, "run-1", "op-1")
    with pytest.raises(ControlError) as error:  # sent with no question shown
        say(state, "m-1", "Hello.", None)
    changed(error, question.question_id)

    say(state, "m-2", "Ed448.", question.question_id)  # the first of two messages wins
    with pytest.raises(ControlError) as error:
        say(state, "m-3", "Ed25519.", question.question_id)
    changed(error, None)

    clicked = asked(questions, context, "run-1", "op-2")  # a button answers first
    questions.answer("p", "s", clicked.question_id, Answer(kind="decide"))
    with pytest.raises(ControlError) as error:
        say(state, "m-4", "Ed448.", clicked.question_id)
    changed(error, None)

    cancelled = asked(questions, context, "run-1", "op-3")  # the run is cancelled first
    questions.close("p", "s", run="run-1", state="cancelled", reason="the run was cancelled")
    with pytest.raises(ControlError) as error:
        say(state, "m-5", "Ed448.", cancelled.question_id)
    changed(error, None)

    assert state.messages("p", "s") == []
    assert questions.get("p", "s", question.question_id).answer == Answer(
        kind="free", text="Ed448."
    )
    assert questions.get("p", "s", clicked.question_id).answer == Answer(kind="decide")
    projects.close()


def test_a_message_from_anything_but_the_users_chat_answers_no_question(
    tmp_path: Path,
) -> None:
    projects, context = opened(tmp_path)
    state, questions = RunState(projects.control), Questions(projects.control)
    state.acquire("p", "s", "run-1", "holder")
    question = asked(questions, context, "run-1", "op-1")
    queued = state.post("p", "s", "m-1", "A note from a watcher.", author="watcher")
    assert queued.state == "queued"
    assert questions.get("p", "s", question.question_id).state == "open"
    projects.close()


def test_a_message_waits_in_the_queue_once_the_asking_runs_lease_expired(
    tmp_path: Path,
) -> None:
    projects, context = opened(tmp_path)
    past = FakeClock(datetime.now(UTC) - timedelta(minutes=5))
    RunState(projects.control, clock=past).acquire("p", "s", "run-1", "a-dead-process")
    questions = Questions(projects.control)
    question = asked(questions, context, "run-1", "op-1")
    found = say(RunState(projects.control), "m-1", "Ed448", None)
    assert isinstance(found, ChatMessage)
    assert found.state == "queued"
    assert questions.get("p", "s", question.question_id).state == "open"
    projects.close()


@pytest.mark.parametrize(("size", "fits"), [(16_000, True), (16_001, False)])
def test_a_message_answers_with_at_most_16000_characters(
    tmp_path: Path, size: int, fits: bool
) -> None:
    projects, context = opened(tmp_path)
    state, questions = RunState(projects.control), Questions(projects.control)
    state.acquire("p", "s", "run-1", "holder")
    question = asked(questions, context, "run-1", "op-1")
    text = "é" * size  # characters, not bytes
    if fits:
        answered = say(state, "m-1", text, question.question_id)
        assert isinstance(answered, Question)
        assert answered.state == "answered"
    else:
        with pytest.raises(ControlError) as error:
            say(state, "m-1", text, question.question_id)
        assert error.value.kind == "invalid_request"
        assert error.value.message.startswith(
            f"the message answers question {question.question_id}"
        )
        assert state.messages("p", "s") == []
        assert questions.get("p", "s", question.question_id).state == "open"
    projects.close()


async def test_a_question_left_open_by_a_failed_close_is_closed_by_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run cancelled while it waits closes its question; when that close fails, the
    question has no run, and the run service's sweep closes it."""
    projects, context = opened(tmp_path)
    state, questions = RunState(projects.control), Questions(projects.control)
    state.acquire("p", "s", "run-1", "holder")
    tool = QuestionTool(questions, context, run="run-1", poll_seconds=0.01)
    waiting = asyncio.create_task(tool.call({"question": "Go on?"}, operation="o", call_id="c"))
    for _ in range(500):
        if questions.open("p", "s"):
            break
        await asyncio.sleep(0.01)
    (question,) = questions.open("p", "s")

    def failing(*args: object, **kwargs: object) -> list[str]:
        raise ControlError("busy", "the store was busy")

    monkeypatch.setattr(questions, "close", failing)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert questions.get("p", "s", question.question_id).state == "open"
    assert questions.close_orphans() == []  # its run still holds the session
    state.release("p", "s", "holder")
    assert questions.close_orphans() == [("p", "s", question.question_id)]
    closed = questions.get("p", "s", question.question_id)
    assert (closed.state, closed.reason, closed.answer) == ("interrupted", ORPHANED, None)
    assert questions.close_orphans() == []
    projects.close()


def test_the_sweep_leaves_a_run_whose_lease_expired_to_the_service(tmp_path: Path) -> None:
    projects, context = opened(tmp_path)
    past = FakeClock(datetime.now(UTC) - timedelta(minutes=5))
    RunState(projects.control, clock=past).acquire("p", "s", "run-1", "a-dead-process")
    questions = Questions(projects.control)
    question = asked(questions, context, "run-1", "op-1")
    assert questions.close_orphans() == []  # the service ends the run, then its questions
    assert questions.get("p", "s", question.question_id).state == "open"
    projects.close()


def test_an_answer_is_taken_only_while_the_run_that_asked_holds_the_session(
    tmp_path: Path,
) -> None:
    """An answer no run would read is refused, so it can be sent as a message instead."""
    projects, context = opened(tmp_path)
    state, questions = RunState(projects.control), Questions(projects.control)
    question = asked(questions, context, "run-1", "op-1")
    decide = Answer(kind="decide")

    def refused() -> None:
        with pytest.raises(ControlError) as error:
            questions.answer("p", "s", question.question_id, decide, live_after=state.live_after())
        assert (error.value.kind, error.value.message) == ("conflict", RUN_ENDED)
        assert error.value.details == {"reason": "run_ended", "run": "run-1"}
        assert questions.get("p", "s", question.question_id).state == "open"

    refused()  # no run holds the session
    past = FakeClock(datetime.now(UTC) - timedelta(minutes=5))
    RunState(projects.control, clock=past).acquire("p", "s", "run-1", "a-dead-process")
    refused()  # its lease expired
    state.acquire("p", "s", "run-1", "holder")  # taken over, live
    done = questions.answer("p", "s", question.question_id, decide, live_after=state.live_after())
    assert done.state == "answered"
    state.release("p", "s", "holder")
    # The same answer again is the one given, whether the run still holds the session or not.
    again = questions.answer("p", "s", question.question_id, decide, live_after=state.live_after())
    assert again == done
    projects.close()


def test_closing_the_session_cancels_its_open_questions(tmp_path: Path) -> None:
    projects, context = opened(tmp_path)
    questions = Questions(projects.control)
    question = asked(questions, context, "run-1", "op-1")
    answered = asked(questions, context, "run-1", "op-2")
    questions.answer("p", "s", answered.question_id, Answer(kind="decide"))
    projects.close_session("p", "s")
    closed = questions.get("p", "s", question.question_id)
    assert (closed.state, closed.reason) == ("cancelled", SESSION_CLOSED)
    assert questions.get("p", "s", answered.question_id).state == "answered"
    projects.close()


# Through the run service.


def questions_of(harness: Harness) -> Questions:
    return Questions(ProjectStore(harness.state).control)


async def asking(harness: Harness) -> Question:
    """The question the session's run asked, once it is open."""
    found: list[Question] = []

    def asked_one() -> bool:
        found[:] = questions_of(harness).open(PROJECT, SESSION)
        return bool(found)

    await eventually(asked_one, timeout=20)
    return found[0]


async def test_a_message_answers_the_question_a_service_run_waits_on(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PD-24: the answer, written as a chat message, reaches the call that asked; the
    message is not queued for another turn."""
    monkeypatch.setattr(question_tool, "POLL_SECONDS", 0.02)
    post(harness, "m-1", "Sign the release.")
    async with serving(harness, Reply(calls=[ASK]), Reply("Signed with Ed448.")):
        question = await asking(harness)
        answered = runs(harness).say(
            PROJECT, SESSION, "m-2", "Ed448, please.", author="user", answers=question.question_id
        )
        assert isinstance(answered, Question)
        assert answered.question_id == question.question_id
        (record,) = await ended(harness)
    assert record.final_text == "Signed with Ed448."
    assert question.run == run_name(record.started_at, record.run_id)
    (result,) = [m for m in harness.gateway.main[1] if m["role"] == "tool"]
    assert json.loads(body(str(result["content"]))) == {
        "question_id": question.question_id,
        "answer": "own_words",
        "text": "Ed448, please.",
    }
    assert states(harness) == {"m-1": "delivered"}
    (kept,) = [entry for entry in record.tools if entry["tool"] == "ask_user"]
    assert cast(dict[str, Any], kept["question"])["message"] == "m-2"


async def test_cancelling_a_run_that_waits_for_an_answer_cancels_its_question(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RS-11: the run ends ``cancelled``, and its question with it."""
    monkeypatch.setattr(question_tool, "POLL_SECONDS", 0.02)
    post(harness, "m-1", "Sign the release.")
    async with serving(harness, Reply(calls=[ASK]), Reply("Never.")):
        question = await asking(harness)
        runs(harness).cancel(PROJECT, SESSION, question.run)
        (record,) = await ended(harness)
    assert record.stopped == "cancelled"
    closed = questions_of(harness).get(PROJECT, SESSION, question.question_id)
    assert (closed.state, closed.reason, closed.answer) == ("cancelled", CANCELLED, None)


async def test_the_questions_of_a_run_whose_service_died_close_interrupted(
    harness: Harness,
) -> None:
    """RS-12: the next service ends the dead run, and its questions with it."""
    past = FakeClock(datetime.now(UTC) - timedelta(minutes=5))
    run = "20261014T000000000000Z-crashed"
    RunState(ProjectStore(harness.state).control, clock=past).acquire(
        PROJECT, SESSION, run, "a-dead-process"
    )
    question = questions_of(harness).ask(
        harness.context(), run, "call-1", "op-1", "Which curve?", CURVES
    )
    async with serving(harness):
        await eventually(lambda: runs(harness).ended(PROJECT, SESSION, run), timeout=10)
    closed = questions_of(harness).get(PROJECT, SESSION, question.question_id)
    assert (closed.state, closed.reason, closed.answer) == ("interrupted", INTERRUPTED, None)


async def test_the_service_closes_a_question_whose_run_holds_no_lease(harness: Harness) -> None:
    run = "20261014T000000000000Z-gone"
    question = questions_of(harness).ask(
        harness.context(), run, "call-1", "op-1", "Which curve?", CURVES
    )
    async with serving(harness, serve=False) as service:
        await service.tick()
    closed = questions_of(harness).get(PROJECT, SESSION, question.question_id)
    assert (closed.state, closed.reason) == ("interrupted", ORPHANED)
