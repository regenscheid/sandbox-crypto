# SPDX-License-Identifier: MIT
"""The ``ask_user`` tool: a main run asks the user a question and waits for the answer
(plans-and-delegation spec, PD-23 to PD-26).

The call waits inside itself. The run keeps its session and makes no model call until
the question closes, however long that takes: no default or timeout ends the wait (open
question 6). Only the run waits; its jobs and delegated runs go on, and so do the
user's other sessions (PD-26). The answer comes back as this call's result, so it
reaches the run and the call that asked (PD-24). A run's limits are checked between
model calls, so none ends the wait. A run that ends while it waits closes its question,
as no one is left to read the answer: ``cancelled`` when the user cancelled the run,
``interrupted`` when it stopped otherwise, as when the run service stops.

A run asks one question per reply, and at most ``MAX_ASKED`` in all, so it cannot bury
the user in questions; a replayed call is the question it asked, and counts once.

A delegated run is not offered the tool: one that needs the user ends ``blocked`` with
its question in its outcome (PD-41).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Annotated, Any, Final

from pydantic import Field

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext
from sandbox_science.contracts.model import Contract

from .aio import threaded
from .control import ControlError
from .control.errors import INVALID_REQUEST, POLICY_DENIED
from .hooks import Effect, RunView, TurnHook
from .model import ToolSpec
from .questions import MAX_DETAIL, MAX_LABEL, MAX_QUESTION, Question, QuestionOption, Questions

ASK_USER: Final = "ask_user"
POLL_SECONDS: Final = 1.0
MAX_ASKED: Final = 10
"""The most questions one run asks the user."""
CANCELLED: Final = "the run was cancelled"
STOPPED: Final = "the run stopped before it was answered"


class AskRequest(Contract):
    question: str
    options: Annotated[tuple[QuestionOption, ...], Field(max_length=4)] = ()
    multiple: bool = False


SPEC: Final = ToolSpec(
    ASK_USER,
    "Ask the user a question and wait for the answer: a preference or a research "
    "decision only they can make, such as which of two approaches to take or how wide "
    "the work should be. Ask when the answer changes what you do next: one question per "
    f"reply, and at most {MAX_ASKED} in a run. Give two to four options when the choices "
    "are clear, with multiple true if "
    "several may be chosen; the user may still answer in their own words, leave the "
    "decision to you, or cancel. This call waits until the user answers, however long "
    "that takes, and returns the answer. It is not a permission request: the policy "
    "decides what operations may run, and an answer here grants nothing.",
    {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUESTION,
                "description": "The question, with what the user needs to answer it",
            },
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "minLength": 1, "maxLength": MAX_LABEL},
                        "detail": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_DETAIL,
                            "description": "What choosing it means",
                        },
                    },
                    "required": ["label"],
                    "additionalProperties": False,
                },
                "description": "Two to four choices, each with a label of its own",
            },
            "multiple": {
                "type": "boolean",
                "description": "Several options may be chosen",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    },
)
SPECS: Final = (SPEC,)
EFFECTS: Final[dict[str, Effect]] = {ASK_USER: "read"}
"""Asking changes nothing a plan governs: a run may ask with its plan in draft."""


class QuestionTool(TurnHook):
    """A main run's ``ask_user``, whose questions name the run as its record is kept. As
    a turn hook, it learns where each of the model's replies begins."""

    specs = SPECS
    name = "questions"

    def __init__(
        self,
        questions: Questions,
        context: ExecutionContext,
        *,
        run: str,
        stop: asyncio.Event | None = None,
        poll_seconds: float | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """``stop`` is set when the user cancels the run (RS-11)."""
        self.questions, self.context, self.run = questions, context, run
        self.stop = stop
        self.poll = POLL_SECONDS if poll_seconds is None else poll_seconds
        self.clock = clock
        self.asked: set[str] = set()
        """The operations of the questions this run asked."""
        self.in_reply = False
        """The model's current reply asked a question already."""

    async def model_returned(self, run: RunView) -> None:
        self.in_reply = False

    async def call(
        self, args: dict[str, Any], *, operation: str, call_id: str
    ) -> tuple[str, dict[str, Any]]:
        """Ask, or find the question this operation asked already, and wait for it to
        close."""
        request = AskRequest.model_validate(args)
        if operation not in self.asked:
            if self.in_reply:
                raise ControlError(
                    INVALID_REQUEST,
                    "ask one question per reply: read this answer before the next question",
                )
            if len(self.asked) >= MAX_ASKED:
                raise ControlError(
                    POLICY_DENIED,
                    f"limit_reached: a run asks the user at most {MAX_ASKED} questions",
                )
        context = self.context
        project, session = context.project_id, context.session_id
        # The question is written in a thread, which a cancel does not stop: the close
        # below waits for it, so a question written as the run ended closes too.
        asking = asyncio.ensure_future(
            threaded(
                self.questions.ask,
                context,
                self.run,
                call_id if 0 < len(call_id.encode()) <= 256 else operation,
                operation,
                request.question,
                request.options,
                multiple=request.multiple,
            )
        )
        try:
            question = await asyncio.shield(asking)
            self.asked.add(operation)
            self.in_reply = True
            while question.state == "open":
                await self.clock.sleep(self.poll)
                question = await threaded(
                    self.questions.get, project, session, question.question_id
                )
        except asyncio.CancelledError:
            cancelled = self.stop is not None and self.stop.is_set()
            # The close outlives the cancel.
            with contextlib.suppress(Exception):
                await asyncio.shield(self._close(asking, cancelled=cancelled))
            raise
        return result(question)

    async def _close(self, asking: asyncio.Future[Question], *, cancelled: bool) -> None:
        """Close the question the run asked, once it is written, as the run ended."""
        question = await asking
        await threaded(
            self.questions.close,
            question.project,
            question.session,
            question=question.question_id,
            state="cancelled" if cancelled else "interrupted",
            reason=CANCELLED if cancelled else STOPPED,
        )


def result(question: Question) -> tuple[str, dict[str, Any]]:
    """What the model reads of a closed question, and what the transcript keeps."""
    answer = question.answer
    value: dict[str, Any] = {"question_id": question.question_id}
    if question.state == "answered" and answer is not None:
        if answer.kind == "picked":
            value |= {"answer": "picked", "picked": list(answer.picked)}
        elif answer.kind == "free":
            value |= {"answer": "own_words", "text": answer.text}
        else:
            value |= {
                "answer": "decide",
                "note": "The user leaves this to you: decide, and say what you chose and why.",
            }
    elif answer is not None and answer.kind == "cancel":
        value |= {
            "answer": "cancelled",
            "note": "The user cancelled the question without answering. Do not ask it "
            "again unless something changes: go on with what you can do, and say what "
            "you still need.",
        }
    else:
        value |= {
            "answer": None,
            "state": question.state,
            "note": f"The question closed without an answer: {question.reason}.",
        }
    record = question.model_dump(
        mode="json", exclude={"project", "session", "text", "options"}, exclude_none=True
    )
    return json.dumps(value, ensure_ascii=False), {"question": record}
