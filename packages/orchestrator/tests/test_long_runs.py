# SPDX-License-Identifier: MIT
"""Long autonomous runs (run-service spec, RS-6f; the owner, 2026-10-16: a run may go on
for hundreds or thousands of rounds without the user). A run's limits are safety nets: at
one, its last model call is a report round that answers with what it found; and a cheap
runaway guard stops a run that repeats itself without progress, and none that works."""

from __future__ import annotations

import json
from typing import cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.cli import main
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.harness import Settings
from sandbox_science.orchestrator.hooks import Closing, RunView, TurnHook
from sandbox_science.orchestrator.loop import (
    MAX_TURNS,
    RUNAWAY,
    TURNS_CEILING,
    Transcript,
    run_task,
)
from sandbox_science.orchestrator.model import ModelTurn, ToolSpec
from sandbox_science.orchestrator.notices import REPORT
from sandbox_science.orchestrator.tools import Request, Toolbox, ToolEntry, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient

from .scripted import ScriptedModel, ToolCall

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")


def toolbox() -> Toolbox:
    return Toolbox(cast(WorkerClient, object()), CONTEXT)


def failing(number: int = 1) -> ModelTurn:
    """A call of a tool that does not exist: its result is the same error each time."""
    return ModelTurn(None, (ToolCall(f"c{number}", "no_such_tool", "{}"),))


def calling(name: str, arguments: str, number: int) -> ModelTurn:
    return ModelTurn(None, (ToolCall(f"c{number}", name, arguments),))


def last_user(messages: list[dict[str, object]]) -> str:
    return next(str(m["content"]) for m in reversed(messages) if m["role"] == "user")


def tool_messages(transcript: Transcript) -> list[str]:
    return [str(m["content"]) for m in transcript.context.messages if m["role"] == "tool"]


async def test_the_defaults_let_a_run_work_for_thousands_of_turns() -> None:
    assert (
        MAX_TURNS,
        Settings().max_turns,
        Transcript(context=AgentContext(messages=[])).max_turns,
    ) == (2000,) * 3
    with pytest.raises(ValueError, match="max_turns"):
        Settings(max_turns=TURNS_CEILING + 1)
    with pytest.raises(ValueError, match="max_turns"):
        Settings(max_turns=0)


async def test_a_run_at_its_turn_limit_reports_what_it_found() -> None:
    """The run's last working turn called a tool; one more call follows, told it is the
    last, whose text stands as the run's final text. A tool it asks for does not run, and
    is answered as not run, so the conversation stays whole for the next run."""
    report = ModelTurn("So far: x is 3; y is still open.", (ToolCall("c9", "no_such_tool", "{}"),))
    model = ScriptedModel([failing(1), calling("other_tool", "{}", 2), report])
    transcript = await run_task(model, toolbox(), "go", run_id="r", max_turns=2)
    assert (transcript.stopped, transcript.final_text, transcript.failure) == (
        "turn_limit",
        "So far: x is 3; y is still open.",
        None,
    )
    assert (transcript.turns, len(model.requests), len(transcript.context.usage)) == (3, 3, 3)
    asked = last_user(model.requests[-1])
    assert asked.startswith(REPORT.text)
    assert json.loads(asked[len(REPORT.text) :]) == {
        "limit": "turn_limit",
        "detail": "its turn limit, 2 model calls",
    }
    # The report's tool call did not run: answered, and recorded beside the others.
    assert transcript.context.messages[-1]["role"] == "tool"
    assert "not_run" in tool_messages(transcript)[-1]
    assert len(transcript.tool_records) == len(tool_messages(transcript)) == 3
    assert transcript.tool_records[-1]["not_run"] == "turn_limit"


async def test_a_report_round_without_text_leaves_no_final_text() -> None:
    model = ScriptedModel([failing(1), ModelTurn(None, (ToolCall("c2", "x", "{}"),))])
    transcript = await run_task(model, toolbox(), "go", run_id="r", max_turns=1)
    assert (transcript.stopped, transcript.final_text, transcript.turns) == ("turn_limit", None, 2)


