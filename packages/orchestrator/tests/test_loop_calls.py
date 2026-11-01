# SPDX-License-Identifier: MIT
"""Characterization of what the tool loop does around each model call and each tool call.

These pin the behavior the loop's wrap points carry: a context error folds and tries
again while the folds say so, in a working turn and in the report round; a gateway
budget refusal halts the run with its refusal on record; an error no wrap converts fails
the run in the model phase; the runaway guard sees a tool's outcome before notices ride
on it; and the report round's tool calls never reach the toolbox.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.context.folding import FoldPoint
from sandbox_science.orchestrator.hooks import Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.loop import RUNAWAY, run_task
from sandbox_science.orchestrator.model import (
    GATEWAY_BUDGET,
    ContextLimitError,
    GatewayBudgetError,
    Message,
    ModelTurn,
    ToolCall,
    ToolSpec,
)
from sandbox_science.orchestrator.notices import REPORT
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")


def toolbox() -> Toolbox:
    return Toolbox(cast(WorkerClient, object()), CONTEXT)


def failing(number: int = 1) -> ModelTurn:
    """A call of a tool that does not exist: its result is the same error each time."""
    return ModelTurn(None, (ToolCall(f"c{number}", "no_such_tool", "{}"),))


class Script:
    """Replays turns, raising the exceptions among them in their place."""

    def __init__(self, steps: Sequence[ModelTurn | Exception]) -> None:
        self.steps = list(steps)
        self.requests: list[list[Message]] = []

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        self.requests.append(list(messages))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Folds:
    """Folds whose ``overflow`` answers as scripted; ``grow`` makes a fold that leaves the
    conversation past the run's budget, as one that cannot shrink it would."""

    def __init__(self, answers: Sequence[bool], *, grow: bool = False) -> None:
        self.answers, self.grow = list(answers), grow
        self.overflows: list[int] = []

    async def before_call(self, point: FoldPoint) -> bool:
        return True

    async def overflow(self, point: FoldPoint) -> bool:
        self.overflows.append(point.call_index)
        if self.grow:
            view = list(point.context.sent)
            point.context.rebuild([*view, {"role": "user", "content": "x" * 200_000}])
        return self.answers.pop(0)


def too_long() -> ContextLimitError:
    return ContextLimitError("the conversation is too long", status=400)


async def test_a_context_error_folds_and_tries_again_while_the_folds_say_so() -> None:
    model = Script([too_long(), too_long(), ModelTurn("Done.")])
    folds = Folds([True, True])
    transcript = await run_task(model, toolbox(), "go", run_id="r", folds=folds)
    assert (transcript.stopped, transcript.final_text, transcript.failure) == (
        "answered",
        "Done.",
        None,
    )
    assert (folds.overflows, len(model.requests), len(transcript.context.usage)) == ([1, 1], 3, 1)


async def test_a_context_error_the_folds_cannot_recover_stops_the_run_for_its_length() -> None:
    model = Script([failing(1), too_long(), too_long()])
    folds = Folds([True, False])
    transcript = await run_task(model, toolbox(), "go", run_id="r", folds=folds)
    assert (transcript.stopped, transcript.final_text, transcript.failure) == (
        "context_limit",
        None,
        None,
    )
    assert (folds.overflows, len(model.requests), transcript.halted) == ([2, 2], 3, None)


async def test_a_fold_that_leaves_the_conversation_too_long_is_asked_again_without_a_call() -> None:
    """The retry finds the conversation past the run's budget and makes no call; the
    context error still stands, so the folds are asked again."""
    model = Script([too_long()])
    folds = Folds([True, False], grow=True)
    transcript = await run_task(
        model, toolbox(), "go", run_id="r", folds=folds, max_input_chars=100_000
    )
    assert (transcript.stopped, folds.overflows, len(model.requests)) == (
        "context_limit",
        [1, 1],
        1,
    )


