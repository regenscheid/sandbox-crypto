# SPDX-License-Identifier: MIT
"""A run's part in its session's chat: the user's messages and the run's events.

docs/specs/run-service.md says what these do. ``RunChat`` is one run's view of its
session's records (``runstate.py``), which its two hooks and its job waits share:

- ``UserMessages`` offers each queued message before the next model call, as the
  user's own message (RS-7); at a closing answer, a message that waits makes the run
  continue (RS-8). After the run's record is saved, the messages a model call saw are
  marked delivered (RS-9). A message, or a work request addressed to the session's
  main agent, also ends a job wait (``RunChat.arrived``).
- ``ModelBudget`` keeps the project's MAIN budget during a run the service starts
  (RS-6a): before each model call after the first, the run stops once the project has
  spent its budget over the last 24 hours (``budget``), or once the run's own calls have
  used its share of it (``run_budget``).
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
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Final, cast

from pydantic import JsonValue

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext, wait_or_sleep

from .aio import threaded
from .control import ControlError
from .control.errors import CONFLICT
from .hooks import Closing, Effect, Notice, Point, Refusal, RunView, TurnHook
from .projects import ModelUsage, RunRecord
from .runstate import MAX_EVENT_BYTES, PAGE, ChatMessage, EventKind, RunState, cost
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
        message_ids: Sequence[str] = (),
        prefix: int = 2,
        parent: str | None = None,
        delegation: str | None = None,
        poll: float = POLL_SECONDS,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """``run`` is the name the run's record will be kept under; ``task`` the task
        the run starts with, or ``None`` when its task is the queued ``message_ids``
        (RS-6). ``prefix`` counts the messages the conversation starts with: the system
        message, the earlier conversation and the task."""
        self.state, self.context, self.run, self.run_id = state, context, run, run_id
        self.task, self.message_ids, self.prefix = task, tuple(message_ids), prefix
        self.requests: Callable[[], bool] | None = None
        """Whether a work request waits that the run may be offered: the run's document
        tool says, once it has one."""
        self.parent, self.delegation = parent, delegation
        self.poll, self.clock = poll, clock
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
        """Return once a message, or a work request it may be offered, waits for this
        run."""
        requests = self.requests
        while not await self.pending() and not (requests is not None and await threaded(requests)):
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


class ModelBudget(TurnHook):
    """The project's MAIN budget, kept at the run's safe points (RS-6a)."""

    name = "budget"

    def __init__(self, chat: RunChat, share: float) -> None:
        """``share`` is the part of the project's budget one run may use, above 0 and at
        most 1."""
        self.chat, self.share = chat, share

    async def halt(self, run: RunView) -> str | None:
        state, project = self.chat.state, self.chat.context.project_id
        # Its own calls are among the project's: their events are written as they return.
        if await threaded(state.over_budget, project, self.chat.clock.now()):
            return "budget"
        used = sum(cost(usage) for usage in run.usage)
        if used >= self.share * await threaded(state.budget, project):
            return "run_budget"
        return None


class UserMessages(TurnHook):
    """The user's queued messages, offered before each model call (RS-7, RS-8, RS-9)."""

    name = SOURCE

    def __init__(self, chat: RunChat) -> None:
        self.chat = chat
        self.lost = False
        """Whether a call found the session held by another run."""

    async def _take(self) -> tuple[Notice, ...]:
        found = await self.chat.pending()
        if found:
            await self.chat.offer(found)
        return tuple(
            Notice(SOURCE, (message.message_id,), message.text, None, user=True)
            for message in found
        )

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if self.lost and point == "turn":
            raise ControlError(CONFLICT, f"run {self.chat.run} no longer holds its session")
        # Before each model call, after every other source's notices: the user's word last.
        return await self._take() if point == "turn" else ()

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        if run.turns_left <= 0:
            return Closing()  # the messages wait for the next run
        found = await self._take()
        return Closing("continue", found) if found else Closing()

    def summary(self) -> object | None:
        """The messages the run was started for, which its record's task joins."""
        return {"initial": list(self.chat.message_ids)} if self.chat.message_ids else None

    async def gate(self, effect: Effect) -> Refusal | None:
        """Before a call that may change something, the run must still hold its session.
        One that lost it, as after a suspend, is refused the call, and stops before its
        next model call."""
        if effect in ("read", "cancel"):
            return None
        context = self.chat.context
        held = await threaded(self.chat.state.lease, context.project_id, context.session_id)
        if held is None or held.run != self.chat.run:
            self.lost = True
            return Refusal(CONFLICT, f"run {self.chat.run} no longer holds its session")
        return None

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
                    "role": "main" if chat.parent is None else "delegated",
                    "parent": chat.parent,
                    "delegation": chat.delegation,
                    "message_ids": list(chat.message_ids),
                    "prompt_provenance": (
                        pins.model_dump(mode="json") if pins is not None else None
                    ),
                },
            )
            if chat.task is not None:
                await chat.write("user", {"message_id": None, "text": chat.task})
            # What the run starts with; later user messages are notices.
            self.seen = min(len(messages), chat.prefix)
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
    for half the lease's time, by the monotonic clock or by wall time (a suspend stops
    the one, not the lease's expiry). The run then stops: the task that holds the lease
    is cancelled, and leaving raises ``conflict``.
    """
    holder = secrets.token_hex(16)
    project, session = context.project_id, context.session_id
    await threaded(state.acquire, project, session, run, holder)
    owner = asyncio.current_task()
    stopping, lost = asyncio.Event(), asyncio.Event()

    def overdue(renewed: float, wall: datetime) -> bool:
        # Wall time too: a suspend stops the monotonic clock, not the lease's expiry.
        half = state.lease_seconds / 2
        return clock.monotonic() - renewed > half or (clock.now() - wall).total_seconds() > half

    async def renewals() -> None:
        renewed, wall = clock.monotonic(), clock.now()
        while not await wait_or_sleep(clock, stopping, state.lease_seconds / 4):
            taken = overdue(renewed, wall)
            if not taken:
                try:
                    await threaded(state.renew, project, session, holder)
                    renewed, wall = clock.monotonic(), clock.now()
                    continue
                except ControlError as error:
                    taken = error.kind == CONFLICT
                    _log.warning("the lease of run %s was not renewed: %s", run, error.message)
                except Exception:
                    _log.exception("the lease of run %s was not renewed", run)
            if taken or overdue(renewed, wall):
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
