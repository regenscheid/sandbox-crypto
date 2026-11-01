# SPDX-License-Identifier: MIT
"""The tool loop's wraps (``wraps.py``) and the hooks' wrap points, one at a time."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.transport import Channel, TransportError
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.folding import FoldPoint
from sandbox_science.orchestrator.hooks import (
    ModelHandler,
    RunView,
    ToolHandler,
    TurnHook,
    wraps_calls,
)
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import (
    GATEWAY_BUDGET,
    ChatModel,
    ContextLimitError,
    GatewayBudgetError,
    Message,
    ModelTurn,
    ToolCall,
    ToolSpec,
)
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.orchestrator.wraps import (
    ModelRefusals,
    OverflowRetry,
    Runaway,
    hook_model_wraps,
    hook_tool_wraps,
    wrap_model,
    wrap_tool,
)

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
CALL = ToolCall("c1", "python", "{}")
REPLY = ModelTurn("Done.")


def toolbox() -> Toolbox:
    return Toolbox(cast(WorkerClient, object()), CONTEXT)


class Folds:
    def __init__(self, answers: Sequence[bool]) -> None:
        self.answers = list(answers)
        self.asked = 0

    async def before_call(self, point: FoldPoint) -> bool:
        return True

    async def overflow(self, point: FoldPoint) -> bool:
        self.asked += 1
        return self.answers.pop(0)


def transcript(folds: Folds | None = None) -> Transcript:
    return Transcript(context=AgentContext(messages=[], folds=folds))


class Steps:
    """A model handler answering as scripted; an answer ``None`` sets ``overflowed``, as an
    attempt refused for the conversation's length leaves it."""

    def __init__(self, run: Transcript, answers: Sequence[ModelTurn | None]) -> None:
        self.run, self.answers, self.calls = run, list(answers), 0

    async def __call__(self) -> ModelTurn | None:
        self.calls += 1
        answer = self.answers.pop(0)
        self.run.overflowed = answer is None
        return answer


# --- Composition ----------------------------------------------------------------------


async def test_model_wraps_run_the_first_outermost() -> None:
    order: list[str] = []

    def named(name: str):
        async def wrap(handler: ModelHandler) -> ModelTurn | None:
            order.append(name + ">")
            reply = await handler()
            order.append("<" + name)
            return reply

        return wrap

    async def inner() -> ModelTurn | None:
        order.append("call")
        return REPLY

    assert await wrap_model(inner, [named("a"), named("b")])() is REPLY
    assert order == ["a>", "b>", "call", "<b", "<a"]
    assert wrap_model(inner, []) is inner


async def test_tool_wraps_run_the_first_outermost_and_may_answer_in_place() -> None:
    order: list[str] = []

    async def outer(call: ToolCall, operation: str, handler: ToolHandler) -> ToolOutcome:
        order.append("outer")
        return await handler(call, operation)

    async def refuse(call: ToolCall, operation: str, handler: ToolHandler) -> ToolOutcome:
        order.append("refuse")
        return ToolOutcome("refused", {"error": "policy_denied"})

    async def inner(call: ToolCall, operation: str) -> ToolOutcome:
        order.append("inner")
        return ToolOutcome("ran", {})

    outcome = await wrap_tool(inner, [outer, refuse])(CALL, "op")
    assert (outcome.text, order) == ("refused", ["outer", "refuse"])


# --- OverflowRetry -------------------------------------------------------------------


async def test_overflow_retry_tries_again_while_a_fold_makes_room() -> None:
    folds = Folds([True, True])
    run = transcript(folds)
    steps = Steps(run, [None, None, REPLY])
    assert await OverflowRetry(run).wrap(steps) is REPLY
    assert (steps.calls, folds.asked) == (3, 2)


async def test_overflow_retry_stops_when_no_fold_makes_room() -> None:
    folds = Folds([True, False])
    run = transcript(folds)
    steps = Steps(run, [None, None, REPLY])
    assert await OverflowRetry(run).wrap(steps) is None
    assert (steps.calls, folds.asked) == (2, 2)


async def test_overflow_retry_leaves_a_reply_and_other_refusals_alone() -> None:
    folds = Folds([True])
    run = transcript(folds)
    steps = Steps(run, [REPLY])
    assert await OverflowRetry(run).wrap(steps) is REPLY

    async def budget() -> ModelTurn | None:
        return None  # a refusal that is not for the conversation's length

    assert await OverflowRetry(run).wrap(budget) is None
    assert (steps.calls, folds.asked) == (1, 0)


async def test_overflow_retry_never_retries_a_run_without_folds() -> None:
    run = transcript()
    steps = Steps(run, [None, REPLY])
    assert await OverflowRetry(run).wrap(steps) is None
    assert steps.calls == 1


# --- ModelRefusals -------------------------------------------------------------------