async def test_a_run_that_answers_within_its_turns_takes_no_report_round() -> None:
    model = ScriptedModel([failing(1), ModelTurn("Done.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.stopped, transcript.final_text, len(model.requests)) == (
        "answered",
        "Done.",
        2,
    )


class Spent(TurnHook):
    """Halts the run before its second call, as a spent cost limit does."""

    name = "budget"

    async def halt(self, run: RunView) -> str | None:
        return "budget" if run.turns else None


async def test_a_spent_cost_limit_stops_the_run_without_a_report_round() -> None:
    model = ScriptedModel([failing(1)])
    transcript = await run_task(model, toolbox(), "go", run_id="r", max_turns=1, hooks=[Spent()])
    assert (transcript.stopped, transcript.final_text, len(model.requests)) == ("budget", None, 1)


class LastCall(TurnHook):
    """Leaves room for one more call from the run's third call on, as a delegated run's
    tokens do (``ModelBudget.last_call``)."""

    name = "budget"

    async def last_call(self, run: RunView) -> str | None:
        return "run_budget" if run.turns >= 2 else None


async def test_a_run_whose_tokens_leave_one_call_makes_it_its_report_round() -> None:
    """The owner's lab (2026-10-16): a delegated run "ended at its run budget without a
    report". Now the call its tokens leave room for is its report round."""
    model = ScriptedModel([failing(1), failing(2), ModelTurn("Partial: two leads.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r", hooks=[LastCall()])
    assert (transcript.stopped, transcript.final_text, transcript.turns) == (
        "run_budget",
        "Partial: two leads.",
        3,
    )
    assert json.loads(last_user(model.requests[-1])[len(REPORT.text) :])["limit"] == "run_budget"


async def test_a_run_that_does_not_fold_reports_before_its_conversation_is_too_long() -> None:
    """A run without folding takes its report round while the call still fits.

    Keep occupancy independent of the default system prompt and tool inventory.
    """

    async def read(request: Request) -> ToolOutcome:
        return ToolOutcome("x" * 36_000, {"path": "big"})

    box = toolbox()
    box.entries = {"read": ToolEntry(ToolSpec("read", "Fixture", {"type": "object"}), "read", read)}
    model = ScriptedModel([calling("read", "{}", 1), ModelTurn("What I read so far.")])
    transcript = await run_task(
        model, box, "go", run_id="r", system="Synthetic reporting test.", max_input_chars=40_000
    )
    assert (transcript.stopped, transcript.final_text) == ("context_limit", "What I read so far.")
    assert json.loads(last_user(model.requests[-1])[len(REPORT.text) :])["limit"] == "context_limit"


async def test_the_runaway_guard_stops_the_same_error_repeated() -> None:
    """RUNAWAY calls in a row that failed with the same error stop the run, ``runaway``,
    after a report round; its record says what tripped the guard."""
    model = ScriptedModel(
        [failing(n) for n in range(RUNAWAY)] + [ModelTurn("I am stuck on a missing tool.")]
    )
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.stopped, transcript.final_text, transcript.turns) == (
        "runaway",
        "I am stuck on a missing tool.",
        RUNAWAY + 1,
    )
    tripped = transcript.summaries["runaway"]
    assert isinstance(tripped, dict)
    assert tripped["rule"] == "same_error"
    assert str(tripped["detail"]).startswith(f"the last {RUNAWAY} tool calls failed")
    assert json.loads(last_user(model.requests[-1])[len(REPORT.text) :])["limit"] == "runaway"


async def test_the_runaway_guard_stops_the_same_call_with_the_same_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def same(*args: object, **kwargs: object) -> ToolOutcome:
        return ToolOutcome('{"lines": ["a"]}', {"path": "a.txt"})

    monkeypatch.setattr(Toolbox, "call", same)
    model = ScriptedModel([calling("read_file", '{"path": "a.txt"}', n) for n in range(RUNAWAY)])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert transcript.stopped == "runaway"
    assert cast(dict[str, str], transcript.summaries["runaway"])["rule"] == "same_call"


async def test_the_runaway_guard_lets_work_and_polling_go_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calls that differ, a wait whose answer is the same while a job runs, and an error
    with a changing message never trip the guard, however many there are."""
    answers = iter(range(10_000))

    async def varied(toolbox: Toolbox, call: ToolCall, *args: object, **kwargs: object) -> object:
        if call.name == "wait_jobs":
            return ToolOutcome('{"jobs": [{"state": "running"}]}', {"wait": "timeout"})
        if call.name == "flaky":
            return ToolOutcome('{"error": "x"}', {"error": f"unavailable {next(answers)}"})
        return ToolOutcome(f'{{"result": {next(answers)}}}', {})

    monkeypatch.setattr(Toolbox, "call", varied)
    turns = (
        [calling("python", '{"code": "1"}', n) for n in range(3 * RUNAWAY)]
        + [calling("wait_jobs", '{"job_ids": ["j"]}', n) for n in range(3 * RUNAWAY)]
        + [calling("flaky", "{}", n) for n in range(3 * RUNAWAY)]
        + [ModelTurn("Done.")]
    )
    transcript = await run_task(ScriptedModel(turns), toolbox(), "go", run_id="r")
    assert (transcript.stopped, transcript.final_text) == ("answered", "Done.")
    assert transcript.turns == 9 * RUNAWAY + 1


class Holds(TurnHook):
    """Holds back every answer for another turn, as a parent's answer is while its
    children run (PD-36)."""

    name = "holds"
    veto = True

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        return Closing("retry", (), stopped="held")


async def test_the_runaway_guard_stops_answers_held_back_again_and_again() -> None:
    model = ScriptedModel([ModelTurn(f"Answer {n}.") for n in range(RUNAWAY)])
    transcript = await run_task(model, toolbox(), "go", run_id="r", hooks=[Holds()])
    # The last held answer is closed again with no turn left: the hook's stop stands.
    assert transcript.stopped == "held"
    assert cast(dict[str, str], transcript.summaries["runaway"])["rule"] == "held"
    assert len(model.requests) == RUNAWAY


async def test_a_long_run_goes_on_past_the_former_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Well past 30 calls, a run that keeps working keeps going."""

    async def ran(toolbox: Toolbox, call: ToolCall, *args: object, **kwargs: object) -> object:
        return ToolOutcome(json.dumps({"ran": call.arguments}), {})

    monkeypatch.setattr(Toolbox, "call", ran)
    turns = [calling("python", json.dumps({"code": str(n)}), n) for n in range(200)]
    transcript = await run_task(
        ScriptedModel([*turns, ModelTurn("Done after 200 calls.")]), toolbox(), "go", run_id="r"
    )
    assert (transcript.stopped, transcript.final_text, transcript.turns) == (
        "answered",
        "Done after 200 calls.",
        201,
    )


@pytest.mark.parametrize("value", ["-1", str(TURNS_CEILING + 1)])
def test_the_cli_refuses_a_turn_limit_out_of_range(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "--worker", "http://127.0.0.1:1", "--token-file", "t", "--script", "s"]
    with pytest.raises(SystemExit):
        main([*argv, "--project", "p", "--session", "s", "--environment", "e",
              "--max-turns", value, "Task."])  # fmt: skip
    assert "--max-turns is 0" in capsys.readouterr().err


def test_the_stopped_event_says_what_tripped_the_guard() -> None:
    from types import SimpleNamespace

    from sandbox_science.orchestrator.chat import _context_stop
    from sandbox_science.orchestrator.projects import RunRecord

    tripped = {"rule": "same_error", "detail": "the last 25 tool calls failed", "count": 25}
    record = SimpleNamespace(stopped="runaway", summaries={"runaway": tripped})
    assert _context_stop(cast(RunRecord, record)) == {
        "reason": "same_error",
        "detail": "the last 25 tool calls failed",
    }
    answered = SimpleNamespace(stopped="answered", summaries={"runaway": tripped})
    assert _context_stop(cast(RunRecord, answered)) == {}
