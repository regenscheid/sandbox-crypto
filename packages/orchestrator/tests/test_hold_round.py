# SPDX-License-Identifier: MIT
"""``hold`` keeps the first answer through the turn answering its review (CR-48).

On the cluster on 2026-11-27, with ``final_review = "hold"``, the "being reviewed" notice
came with the answer, the answer itself when the final review ended, and then the
answering turn's different text. The run's events now keep the first answer back while
its round runs: the user sees the round's answer, the first only when it becomes the
run's answer (the round ended without one, or the hold's limit passed). The run's
record keeps both in its messages."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, ClassVar, cast

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.chat import HELD, HOLD_ENDED, EventLog
from sandbox_science.orchestrator.hooks import Closing, Notice, RunView, TurnHook
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import ModelTurn
from sandbox_science.orchestrator.tools import Toolbox

from .scripted import ScriptedModel, ToolCall

FIRST = "The count is 16; the task is complete."
SECOND = "Corrected: the count is 15."
CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")


class Chat:
    context = CONTEXT
    run_id, run, parent, delegation = "r", "run-r", None, None
    message_ids: tuple[str, ...] = ()
    unavailable: ClassVar[dict[str, str]] = {}
    connectors = None
    task = None
    prefix = 2

    def __init__(self) -> None:
        self.written: list[tuple[str, dict[str, Any]]] = []

    async def write(self, kind: str, data: dict[str, Any]) -> None:
        self.written.append((kind, data))

    def texts(self) -> list[str]:
        return [str(d["text"]) for kind, d in self.written if kind == "assistant" and d["text"]]

    def notices(self) -> list[str]:
        return [str(d.get("text")) for kind, d in self.written if kind == "notice"]


class Review(TurnHook):
    """A final review in ``hold``: its first closing asks for a round with the answer
    held, and it holds answers until its second closing, or until ``expire()``."""

    name = "reviews"

    def __init__(self, chat: Chat) -> None:
        self.chat = chat
        self.closings = 0
        self.expired = False
        self.during: list[str] = []
        """The answers the user had seen when the round's closing came."""

    @property
    def holds_answers(self) -> bool:
        return self.closings < 2 and not self.expired

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        self.closings += 1
        if self.closings == 1:
            notice = Notice("reviews", ("f1",), "Review findings.", {"finding": "f1"})
            return Closing("respond", (notice,), hold_answer=True)
        self.during = self.chat.texts()
        return Closing()


async def _run(
    model: ScriptedModel, review: Review, events: EventLog, *extra: TurnHook, **options: Any
) -> Transcript:
    return await run_task(
        model,
        Toolbox(cast(Any, object()), CONTEXT),
        "Count them.",
        run_id="r",
        hooks=[review, *extra, events],
        **options,
    )


async def test_the_user_sees_the_answering_turns_answer_not_the_first() -> None:
    chat = Chat()
    review = Review(chat)
    model = ScriptedModel([ModelTurn(FIRST), ModelTurn(SECOND)])
    transcript = await _run(model, review, EventLog(cast(Any, chat)))
    assert (transcript.answer, transcript.final_text) == (SECOND, SECOND)
    # While the round's answer waited for its closing, neither answer was shown.
    assert review.during == []
    assert chat.texts() == [SECOND]
    assert HELD in chat.notices()
    assert HOLD_ENDED not in chat.notices()
    # The first answer is in the run's messages, and is the answer the round was held for.
    assert FIRST in [m.get("content") for m in transcript.context.messages]
    assert transcript.held_answer == FIRST


async def test_a_round_that_ends_without_an_answer_releases_the_first() -> None:
    chat = Chat()
    review = Review(chat)
    calls = [ToolCall(f"c{n}", "no_such_tool", "{}") for n in range(4)]
    model = ScriptedModel([ModelTurn(FIRST), *(ModelTurn(None, (call,)) for call in calls)])
    events = EventLog(cast(Any, chat))
    transcript = await _run(model, review, events, max_turns=3)
    assert transcript.answer == FIRST
    assert chat.texts() == []  # not while the run went on
    record = SimpleNamespace(
        messages=transcript.context.messages,
        usage=transcript.context.usage,
        tools=transcript.tool_records,
        answer=transcript.answer,
        stopped=transcript.stopped,
        failure=None,
        final_text=transcript.final_text,
        summaries={},
    )
    await events.acknowledge(cast(Any, record))
    assert chat.texts() == [FIRST]


async def test_the_holds_limit_releases_the_first_answer_and_says_so() -> None:
    chat = Chat()
    review = Review(chat)

    class Expiring(TurnHook):
        """Ends the hold once the answering turn has started."""

        name = "expiring"

        async def model_returned(self, run: RunView) -> None:
            if run.holding:
                review.expired = True

    model = ScriptedModel(
        [
            ModelTurn(FIRST),
            ModelTurn(None, (ToolCall("c1", "no_such_tool", json.dumps({})),)),
            ModelTurn(SECOND),
        ]
    )
    transcript = await _run(model, review, EventLog(cast(Any, chat)), Expiring())
    assert transcript.answer == FIRST
    assert transcript.final_text == SECOND
    assert chat.texts() == [FIRST, SECOND]
    assert HOLD_ENDED in chat.notices()
