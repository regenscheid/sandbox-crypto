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
  used its share of it, or a delegated run's its token limit (``run_budget``); a
  delegated run whose tokens leave room for one more call makes it its report round
  (RS-6f). With
  delegation, the root's share bounds its whole tree, and the tree's calls in flight
  count toward both (``delegation/budget.py``, PD-38).
- ``SessionHeld`` refuses a delegated run's calls that may change something once its
  root run no longer holds the session; ``UserMessages`` does the same for the root.
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
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
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
from .notices import REDELIVERED
from .projects import ModelUsage, RunRecord
from .runstate import (
    MAX_EVENT_BYTES,
    PAGE,
    ChatMessage,
    EventKind,
    RunState,
    call_tokens,
)
from .tool_status import scalar as _scalar
from .tool_status import tool_outcome as tool_outcome
from .tools import bound

_log = logging.getLogger(__name__)

SOURCE: Final = "messages"
"""The user's messages, as ``RunRecord.delivered`` names them."""
POLL_SECONDS: Final = 1.0
"""How often a job wait looks for a user message."""
ANSWER_TOKENS: Final = 4096
"""The answer a call is allowed for when its cost is estimated before it is made."""
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
        holder: str | None = None,
        poll: float = POLL_SECONDS,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """``run`` is the name the run's record will be kept under; ``task`` the task
        the run starts with, or ``None`` when its task is the queued ``message_ids``
        (RS-6). ``prefix`` counts the messages the conversation starts with: the system
        message, the earlier conversation and the task. ``holder`` is the run that holds
        the session's lease for it: a delegated run's root (PD-32), or the run itself."""
        self.state, self.context, self.run, self.run_id = state, context, run, run_id
        self.holder = holder or run
        self.task, self.message_ids, self.prefix = task, tuple(message_ids), prefix
        self.requests: Callable[[], bool] | None = None
        """Whether a work request waits that the run may be offered: the run's document
        tool says, once it has one."""
        self.context_stop: Callable[[], str | None] | None = None
        """The folder's exact stop reason, independent of compact record previews."""
        self.unavailable: Mapping[str, str] = {}
        """The MCP servers whose listing failed at the run's start, with why: the
        ``started`` event says so (lab 41, B4)."""
        self.parent, self.delegation = parent, delegation
        self.poll, self.clock = poll, clock
        self.offered: set[str] = set()
        self.texts: dict[str, str] = {}
        self.shown: Mapping[str, str] = {}
        """The user's messages whose text the run's view holds already, by ID: each
        one's archive ID (docs/specs/context-management.md, CM-29a)."""

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
                holder=self.holder,
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
    """The project's daily cost limit, if it has one, kept at the run's safe points
    (RS-6a, RS-6c), and a delegated run's own token limit (PD-38)."""

    name = "budget"

    def __init__(self, chat: RunChat, share: float | None = None, *, tokens: int | None = None):
        """``share`` is the part of the project's daily cost limit one run may use, above
        0 and at most 1, if any; ``tokens`` is a delegated run's limit."""
        self.chat, self.share, self.tokens = chat, share, tokens

    async def halt(self, run: RunView) -> str | None:
        state, project = self.chat.state, self.chat.context.project_id
        limit = await threaded(state.cost_limit, project)
        if limit is not None:
            # Its own calls are among the project's: their events are written as they
            # return. A call without a known cost counts the limit as reached (lab 43, B2).
            if await threaded(state.over_cost_limit, project, self.chat.clock.now()):
                return "budget"
            own = sum(usage.cost or 0 for usage in run.usage)
            if self.share is not None and own > 0 and own >= self.share * limit:
                return "run_budget"
        # A delegated run stops before a call that would take it past its limit.
        used = sum(call_tokens(usage) for usage in run.usage)
        if self.tokens is not None and used + estimate(run) >= self.tokens:
            return "run_budget"
        return None

    async def last_call(self, run: RunView) -> str | None:
        """A delegated run whose tokens leave room for its next call but not for one more
        after it makes that call its report round (RS-6f), so that it reports what it found
        rather than stopping without a word."""
        if self.tokens is None:
            return None
        used = sum(call_tokens(usage) for usage in run.usage)
        # The next call sends at least what the last one sent and answered.
        last = call_tokens(run.usage[-1]) if run.usage else 0
        return "run_budget" if used + 2 * max(estimate(run), last) >= self.tokens else None


