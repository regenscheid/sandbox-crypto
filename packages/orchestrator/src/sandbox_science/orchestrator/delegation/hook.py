# SPDX-License-Identifier: MIT
"""A parent run's delegations: its delegate tools, its children and their handbacks
(plans-and-delegation spec, PD-33 to PD-39).

``delegate`` writes the delegation and starts its child at once, as a task beside the
parent, and returns; the harness runs the child in role ``delegated`` and saves its
record, and the delegation's outcome is written when it ends. ``wait_delegations`` waits
on some children, all or the first to end, and ends early when the user writes to the
session. As a turn hook, ``Delegations``:

- offers, at each safe point, the outcomes of the session's delegations that no run has
  acknowledged, its own children's and those an earlier run left (PD-35), unless a wait
  already returned them;
- refuses a closing answer while its children run (PD-36): the run takes another turn
  with a notice naming them, or, with no turn or room left, stops without an answer.
  Outcomes the answer has not seen continue the run, as other notices do;
- cancels its running children when it ends, whatever ends it, and waits for their
  records and outcomes (PD-39), since its lease holds their tasks. A parent's record is
  therefore saved after its children's;
- acknowledges the outcomes its saved record names: those a model call saw as notices,
  and those a wait returned before a model call (``summary``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, cast

from pydantic import JsonValue

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ErrorKind, ExecutionContext, new_id
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.hooks import Closing, Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.notices import (
    DELEGATIONS,
    DELEGATIONS_ATTACHED,
    DELEGATIONS_RUNNING,
)
from sandbox_science.orchestrator.projects import RunRecord, run_name
from sandbox_science.orchestrator.worker_client import WorkerAPIError, dump

from .prompt import brief_text
from .records import Delegation, DelegationRecords, Ended
from .specs import SPECS, CheckBrief, WaitRequest

_log = logging.getLogger(__name__)

CHECK_TURNS: Final = 12
"""A check's turns, at most (coordinator, 2026-10-15)."""
CHECK_TOKENS: Final = 150_000
"""A check's tokens, at most: its calls' input and output, as the budget counts them."""
MAX_RUNNING: Final = 10
"""Delegated runs a session's tree runs at once (PD-38)."""
STOP_SECONDS: Final = 60.0
"""How long a parent's end waits for its cancelled children to save their records."""


@dataclass(frozen=True)
class Child:
    """A delegated run for the harness to run."""

    delegation_id: str
    run_id: str
    started_at: datetime
    task: str
    """Its first message: the brief."""
    max_turns: int
    tokens: int
    """The tokens its calls may use."""


@dataclass(frozen=True)
class ChildEnd:
    """How a delegated run ended, from its transcript."""

    stopped: str
    failed: bool
    final_text: str | None


class ChildRunner(Protocol):
    """Runs a delegated run to its end and saves its record; a cancel while ``stop`` is
    set ends it ``cancelled``."""

    def __call__(self, child: Child, stop: asyncio.Event) -> Awaitable[ChildEnd]: ...


def observed(end: ChildEnd) -> Ended:
    """How the harness saw a delegated run end (PD-17, PD-19): limits are not failures."""
    if end.failed:
        return "failed"
    match end.stopped:
        case "answered":
            return "returned"
        case "cancelled":
            return "cancelled"
        case "turn_limit" | "context_limit" | "run_budget" | "budget":
            return "limit_reached"
        case _:
            return "failed"


@dataclass
class _Running:
    delegation: Delegation
    task: asyncio.Task[None]
    stop: asyncio.Event


