# SPDX-License-Identifier: MIT
"""A run's part in its session's chat: the user's messages and the run's events.

docs/specs/run-service.md says what these do. ``RunChat`` is one run's view of its
session's records (``runstate.py``), which its two hooks and its job waits share:

- ``UserMessages`` offers each queued message before the next model call, as the
  user's own message (RS-7); at a closing answer, a message that waits makes the run
  continue (RS-8). After the run's record is saved, the messages a model call saw are
  marked delivered (RS-9). A message also ends a job wait (``RunChat.arrived``).
- ``EventLog`` writes what the run does as events, as it happens (RS-13, RS-14), from
  the conversation and the run's records. It comes last among the hooks, so that the
  notices of a safe point are in the conversation when it writes them. Its last
  events, and ``stopped``, are written when the record is saved.

``session_lease`` holds the session's run lease (RS-5) around a run and the saving of
its record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import AsyncGenerator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import asynccontextmanager
from typing import Any, Final, cast

from pydantic import JsonValue

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext, wait_or_sleep

from .aio import threaded
from .control import ControlError
from .control.errors import CONFLICT
from .hooks import Closing, Notice, Point, RunView, TurnHook
from .projects import ModelUsage, RunRecord
from .runstate import MAX_EVENT_BYTES, PAGE, ChatMessage, EventKind, RunState
from .tools import bound

_log = logging.getLogger(__name__)

SOURCE: Final = "messages"
"""The user's messages, as ``RunRecord.delivered`` names them."""
POLL_SECONDS: Final = 1.0
"""How often a job wait looks for a user message."""
TEXT_CHARS: Final = 16_000
"""The longest text an event keeps whole: longer text keeps its head and tail."""