def estimate(run: RunView) -> int:
    """What a run's next call may cost: what it sends, as a quarter of its characters
    with the tool schemas, and an allowance for the answer."""
    return run.size // 4 + ANSWER_TOKENS


async def _held(chat: RunChat) -> bool:
    """Whether the run's session is still held for it."""
    context = chat.context
    held = await threaded(chat.state.lease, context.project_id, context.session_id)
    return held is not None and held.run == chat.holder


class SessionHeld(TurnHook):
    """A delegated run's calls that may change something, while its root holds the
    session. The root that lost it stops, and cancels its children (PD-39)."""

    name = "session"

    def __init__(self, chat: RunChat) -> None:
        self.chat = chat

    async def gate(self, effect: Effect) -> Refusal | None:
        if effect in ("read", "cancel") or await _held(self.chat):
            return None
        return Refusal(CONFLICT, f"run {self.chat.holder} no longer holds its session")


class UserMessages(TurnHook):
    """The user's queued messages, offered before each model call (RS-7, RS-8, RS-9)."""

    name = SOURCE

    def __init__(
        self,
        chat: RunChat,
        discover: Callable[[Sequence[str], Sequence[str]], Awaitable[Notice | None]] | None = None,
    ) -> None:
        """``discover`` gives the skills notice for the messages taken, by their texts and
        IDs, which goes just before them (docs/specs/skills.md, SK-35)."""
        self.chat, self.discover = chat, discover
        self.lost = False
        """Whether a call found the session held by another run."""

    async def _take(self) -> tuple[Notice, ...]:
        found = await self.chat.pending()
        if found:
            await self.chat.offer(found)
        shown = self.chat.shown
        # A message the view already holds, as one an interrupted run received, is named,
        # not repeated (CM-29a, RS-12); its delivery is acknowledged all the same.
        messages = tuple(
            Notice(SOURCE, (message.message_id,), REDELIVERED.text, shown[message.message_id])
            if message.message_id in shown
            else Notice(SOURCE, (message.message_id,), message.text, None, user=True)
            for message in found
        )
        fresh = [message for message in found if message.message_id not in shown]
        if fresh and self.discover is not None:
            skills = await self.discover(
                [message.text for message in fresh], [message.message_id for message in fresh]
            )
            if skills is not None:
                return (skills, *messages)
        return messages

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
        if effect in ("read", "cancel") or await _held(self.chat):
            return None
        self.lost = True
        return Refusal(CONFLICT, f"run {self.chat.run} no longer holds its session")

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
        if (
            self.chat.parent is None
            and record.stopped == "context_limit"
            and self.chat.context_stop is not None
            and self.chat.context_stop() == "research_state"
        ):
            # Common saved-run hook: resumed roots need the same consolidation
            # hold as ordinary service runs, which have separate finalization.
            context = self.chat.context
            after = 0
            while queued := await threaded(
                self.chat.state.messages,
                context.project_id,
                context.session_id,
                state="queued",
                after=after,
            ):
                await threaded(
                    self.chat.state.hold,
                    context.project_id,
                    context.session_id,
                    [message.message_id for message in queued],
                    "research_state",
                )
                after = queued[-1].number


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
                **_context_stop(record),
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
                    **(
                        {"connectors_unavailable": dict(chat.unavailable)}
                        if chat.unavailable
                        else {}
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
                        "outcome": tool_outcome(record),
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


def _context_stop(record: RunRecord) -> dict[str, Any]:
    """Why a run that folds stopped for its context, as its folds say (CM-43 to CM-45);
    or what tripped the runaway guard (RS-6f)."""
    runaway = record.summaries.get("runaway")
    if record.stopped == "runaway" and isinstance(runaway, dict):
        found = cast(dict[str, Any], runaway)
        return {"reason": found.get("rule"), "detail": found.get("detail")}
    folds = record.summaries.get("folds")
    if record.stopped != "context_limit" or not isinstance(folds, dict):
        return {}
    found = cast(dict[str, Any], folds)
    return {"reason": found.get("stopped"), "detail": found.get("detail")}


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
