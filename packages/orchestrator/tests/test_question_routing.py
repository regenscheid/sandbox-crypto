# SPDX-License-Identifier: MIT
"""Questions to the user and the run service (plans-and-delegation spec, PD-23 to PD-26):
a chat message answers the open question of the run that holds the session, and a
question closes when no run can receive its answer: the run cancelled, the service
restarted under it, or the session closed."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import question_tool
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.question_tool import CANCELLED
from sandbox_science.orchestrator.questions import (
    INTERRUPTED,
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


def test_a_message_answers_the_oldest_question_of_the_run_that_holds_the_session(
    tmp_path: Path,
) -> None:
    projects, context = opened(tmp_path)
    state, questions = RunState(projects.control), Questions(projects.control)
    stale = asked(questions, context, "run-0", "op-0")  # its run holds the session no more
    state.acquire("p", "s", "run-1", "holder")
    first, second = (asked(questions, context, "run-1", op) for op in ("op-1", "op-2"))

    answered = state.post("p", "s", "m-1", "  Ed448, for the margin.\n", author="user")
    assert isinstance(answered, Question)
    assert (answered.question_id, answered.state, answered.message) == (
        first.question_id,
        "answered",
        "m-1",
    )
    assert answered.answer == Answer(kind="free", text="Ed448, for the margin.")
    assert state.post("p", "s", "m-1", "  Ed448, for the margin.\n", author="user") == answered
    with pytest.raises(ControlError) as error:
        state.post("p", "s", "m-1", "Ed25519", author="user")
    assert error.value.kind == "conflict"

    later = state.post("p", "s", "m-2", "Both, then.", author="user")
    assert isinstance(later, Question)
    assert later.question_id == second.question_id
    queued = state.post("p", "s", "m-3", "And the date?", author="user")
    assert isinstance(queued, ChatMessage)
    assert queued.state == "queued"
    assert [m.message_id for m in state.messages("p", "s")] == ["m-3"]  # answers are not queued
    assert questions.get("p", "s", stale.question_id).state == "open"
    projects.close()


def test_a_message_waits_in_the_queue_once_the_asking_runs_lease_expired(
    tmp_path: Path,
) -> None:
    projects, context = opened(tmp_path)
    past = FakeClock(datetime.now(UTC) - timedelta(minutes=5))
    RunState(projects.control, clock=past).acquire("p", "s", "run-1", "a-dead-process")
    questions = Questions(projects.control)
    question = asked(questions, context, "run-1", "op-1")
    found = RunState(projects.control).post("p", "s", "m-1", "Ed448", author="user")
    assert isinstance(found, ChatMessage)
    assert found.state == "queued"
    assert questions.get("p", "s", question.question_id).state == "open"
    projects.close()


def test_a_message_too_long_for_an_answer_is_refused_naming_its_question(
    tmp_path: Path,
) -> None:
    projects, context = opened(tmp_path)
    state, questions = RunState(projects.control), Questions(projects.control)
    state.acquire("p", "s", "run-1", "holder")
    question = asked(questions, context, "run-1", "op-1")
    with pytest.raises(ControlError) as error:
        state.post("p", "s", "m-1", "x" * 16_001, author="user")
    assert error.value.kind == "invalid_request"
    assert error.value.message.startswith(f"the message answers question {question.question_id}")
    assert state.messages("p", "s") == []
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
        answered = runs(harness).post(PROJECT, SESSION, "m-2", "Ed448, please.", author="user")
        assert isinstance(answered, Question)
        assert answered.question_id == question.question_id
        (record,) = await ended(harness)
    assert record.final_text == "Signed with Ed448."
    assert question.run == run_name(record.started_at, record.run_id)
    (result,) = [m for m in harness.gateway.main[1] if m["role"] == "tool"]
    assert json.loads(str(result["content"])) == {
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