class Delegations(TurnHook):
    """One run's delegations; the run is ``parent``, the name its record will have."""

    name = "delegations"
    specs = SPECS

    def __init__(
        self,
        records: DelegationRecords,
        ledger: Ledger,
        context: ExecutionContext,
        *,
        parent: str,
        runner: ChildRunner,
        max_turns: int,
        wake: Callable[[], Awaitable[None]] | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """``max_turns`` bounds a child's turns; ``wake`` returns once the user writes to
        the session, which ends a wait."""
        self.records, self.ledger, self.context = records, ledger, context
        self.parent, self.runner, self.max_turns = parent, runner, max_turns
        self.wake, self.clock = wake, clock
        self.children: dict[str, _Running] = {}
        self.returned: set[str] = set()
        """Outcomes a wait returned that no model call has seen yet."""
        self.received: set[str] = set()
        """Outcomes a wait returned that a model call saw."""

    # --- The tools ------------------------------------------------------------------

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if name == "delegate":
            return await self._delegate(CheckBrief.model_validate(args))
        return await self._wait(WaitRequest.model_validate(args))

    async def _delegate(self, brief: CheckBrief) -> tuple[str, dict[str, Any]]:
        if len(self._running()) >= MAX_RUNNING:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE,
                f"limit_reached: {MAX_RUNNING} delegated runs are running in this session; "
                "wait for one to end",
            )
        project, session = self.context.project_id, self.context.session_id
        entry = await threaded(self.ledger.entry, project, brief.check.entry, brief.check.revision)
        delegation_id, run_id = new_id("dlg"), new_id("run")
        started = self.clock.now().astimezone(UTC)
        check: dict[str, JsonValue] = {
            "entry": entry.entry_id,
            "handle": entry.handle,
            "revision": brief.check.revision,
        }
        delegation = Delegation(
            delegation_id=delegation_id,
            parent=self.parent,
            run=run_name(started, run_id),
            kind="check",
            brief={
                "task": brief.task,
                "context": brief.context,
                "inputs": list(brief.inputs),
                "check": check,
            },
            started_at=started,
        )
        await threaded(self.records.start, project, session, delegation)
        child = Child(
            delegation_id,
            run_id,
            started,
            brief_text(brief, entry.handle),
            max_turns=min(CHECK_TURNS, self.max_turns),
            tokens=CHECK_TOKENS,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(self._run(child, stop), name=f"delegation {delegation_id}")
        self.children[delegation_id] = _Running(delegation, task, stop)
        started_with = {
            "delegation_id": delegation_id,
            "run": delegation.run,
            "state": "running",
            "check": {"entry": entry.handle, "revision": brief.check.revision},
            "limits": {"turns": child.max_turns, "tokens": child.tokens},
        }
        return dump(started_with), {"delegation": started_with}

    async def _run(self, child: Child, stop: asyncio.Event) -> None:
        """Run a child to its end, then write its outcome, however it ended."""
        ended: Ended = "failed"
        stopped, report = "error", None
        try:
            end = await self.runner(child, stop)
            ended, stopped, report = observed(end), end.stopped, end.final_text
        except asyncio.CancelledError:
            # Cancelled outside its loop, before it started or while it was saved.
            if (current := asyncio.current_task()) is not None:
                current.uncancel()
            ended, stopped = "cancelled", "cancelled"
        except Exception:
            _log.exception("delegated run %s failed", child.run_id)
        context = self.context
        try:
            await threaded(
                self.records.end,
                context.project_id,
                context.session_id,
                child.delegation_id,
                ended,
                stopped,
                report,
            )
        except Exception:
            _log.exception("the outcome of delegation %s could not be kept", child.delegation_id)

    async def _wait(self, request: WaitRequest) -> tuple[str, dict[str, Any]]:
        ids = list(dict.fromkeys(request.delegation_ids))
        unknown = [delegation for delegation in ids if delegation not in self.children]
        if unknown:
            raise WorkerAPIError(
                ErrorKind.NOT_FOUND, f"this run started no delegation {unknown[0]!r}"
            )
        pending = {self.children[i].task for i in ids if not self.children[i].task.done()}
        reason = "ended"
        if pending:
            when = asyncio.FIRST_COMPLETED if request.mode == "first" else asyncio.ALL_COMPLETED
            ending: asyncio.Future[None] = asyncio.ensure_future(_ended(pending, when))
            waiting = [ending]
            if self.wake is not None:
                waiting.append(asyncio.ensure_future(self.wake()))
            try:
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if ending not in done:
                    reason = "user_message"
            finally:
                for future in waiting:
                    future.cancel()  # the children's tasks go on
                for future in waiting:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await future
        context = self.context
        found = [
            await threaded(self.records.get, context.project_id, context.session_id, i) for i in ids
        ]
        self.returned.update(d.delegation_id for d in found if d.ended is not None)
        result = {
            "reason": reason,
            "delegations": [
                outcome(d) if d.ended is not None else {**_named(d), "state": "running"}
                for d in found
            ],
        }
        ended = [d.delegation_id for d in found if d.ended is not None]
        return dump(result), {"wait": {"reason": reason, "ended": ended}}

    # --- The turn hook --------------------------------------------------------------

    def lease(self) -> AbstractAsyncContextManager[None]:
        return self._held()

    @asynccontextmanager
    async def _held(self) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await self._cancel()

    async def _cancel(self) -> None:
        """Cancel the running children, children first, and wait for their outcomes."""
        running = self._running()
        for child in running:
            child.stop.set()
            child.task.cancel()
        if not running:
            return
        _, left = await asyncio.wait([child.task for child in running], timeout=STOP_SECONDS)
        for task in left:
            _log.error("%s did not end in %.0f s after its cancel", task.get_name(), STOP_SECONDS)

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        found = await self._fresh(run)
        if not found:
            return ()
        return (self._handback(found, DELEGATIONS_ATTACHED.text if point == "tool" else None),)

    async def model_returned(self, run: RunView) -> None:
        self.received |= self.returned
        self.returned.clear()

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        found = await self._fresh(run)
        notices = [self._handback(found)] if found else []
        running = self._running()
        if running:
            waiting = [{**_named(child.delegation), "state": "running"} for child in running]
            notices.append(Notice(self.name, (), DELEGATIONS_RUNNING.text, waiting))
            # With no turn or room left, the run stops: its end cancels them.
            stopped = "turn_limit" if run.turns_left <= 0 else "context_limit"
            return Closing("retry", tuple(notices), stopped=stopped)
        return Closing("continue", tuple(notices)) if notices else Closing()

    def summary(self) -> object | None:
        if not self.children and not self.received:
            return None
        return {"started": list(self.children), "received": sorted(self.received)}

    async def acknowledge(self, record: RunRecord) -> None:
        ids = set(record.delivered.get(self.name, ()))
        summary = record.summaries.get(self.name)
        if isinstance(summary, dict):
            received = cast(dict[str, object], summary).get("received")
            if isinstance(received, list):
                ids.update(str(i) for i in cast(list[object], received))
        if ids:
            context = self.context
            await threaded(
                self.records.acknowledge,
                context.project_id,
                context.session_id,
                sorted(ids),
                self.parent,
            )

    # --- Helpers --------------------------------------------------------------------

    def _running(self) -> list[_Running]:
        return [child for child in self.children.values() if not child.task.done()]

    async def _fresh(self, run: RunView) -> list[Delegation]:
        """The session's unacknowledged outcomes this run's conversation does not hold."""
        context = self.context
        found = await threaded(self.records.handbacks, context.project_id, context.session_id)
        held = set(run.offered.get(self.name, ())) | self.returned | self.received
        return [d for d in found if d.delegation_id not in held]

    def _handback(self, found: Sequence[Delegation], key: str | None = None) -> Notice:
        ids = tuple(d.delegation_id for d in found)
        return Notice(self.name, ids, key or DELEGATIONS.text, [outcome(d) for d in found])


async def _ended(tasks: set[asyncio.Task[None]], when: str) -> None:
    await asyncio.wait(tasks, return_when=when)


def _named(delegation: Delegation) -> dict[str, object]:
    check = cast(dict[str, object], delegation.brief.get("check") or {})
    return {
        "delegation_id": delegation.delegation_id,
        "run": delegation.run,
        "check": {"entry": check.get("handle"), "revision": check.get("revision")},
    }


def outcome(delegation: Delegation) -> dict[str, object]:
    """A delegation's outcome as its parent receives it (PD-20)."""
    return {
        **_named(delegation),
        "ended": delegation.ended,
        "stopped": delegation.stopped,
        "report": delegation.report,
    }