class RunChat:
    """One run's messages and events. A failed event write is logged and the run goes
    on: events are for display, and the record is what counts (RS-13)."""

    def __init__(
        self,
        state: RunState,
        context: ExecutionContext,
        *,
        run: str,
        run_id: str,
        task: str | None,
        poll: float = POLL_SECONDS,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """``run`` is the name the run's record will be kept under; ``task`` the task
        the run starts with, if it is not made of messages."""
        self.state, self.context, self.run, self.run_id = state, context, run, run_id
        self.task, self.poll, self.clock = task, poll, clock
        self.offered: set[str] = set()
        self.texts: dict[str, str] = {}

    async def pending(self) -> list[ChatMessage]:
        """Queued messages this run has not offered yet, oldest first, every page."""
        context = self.context
        fresh: list[ChatMessage] = []
        after = 0
        while True:
            page = await threaded(
                self.state.messages,
                context.project_id,
                context.session_id,
                after=after,
                state="queued",
            )
            fresh.extend(message for message in page if message.message_id not in self.offered)
            if len(page) < PAGE:
                break
            after = page[-1].number
        self.texts.update((message.message_id, message.text) for message in fresh)
        return fresh

    async def offer(self, found: Sequence[ChatMessage]) -> None:
        """Mark messages offered to this run, which must still hold the session."""
        context = self.context
        ids = [message.message_id for message in found]
        await threaded(self.state.offer, context.project_id, context.session_id, ids, self.run)
        self.offered.update(ids)

    async def arrived(self) -> None:
        """Return once a message waits for this run."""
        while not await self.pending():
            await self.clock.sleep(self.poll)

    async def write(self, kind: EventKind, data: dict[str, Any]) -> None:
        context = self.context
        try:
            await threaded(
                self.state.append,
                context.project_id,
                context.session_id,
                self.run,
                kind,
                _bounded(data),
                holding=True,
            )
        except Exception:
            _log.exception("the %s event of run %s could not be written", kind, self.run)

    async def waiting(self, wait: dict[str, object] | None) -> None:
        """A job wait began, or ended (``None``)."""
        if wait is None:
            await self.write("waiting", {"job_ids": None, "mode": None})
        else:
            await self.write("waiting", {"job_ids": wait["job_ids"], "mode": wait["mode"]})


class UserMessages(TurnHook):
    """The user's queued messages, offered before each model call (RS-7, RS-8, RS-9)."""

    name = SOURCE

    def __init__(self, chat: RunChat) -> None:
        self.chat = chat

    async def _take(self) -> tuple[Notice, ...]:
        found = await self.chat.pending()
        if found:
            await self.chat.offer(found)
        return tuple(
            Notice(SOURCE, (message.message_id,), message.text, None, user=True)
            for message in found
        )

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        # Before each model call, after every other source's notices: the user's word last.
        return await self._take() if point == "turn" else ()

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        if run.turns_left <= 0:
            return Closing()  # the messages wait for the next run
        found = await self._take()
        return Closing("continue", found) if found else Closing()

    async def acknowledge(self, record: RunRecord) -> None:
        ids = record.delivered.get(SOURCE)
        if ids:
            context = self.chat.context
            await threaded(
                self.chat.state.deliver,
                context.project_id,
                context.session_id,
                list(ids),
                self.chat.run,
                holding=True,
            )


class EventLog(TurnHook):
    """The run's events, written as it goes (RS-13, RS-14). The last hook."""

    name = "events"

    def __init__(self, chat: RunChat) -> None:
        self.chat = chat
        self.started = False
        self.seen = 0
        """Messages of the conversation already written as events."""
        self.written: dict[str, set[str]] = {}
        """Each source's notice IDs written as events."""
        self.replies = self.calls = 0

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        await self._catch_up(run.messages, run.usage, run.tool_records, run.offered, run)
        if point == "turn":
            await self.chat.write("model", {"turn": run.turns + 1})
        return ()

    async def model_returned(self, run: RunView) -> None:
        await self._catch_up(run.messages, run.usage, run.tool_records, run.offered, run)

    async def observe_tool(self, run: RunView, record: dict[str, Any]) -> None:
        await self._catch_up(run.messages, run.usage, run.tool_records, run.offered, run)

    async def accept(self, run: RunView) -> None:
        await self._catch_up(run.messages, run.usage, run.tool_records, run.offered, run)

    async def acknowledge(self, record: RunRecord) -> None:
        await self._catch_up(record.messages, record.usage, record.tools, {}, None)
        await self.chat.write(
            "stopped",
            {
                "stopped": record.stopped,
                "failure": record.failure,
                "final_text": record.final_text,
                "record": self.chat.run,
            },
        )

    async def _catch_up(
        self,
        messages: Sequence[Mapping[str, Any]],
        usage: Sequence[ModelUsage],
        tools: Sequence[Mapping[str, Any]],
        offered: Mapping[str, AbstractSet[str]],
        run: RunView | None,
    ) -> None:
        chat = self.chat
        if not self.started:
            self.started = True
            pins = run.prompt_provenance if run is not None else None
            await chat.write(
                "started",
                {
                    "project": chat.context.project_id,
                    "session": chat.context.session_id,
                    "run_id": chat.run_id,
                    "message_ids": [],
                    "prompt_provenance": (
                        pins.model_dump(mode="json") if pins is not None else None
                    ),
                },
            )
            if chat.task is not None:
                await chat.write("user", {"message_id": None, "text": chat.task})
            # The system message and the task; later user messages are notices.
            self.seen = min(len(messages), 2 if chat.task is not None else 1)
        for source, ids in offered.items():
            new = sorted(set(ids) - self.written.get(source, set()))
            if not new:
                continue
            self.written.setdefault(source, set()).update(new)
            if source == SOURCE:
                for message_id in new:
                    text = chat.texts.get(message_id, "")
                    await chat.write("user", {"message_id": message_id, "text": text})
            else:
                await chat.write("notice", {"source": source, "ids": new})
        for message in messages[self.seen :]:
            if message.get("role") == "assistant":
                await self._reply(message, usage)
            elif message.get("role") == "tool":
                record: Mapping[str, Any] = tools[self.calls] if self.calls < len(tools) else {}
                self.calls += 1
                await chat.write(
                    "tool",
                    {
                        "turn": self.replies,
                        "call_id": message.get("tool_call_id"),
                        "name": record.get("tool"),
                        "result": str(message.get("content") or ""),
                        "outcome": _outcome(record),
                    },
                )
        self.seen = len(messages)

    async def _reply(self, message: Mapping[str, Any], usage: Sequence[ModelUsage]) -> None:
        used = usage[self.replies] if self.replies < len(usage) else None
        self.replies += 1
        calls = cast(list[dict[str, Any]], message.get("tool_calls") or [])
        await self.chat.write(
            "assistant",
            {
                "turn": self.replies,
                "text": message.get("content"),
                "tool_calls": [
                    {
                        "id": call.get("id"),
                        "name": call.get("function", {}).get("name"),
                        "arguments": call.get("function", {}).get("arguments"),
                    }
                    for call in calls
                ],
                "usage": used.model_dump(mode="json") if used is not None else None,
            },
        )


def _outcome(record: Mapping[str, Any]) -> dict[str, JsonValue]:
    """A tool call's state, error kind and exit code, as far as its record has them."""
    execution = record.get("execution")
    found = cast(Mapping[str, Any], execution) if isinstance(execution, dict) else record
    error = record.get("error")
    kind = cast(Mapping[str, Any], error).get("kind") if isinstance(error, dict) else error
    result = found.get("result")
    code = cast(Mapping[str, Any], result).get("exit_code") if isinstance(result, dict) else None
    return {
        "state": _scalar(found.get("state")),
        "error": _scalar(kind),
        "exit_code": _scalar(code),
    }


def _scalar(value: object) -> JsonValue:
    return value if isinstance(value, str | int | float | bool) or value is None else str(value)


def _bounded(data: dict[str, Any]) -> dict[str, JsonValue]:
    """``data`` with every text short enough for an event, keeping head and tail. Tool
    calls too many to fit are left out, with their count, so the rest survives."""
    limit = TEXT_CHARS
    while True:
        found = cast(dict[str, JsonValue], _bound(data, limit))
        if len(_encoded(found)) <= MAX_EVENT_BYTES - 1024:
            return found
        if limit > 64:
            limit //= 2
        elif isinstance(calls := data.get("tool_calls"), list):
            data = {**data, "tool_calls": [{"omitted": len(cast(list[object], calls))}]}
        else:
            return found


def _bound(value: object, limit: int) -> object:
    if isinstance(value, str):
        return bound(value, limit)
    if isinstance(value, dict):
        return {str(k): _bound(v, limit) for k, v in cast(dict[object, object], value).items()}
    if isinstance(value, list | tuple):
        return [_bound(item, limit) for item in cast(Sequence[object], value)]
    return _scalar(value)


def _encoded(data: dict[str, JsonValue]) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()


@asynccontextmanager
async def session_lease(
    state: RunState, context: ExecutionContext, run: str, *, clock: Clock = SYSTEM_CLOCK
) -> AsyncGenerator[None]:
    """Hold the session's run lease (RS-5) for ``run``, renewing it four times a lease;
    ``busy`` while another run holds it.

    A lease is lost when another holder took the session, or when no renewal succeeded
    for half the lease's time. The run then stops: the task that holds the lease is
    cancelled, and leaving raises ``conflict``.
    """
    holder = secrets.token_hex(16)
    project, session = context.project_id, context.session_id
    await threaded(state.acquire, project, session, run, holder)
    owner = asyncio.current_task()
    stopping, lost = asyncio.Event(), asyncio.Event()

    async def renewals() -> None:
        renewed = clock.monotonic()
        while not await wait_or_sleep(clock, stopping, state.lease_seconds / 4):
            taken = False
            try:
                await threaded(state.renew, project, session, holder)
                renewed = clock.monotonic()
                continue
            except ControlError as error:
                taken = error.kind == CONFLICT
                _log.warning("the lease of run %s was not renewed: %s", run, error.message)
            except Exception:
                _log.exception("the lease of run %s was not renewed", run)
            if taken or clock.monotonic() - renewed > state.lease_seconds / 2:
                _log.error("run %s lost the lease of session %s; it stops", run, session)
                lost.set()
                if owner is not None:
                    owner.cancel()
                return

    renewing = asyncio.create_task(renewals())
    try:
        yield
    except asyncio.CancelledError:
        if not lost.is_set() or owner is None:
            raise
        owner.uncancel()
        raise ControlError(
            CONFLICT, f"run {run} lost the lease of session {session!r} and stopped"
        ) from None
    finally:
        stopping.set()
        with contextlib.suppress(Exception):
            await renewing
        await threaded(state.release, project, session, holder)