async def test_a_context_error_ends_the_call_without_a_reply() -> None:
    run = transcript()
    run.phase = "model"

    async def refused() -> ModelTurn | None:
        raise ContextLimitError("too long", status=400)

    assert await ModelRefusals(run).wrap(refused) is None
    assert (run.overflowed, run.halted, run.phase, run.summaries) == (True, None, "harness", {})


async def test_a_budget_refusal_halts_with_its_refusal_kept() -> None:
    run = transcript()
    run.phase = "model"

    async def refused() -> ModelTurn | None:
        raise GatewayBudgetError("spent " + "x" * 600, status=400)

    assert await ModelRefusals(run).wrap(refused) is None
    assert (run.overflowed, run.halted, run.phase) == (False, GATEWAY_BUDGET, "harness")
    refusal = cast(dict[str, str], run.summaries["budget"])["refusal"]
    assert len(refusal) == 500


async def test_other_model_errors_pass_in_the_model_phase() -> None:
    run = transcript()
    run.phase = "model"

    async def broken() -> ModelTurn | None:
        raise RuntimeError("broken")

    with pytest.raises(RuntimeError):
        await ModelRefusals(run).wrap(broken)
    assert (run.phase, run.halted, run.overflowed) == ("model", None, False)

    async def answered() -> ModelTurn | None:
        return REPLY

    assert await ModelRefusals(run).wrap(answered) is REPLY


# --- Runaway -------------------------------------------------------------------------


async def test_the_runaway_wrap_counts_each_outcome_and_returns_it_unchanged() -> None:
    guard = Runaway(limit=2)
    failed = ToolOutcome("no such tool", {"error": "invalid_request"})

    async def run(call: ToolCall, operation: str) -> ToolOutcome:
        return failed

    assert await guard.wrap(CALL, "op1", run) is failed
    assert guard.tripped is None
    assert await guard.wrap(CALL, "op2", run) is failed
    assert cast(dict[str, str], guard.tripped)["rule"] == "same_error"


async def test_the_runaway_wrap_counts_no_call_that_raised() -> None:
    guard = Runaway(limit=1)

    async def lost(call: ToolCall, operation: str) -> ToolOutcome:
        raise ConnectionError("the worker is gone")

    with pytest.raises(ConnectionError):
        await guard.wrap(CALL, "op", lost)
    assert (guard.count, guard.tripped) == (0, None)


# --- The hooks' wrap points ----------------------------------------------------------


class Plain(TurnHook):
    name = "plain"


class Wrapping(TurnHook):
    """Counts model attempts; answers every tool call itself."""

    name = "wrapping"

    def __init__(self) -> None:
        self.attempts = 0
        self.tools: list[str] = []

    async def wrap_model_call(self, run: RunView, handler: ModelHandler) -> ModelTurn | None:
        self.attempts += 1
        return await handler()

    async def wrap_tool_call(
        self, run: RunView, call: ToolCall, operation: str, handler: ToolHandler
    ) -> ToolOutcome:
        self.tools.append(call.id)
        return ToolOutcome("answered by the hook", {"error": "unavailable"})


def test_only_hooks_that_override_a_wrap_point_are_composed() -> None:
    run = transcript()
    hooks = [Plain(), Wrapping()]
    assert (len(hook_model_wraps(hooks, run)), len(hook_tool_wraps(hooks, run))) == (1, 1)
    assert (wraps_calls(Plain()), wraps_calls(Wrapping())) == (False, True)


class Script:
    def __init__(self, steps: Sequence[ModelTurn | Exception]) -> None:
        self.steps = list(steps)

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


async def test_a_hook_wraps_each_attempt_and_each_call_that_runs() -> None:
    """A hook's model wrap sees each attempt, the one after a fold and the report round's
    among them; its tool wrap sees each call that runs, and the runaway guard what it
    returns. The report round's calls pass no wrap."""
    hook = Wrapping()
    report = ModelTurn("Report.", (ToolCall("c9", "python", "{}"),))
    model = Script(
        [
            ContextLimitError("too long", status=400),
            ModelTurn(None, (ToolCall("c1", "python", "{}"),)),
            report,
        ]
    )
    run = await run_task(
        model, toolbox(), "go", run_id="r", max_turns=1, hooks=[hook], folds=Folds([True])
    )
    assert (run.stopped, run.final_text) == ("turn_limit", "Report.")
    assert (hook.attempts, hook.tools) == (3, ["c1"])
    assert run.tool_records[0]["error"] == "unavailable"
    assert run.tool_records[1]["not_run"] == "turn_limit"


async def test_a_confined_attempt_refuses_a_hook_that_wraps_calls() -> None:
    host = HostLoops(cast(Channel, object()), frozenset({"p"}))
    model = cast(ChatModel, object())
    with pytest.raises(TransportError, match="wraps model or tool calls") as refused:
        await host.run(model, toolbox(), "go", hooks=(Plain(), Wrapping()))
    assert refused.value.kind == "denied"
    # A hook that wraps nothing passes this check, and meets the next one.
    with pytest.raises(TransportError, match="host model execution"):
        await host.run(model, toolbox(), "go", hooks=(Plain(),))
