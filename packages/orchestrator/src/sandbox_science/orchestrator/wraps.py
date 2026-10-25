# SPDX-License-Identifier: MIT
"""What runs around each model call and each tool call of the tool loop: its wraps.

A wrap is given the next step of the call and answers for it: it may act before and
after it, try it again, or answer in its place. The loop composes them outermost first:

- a model call: ``OverflowRetry``, ``ModelRefusals``, each hook's ``wrap_model_call``
  in hook order, then the attempt, which raises the model's errors;
- a tool call, once its intent is saved: ``Runaway``, each hook's ``wrap_tool_call``
  in hook order, then the toolbox, whose gates may refuse it.

The loop's own wraps hold its run's transcript and change only what they did inline
before: they are its concerns, kept apart, not hooks a subsystem supplies
(docs/ARCHITECTURE.md, "The loop's composition points").
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .hooks import ModelHandler, RunView, ToolHandler, TurnHook, overrides
from .model import GATEWAY_BUDGET, ContextLimitError, GatewayBudgetError, ModelTurn, ToolCall
from .purpose import without

if TYPE_CHECKING:
    from .loop import Transcript
    from .tools import ToolOutcome

type ModelWrap = Callable[[ModelHandler], Awaitable[ModelTurn | None]]
type ToolWrap = Callable[[ToolCall, str, ToolHandler], Awaitable[ToolOutcome]]

RUNAWAY: Final = 25
"""The runaway guard's count: tool calls in a row that failed with the same error, or
that made the same call with the same arguments and got the same result, or answers in a
row that a closing held back (RS-6f)."""
POLLS: Final = frozenset(
    {
        "wait_jobs",
        "job_status",
        "list_jobs",
        "wait_delegations",
        "wait_agent_message",
        "delegation_status",
        "provision_status",
        "track_progress",
    }
)
"""Tools that watch something change: the same answer again is time passing, so the
runaway guard never counts them as repeated calls, only as failed ones."""

_log = logging.getLogger(__name__)


def wrap_model(inner: ModelHandler, wraps: Sequence[ModelWrap]) -> ModelHandler:
    """``inner`` inside ``wraps``, the first outermost."""
    handler = inner
    for wrap in reversed(wraps):
        handler = _model_step(wrap, handler)
    return handler


def wrap_tool(inner: ToolHandler, wraps: Sequence[ToolWrap]) -> ToolHandler:
    """``inner`` inside ``wraps``, the first outermost."""
    handler = inner
    for wrap in reversed(wraps):
        handler = _tool_step(wrap, handler)
    return handler


def _model_step(wrap: ModelWrap, handler: ModelHandler) -> ModelHandler:
    async def step() -> ModelTurn | None:
        return await wrap(handler)

    return step


def _tool_step(wrap: ToolWrap, handler: ToolHandler) -> ToolHandler:
    async def step(call: ToolCall, operation: str) -> ToolOutcome:
        return await wrap(call, operation, handler)

    return step


def hook_model_wraps(hooks: Sequence[TurnHook], run: RunView) -> list[ModelWrap]:
    """The model wraps of the hooks that override ``wrap_model_call``, in hook order."""
    return [_hook_model_wrap(hook, run) for hook in hooks if overrides(hook, "wrap_model_call")]


def hook_tool_wraps(hooks: Sequence[TurnHook], run: RunView) -> list[ToolWrap]:
    """The tool wraps of the hooks that override ``wrap_tool_call``, in hook order."""
    return [_hook_tool_wrap(hook, run) for hook in hooks if overrides(hook, "wrap_tool_call")]


def _hook_model_wrap(hook: TurnHook, run: RunView) -> ModelWrap:
    async def wrap(handler: ModelHandler) -> ModelTurn | None:
        return await hook.wrap_model_call(run, handler)

    return wrap


def _hook_tool_wrap(hook: TurnHook, run: RunView) -> ToolWrap:
    async def wrap(call: ToolCall, operation: str, handler: ToolHandler) -> ToolOutcome:
        return await hook.wrap_tool_call(run, call, operation, handler)

    return wrap


@dataclass
class OverflowRetry:
    """A model call refused for the conversation's length folds it and is tried again,
    for as long as the folds say a fold made room (CM-43 to CM-45). A retry that finds
    the conversation past the run's budget makes no call, and the refusal still stands,
    so the folds are asked again. A run without folds is never retried."""

    transcript: Transcript

    async def wrap(self, handler: ModelHandler) -> ModelTurn | None:
        transcript = self.transcript
        reply = await handler()
        while reply is None and transcript.overflowed:
            if not await transcript.context.overflow(call_index=transcript.turns):
                break
            reply = await handler()
        return reply


@dataclass
class ModelRefusals:
    """The model's refusals that end the call without a reply, not the run with an error:
    one for the conversation's length, which ``overflowed`` then says, and the gateway's
    for its budget, which ``halted`` says, its refusal kept in the run's summaries. Any
    other error passes, and fails the run in the model phase."""

    transcript: Transcript

    async def wrap(self, handler: ModelHandler) -> ModelTurn | None:
        transcript = self.transcript
        size = transcript.context.size
        try:
            return await handler()
        except ContextLimitError as error:
            _log.warning("the conversation, %d characters, exceeds the model's: %s", size, error)
            transcript.overflowed = True
        except GatewayBudgetError as error:
            _log.warning("the model gateway refused the call for its budget: %s", error)
            transcript.halted = GATEWAY_BUDGET
            # Whose budget it was, the gateway's or a configured local limit, stays on record.
            transcript.summaries["budget"] = {"refusal": str(error)[:500]}
        transcript.phase = "harness"
        return None


@dataclass
class Runaway:
    """The runaway guard (RS-6f): a cheap check that stops a run repeating itself without
    progress, and never one that works. It trips at ``limit`` in a row of one of:

    - tool calls that failed with the same error, the same kind and the same message,
      whatever the tool or its arguments;
    - tool calls of the same tool with the same arguments that got the same result,
      other than ``POLLS``, whose same answer is time passing; the arguments are
      compared without the call's ``purpose_note``, so rewording the purpose of a
      repeated call does not start the count again (Purpose 3);
    - closing answers that a hook held back for another turn (a ``retry``), with no
      tool call between them.

    Anything else in between starts the count again. As the outermost tool wrap, it sees
    each outcome before any notice rides on it; the loop tells it of closing answers.
    """

    limit: int = RUNAWAY
    key: tuple[str, ...] | None = None
    count: int = 0
    tripped: dict[str, str] | None = None
    """What tripped it: the rule and its detail, which the run's record keeps."""

    async def wrap(self, call: ToolCall, operation: str, handler: ToolHandler) -> ToolOutcome:
        outcome = await handler(call, operation)
        self.call(call, outcome)
        return outcome

    def call(self, call: ToolCall, outcome: ToolOutcome) -> None:
        """A tool call made, with its outcome before any notice rides on it."""
        error = outcome.record.get("error")
        digest = hashlib.sha256(outcome.text.encode("utf-8", "replace")).hexdigest()
        if error is not None:
            self._seen(("same_error", str(error), digest), f"failed with the same error ({error})")
        elif call.name in POLLS:
            self._seen(None, "")
        else:
            compared = without(call.arguments)
            arguments = hashlib.sha256(compared.encode("utf-8", "replace")).hexdigest()
            self._seen(
                ("same_call", call.name, arguments, digest),
                f"made the same {call.name} call with the same arguments and got the same result",
            )

    def answer(self, held: bool) -> None:
        """A closing answer, and whether a hook held it back for another turn."""
        if held:
            self._seen(("held",), "", calls=False)
        else:
            self._seen(None, "")

    def _seen(self, key: tuple[str, ...] | None, what: str, *, calls: bool = True) -> None:
        if key is None or key != self.key:
            self.key, self.count = key, 0
        if key is None:
            return
        self.count += 1
        if self.count >= self.limit and self.tripped is None:
            rule = key[0]
            if calls:
                detail = f"the last {self.count} tool calls {what}"
            else:
                detail = (
                    f"the last {self.count} answers were held back for another turn, with "
                    "no tool call between them"
                )
            self.tripped = {"rule": rule, "detail": detail}