async def test_a_context_error_in_the_report_round_folds_and_tries_again() -> None:
    model = Script([failing(1), too_long(), ModelTurn("So far: one lead.")])
    folds = Folds([True])
    transcript = await run_task(model, toolbox(), "go", run_id="r", max_turns=1, folds=folds)
    assert (transcript.stopped, transcript.final_text, transcript.turns) == (
        "turn_limit",
        "So far: one lead.",
        2,
    )
    assert (folds.overflows, len(model.requests)) == ([2], 3)
    asked = next(str(m["content"]) for m in reversed(model.requests[-1]) if m["role"] == "user")
    assert asked.startswith(REPORT.text)


async def test_a_gateway_budget_refusal_halts_the_run_with_its_refusal_on_record() -> None:
    model = Script([failing(1), GatewayBudgetError("budget spent " + "x" * 600, status=400)])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.stopped, transcript.final_text, transcript.failure) == (
        GATEWAY_BUDGET,
        None,
        None,
    )
    assert transcript.halted == GATEWAY_BUDGET
    refusal = cast(dict[str, str], transcript.summaries["budget"])["refusal"]
    assert refusal.startswith("budget spent ")
    assert len(refusal) == 500
    assert (len(model.requests), len(transcript.context.usage), transcript.phase) == (
        2,
        1,
        "harness",
    )


async def test_a_gateway_budget_refusal_in_the_report_round_keeps_the_limit() -> None:
    model = Script([failing(1), GatewayBudgetError("spent", status=400)])
    transcript = await run_task(model, toolbox(), "go", run_id="r", max_turns=1)
    assert (transcript.stopped, transcript.halted, transcript.final_text) == (
        "turn_limit",
        "turn_limit",
        None,
    )
    assert transcript.summaries["budget"] == {"refusal": "spent"}


async def test_a_model_error_no_wrap_converts_fails_the_run_in_the_model_phase() -> None:
    model = Script([failing(1), RuntimeError("the gateway broke")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.stopped, transcript.failure, transcript.final_text) == (
        "error",
        "model",
        None,
    )
    assert cast(dict[str, object], transcript.summaries["failure"])["phase"] == "model"


class Fresh(TurnHook):
    """Offers a new notice on every tool result, as job notices may."""

    name = "fresh"

    def __init__(self) -> None:
        self.count = 0

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point != "tool":
            return ()
        self.count += 1
        return (Notice("fresh", (f"n{self.count}",), "fresh_notice", {"n": self.count}),)


async def test_the_runaway_guard_counts_a_result_before_notices_ride_on_it() -> None:
    hook = Fresh()
    model = Script([failing(n) for n in range(RUNAWAY)] + [ModelTurn("Stuck.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r", hooks=[hook])
    assert (transcript.stopped, transcript.final_text) == ("runaway", "Stuck.")
    assert cast(dict[str, str], transcript.summaries["runaway"])["rule"] == "same_error"
    # Each result the model read carried its own notice: the texts differ.
    assert hook.count == RUNAWAY
    assert all("fresh_notice" in str(record) for record in transcript.tool_records)


async def test_the_report_rounds_tool_calls_never_reach_the_toolbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[str] = []

    async def counted(toolbox: Toolbox, call: ToolCall, *args: object, **kwargs: object) -> object:
        made.append(call.id)
        return ToolOutcome(json.dumps({"ran": call.id}), {})

    monkeypatch.setattr(Toolbox, "call", counted)
    report = ModelTurn("Report.", (ToolCall("c2", "python", "{}"), ToolCall("c3", "shell", "{}")))
    model = Script([ModelTurn(None, (ToolCall("c1", "python", "{}"),)), report])
    transcript = await run_task(model, toolbox(), "go", run_id="r", max_turns=1)
    assert (transcript.stopped, transcript.final_text, made) == ("turn_limit", "Report.", ["c1"])
    assert [record.get("not_run") for record in transcript.tool_records] == [
        None,
        "turn_limit",
        "turn_limit",
    ]
