# SPDX-License-Identifier: MIT
"""A main run's delegations: its delegate tools, its children and their handbacks
(plans-and-delegation spec, PD-33 to PD-40).

``delegate`` writes the delegation and starts its child at once, as a task beside the
parent, and returns; the harness runs the child in role ``delegated`` and saves its
record, and the delegation's outcome is written when it ends. ``wait_delegations`` waits
on some children, all or the first to end, and ends early when the user writes to the
session; it returns whole outcomes, as many as fit, of any of the session's ended
delegations. ``delegation_status`` reads one; ``cancel_delegation`` cancels one the run
started and returns its outcome (PD-39). The tools are offered only when the project
lets its runs delegate. An outcome gives the child's result when it validated, and
otherwise its answer as returned with the errors (PD-17, PD-18, PD-20). As a turn hook,
``Delegations``, which every main run with the stores has:

- ends ``interrupted``, as the run starts, the session's delegations still running,
  whose process ended, and writes the ``stopped`` event of any of the session's
  delegated runs that lacks it, such as one whose command-line root lost its lease
  (PD-40);
- offers, at each safe point, the session's outcomes that no run has acknowledged,
  its own children's and those an earlier run left (PD-35), each as a short summary,
  together at most ``max_chars``; the rest wait for a later safe point;
- refuses a closing answer while its children run (PD-36): the run takes another turn
  with a notice naming them, or, with no turn or room left, stops without an answer.
  Outcomes the answer has not seen continue the run, as other notices do;
- cancels its running children when it ends, whatever ends it, and waits for their
  records and outcomes (PD-39), since its lease holds their tasks. A child that has not
  ended in ``STOP_SECONDS`` is recorded ``interrupted``, so a parent's record follows its
  children's outcomes;
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
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.hooks import Closing, Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.model import ToolSpec
from sandbox_science.orchestrator.notices import (
    DELEGATIONS,
    DELEGATIONS_ATTACHED,
    DELEGATIONS_RUNNING,
)
from sandbox_science.orchestrator.projects import RunRecord, run_name
from sandbox_science.orchestrator.worker_client import WorkerAPIError, dump

from .budget import FLOOR, MIN_CHILD, TreeBudget
from .outcome import Target, proof_reads
from .prompt import brief_text, check_task, evidence_line
from .records import Delegation, DelegationRecords, Ended, Ending
from .specs import SPECS, CheckBrief, Named, WaitRequest

_log = logging.getLogger(__name__)

CHECK_TURNS: Final = 12
"""A check's turns, at most (coordinator, 2026-10-15)."""
CHECK_TOKENS: Final = 150_000
"""A check's tokens, at most: its calls' input and output, as the budget counts them."""
MAX_RUNNING: Final = 10
"""Delegated runs a session's tree runs at once (PD-38)."""
STOP_SECONDS: Final = 60.0
"""How long a parent's end, or a cancel, waits for a cancelled child's record."""
SUMMARY_CHARS: Final = 300
"""The head of a report a notice shows; ``wait_delegations`` returns the whole."""
WRITES: Final = 3
"""Tries at writing an outcome; one never written ends ``interrupted`` at the session's
next main run."""


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
    target: Target
    """The entry and revision it checks, which its result must name."""
    tree: TreeBudget | None = None
    """The root's share of the model budget, which the child's calls spend (PD-38)."""


@dataclass(frozen=True)
class ChildEnd:
    """How a delegated run ended, from its transcript and its hooks."""

    stopped: str
    failed: bool
    final_text: str | None
    refused: str | None = None
    """Why its plan fence refused its calls, if it did."""
    result: dict[str, JsonValue] | None = None
    """Its result, as its output schema validated it."""
    errors: tuple[str, ...] = ()
    alias: str | None = None
    models: tuple[str, ...] = ()
    read_entry: bool | None = None
    """Whether it read the entry it checks at that revision."""
    read_proofs: bool | None = None
    """Whether it read the target of each of the entry's proofs at its pinned revision."""
    used: int = 0
    """The tokens its calls used, as the budget counts them."""


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
    run_id: str
    task: asyncio.Task[None]
    stop: asyncio.Event
    limits: dict[str, int]
    """Its turns and tokens, at most."""


class Delegations(TurnHook):
    """One main run's delegations; the run is ``parent``, the name its record will have."""

    name = "delegations"

    def __init__(
        self,
        records: DelegationRecords,
        ledger: Ledger,
        context: ExecutionContext,
        *,
        parent: str,
        runner: ChildRunner,
        max_turns: int,
        offer: bool,
        wake: Callable[[], Awaitable[None]] | None = None,
        over_budget: Callable[[], Awaitable[bool]] | None = None,
        tree: TreeBudget | None = None,
        max_chars: int = 5000,
        output_chars: int = 20000,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """``offer`` says whether the run gets the tools; ``max_turns`` bounds a child's
        turns; ``wake`` returns once the user writes to the session, which ends a wait;
        ``over_budget`` says whether the project spent its model budget, and ``tree`` is the
        run's share of it, from which each child's tokens are reserved. Notices together
        take at most ``max_chars``, and a wait's result at most ``output_chars``."""
        self.records, self.ledger, self.context = records, ledger, context
        self.parent, self.runner, self.max_turns = parent, runner, max_turns
        self.specs: tuple[ToolSpec, ...] = SPECS if offer else ()
        self.wake, self.over_budget, self.clock = wake, over_budget, clock
        self.tree = tree
        self.turns_left = max_turns
        """The run's turns left at its last safe point: a child gets no more (PD-38)."""
        self.max_chars, self.output_chars = max_chars, output_chars
        self.children: dict[str, _Running] = {}
        self.returned: set[str] = set()
        """Outcomes a wait returned that no model call has seen yet."""
        self.received: set[str] = set()
        """Outcomes a wait returned that a model call saw."""

    def live(self) -> set[str]:
        """The run IDs of the running children, whose kernels the parent's kernel check
        leaves alone (PD-37)."""
        return {child.run_id for child in self._running()}

    # --- The tools ------------------------------------------------------------------

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        match name:
            case "delegate":
                return await self._delegate(CheckBrief.model_validate(args))
            case "wait_delegations":
                return await self._wait(WaitRequest.model_validate(args))
            case "delegation_status":
                return await self._status(Named.model_validate(args).delegation_id)
            case _:
                return await self._cancel_one(Named.model_validate(args).delegation_id)

    async def _delegate(self, brief: CheckBrief) -> tuple[str, dict[str, Any]]:
        if len(self._running()) >= MAX_RUNNING:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE,
                f"limit_reached: {MAX_RUNNING} delegated runs are running in this session; "
                "wait for one to end",
            )
        if self.turns_left <= 0:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE,
                "limit_reached: this run has no turn left to wait for a delegated run",
            )
        if self.over_budget is not None and await self.over_budget():
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE, "limit_reached: the project has spent its model budget"
            )
        project, session = self.context.project_id, self.context.session_id
        entry = await threaded(self.ledger.entry, project, brief.check.entry, brief.check.revision)
        delegation_id, run_id = new_id("dlg"), new_id("run")
        tokens = CHECK_TOKENS
        if self.tree is not None:
            reserved = await self.tree.reserve(delegation_id, CHECK_TOKENS)
            if reserved is None:
                raise WorkerAPIError(
                    ErrorKind.UNAVAILABLE,
                    f"limit_reached: this run's share of the model budget leaves less than "
                    f"{MIN_CHILD} tokens for a check after the {FLOOR} it keeps for itself; "
                    "wait for a running check to end, or do the work yourself",
                )
            tokens = reserved
        started = self.clock.now().astimezone(UTC)
        check: dict[str, JsonValue] = {
            "entry": entry.entry_id,
            "handle": entry.handle,
            "revision": brief.check.revision,
        }
        # The platform writes the task and lists the entry's evidence: the parent adds
        # only its note, which cannot stand as the task or as evidence (LG-17).
        task = check_task(entry.handle, brief.check.revision)
        inputs = [evidence_line(link) for link in entry.evidence]
        delegation = Delegation(
            delegation_id=delegation_id,
            parent=self.parent,
            run=run_name(started, run_id),
            kind="check",
            brief={"task": task, "note": brief.note, "inputs": list(inputs), "check": check},
            started_at=started,
        )
        try:
            await threaded(self.records.start, project, session, delegation)
        except BaseException:
            if self.tree is not None:
                self.tree.ended(delegation_id, 0)
            raise
        child = Child(
            delegation_id,
            run_id,
            started,
            brief_text(task, inputs, brief.note),
            max_turns=min(CHECK_TURNS, self.max_turns, self.turns_left),
            tokens=tokens,
            target=Target(
                entry.entry_id,
                entry.handle,
                brief.check.revision,
                proof_reads(entry.evidence),
            ),
            tree=self.tree,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(self._run(child, stop), name=f"delegation {delegation_id}")
        self.children[delegation_id] = _Running(
            delegation, run_id, task, stop, {"turns": child.max_turns, "tokens": child.tokens}
        )
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
        ending = Ending("failed", "error")
        used: int | None = None
        try:
            end = await self.runner(child, stop)
            used = end.used
            ending = Ending(
                observed(end),
                end.stopped,
                end.final_text,
                end.refused,
                end.result,
                end.errors,
                end.alias,
                end.models,
                end.read_entry,
                end.read_proofs,
            )
        except asyncio.CancelledError:
            # Cancelled outside its loop, before it started or while it was saved.
            if (current := asyncio.current_task()) is not None:
                current.uncancel()
            ending = Ending("cancelled", "cancelled")
        except Exception:
            _log.exception("delegated run %s failed", child.run_id)
        if self.tree is not None:
            # What it did not use returns to the root.
            self.tree.ended(child.delegation_id, used)
        await self._end(child.delegation_id, ending)

    async def _end(self, delegation_id: str, ending: Ending) -> None:
        """Write an outcome, trying again on failure; one never written is ended at the
        session's next main run (``DelegationRecords.abandon``)."""
        context = self.context
        for attempt in range(WRITES):
            try:
                await threaded(
                    self.records.end, context.project_id, context.session_id, delegation_id, ending
                )
                return
            except Exception:
                _log.exception("the outcome of delegation %s could not be kept", delegation_id)
                if attempt + 1 < WRITES:
                    await self.clock.sleep(0.5 * 2**attempt)

    async def _wait(self, request: WaitRequest) -> tuple[str, dict[str, Any]]:
        ids = list(dict.fromkeys(request.delegation_ids))
        found = {i: await self._read(i) for i in ids}
        running = [i for i, d in found.items() if d.ended is None]
        stranger = [i for i in running if i not in self.children]
        if stranger:
            raise WorkerAPIError(
                ErrorKind.NOT_FOUND, f"delegation {stranger[0]!r} is not one of this run's"
            )
        pending = {self.children[i].task for i in running if not self.children[i].task.done()}
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
            found = {i: await self._read(i) for i in ids}
        shown: list[dict[str, object]] = []
        for delegation in found.values():
            used = len(dump({"reason": reason, "delegations": shown}))
            shown.append(self._shown(delegation, used))
        result = {"reason": reason, "delegations": shown}
        ended = [i for i, d in found.items() if d.ended is not None]
        return dump(result), {"wait": {"reason": reason, "ended": ended}}

    async def _status(self, delegation_id: str) -> tuple[str, dict[str, Any]]:
        shown = self._shown(await self._read(delegation_id))
        return dump(shown), {"status": {"delegation_id": delegation_id}}

    async def _cancel_one(self, delegation_id: str) -> tuple[str, dict[str, Any]]:
        """Cancel a running child (PD-39) and return its outcome; one that ended stays as
        it ended."""
        child = self.children.get(delegation_id)
        if child is None:
            raise WorkerAPIError(
                ErrorKind.NOT_FOUND, f"this run started no delegation {delegation_id!r}"
            )
        cancelled = not child.task.done()
        if cancelled:
            await self._stopped([child])
        found = await self._read(delegation_id)
        shown = {**self._shown(found), "cancelled": cancelled}
        return dump(shown), {"cancel": {"delegation_id": delegation_id, "cancelled": cancelled}}

    def _shown(self, delegation: Delegation, used: int = 0) -> dict[str, object]:
        """A delegation as a tool returns it: a running one's state, or an ended one's
        whole outcome when it fits the output limit with the ``used`` characters before
        it, which then counts as received once a model call has seen it; otherwise its
        summary."""
        if delegation.ended is None:
            child = self.children.get(delegation.delegation_id)
            limits = {"limits": child.limits} if child is not None else {}
            return {**_named(delegation), "state": "running", **limits}
        whole = outcome(delegation)
        if used and used + len(dump(whole)) > self.output_chars:
            return {**summarized(delegation), "more": "read it with delegation_status"}
        # Alone, a result always fits; a long answer is cut to its head and tail.
        answer = whole.get("answer")
        while (
            isinstance(answer, str) and len(answer) > 100 and len(dump(whole)) > self.output_chars
        ):
            keep = len(answer) * 3 // 8
            answer = f"{answer[:keep]}\n[…]\n{answer[-keep:]}"
            whole = {**whole, "answer": answer, "answer_cut": True}
        self.returned.add(delegation.delegation_id)
        return whole

    async def _read(self, delegation_id: str) -> Delegation:
        context = self.context
        try:
            return await threaded(
                self.records.get, context.project_id, context.session_id, delegation_id
            )
        except ControlError as error:
            raise WorkerAPIError(ErrorKind.NOT_FOUND, error.message) from None

    # --- The turn hook --------------------------------------------------------------

    def lease(self) -> AbstractAsyncContextManager[None]:
        return self._held()

    @asynccontextmanager
    async def _held(self) -> AsyncGenerator[None]:
        context = self.context
        abandoned = await threaded(self.records.abandon, context.project_id, context.session_id)
        for delegation_id, _ in abandoned:
            _log.warning("delegation %s lost its process: interrupted", delegation_id)
        try:
            yield
        finally:
            await self._stopped(self._running())

    async def _stopped(self, running: Sequence[_Running]) -> None:
        """Cancel children and wait for their records and outcomes; one that has not ended
        in ``STOP_SECONDS`` is recorded ``interrupted``."""
        for child in running:
            child.stop.set()
            child.task.cancel()
        if not running:
            return
        _, left = await asyncio.wait([child.task for child in running], timeout=STOP_SECONDS)
        for child in running:
            delegation_id = child.delegation.delegation_id
            if child.task in left:
                _log.error("delegated run %s did not end after its cancel", child.run_id)
                if self.tree is not None:
                    self.tree.ended(delegation_id)  # it counts as what it used so far
                await self._end(delegation_id, Ending("interrupted", "cancelled"))
            elif child.task.cancelled():  # cancelled before it started: it wrote nothing
                if self.tree is not None:
                    self.tree.ended(delegation_id, 0)
                await self._end(delegation_id, Ending("cancelled", "cancelled"))

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        found = await self._fresh(run)
        if not found:
            return ()
        return (self._handback(found, DELEGATIONS_ATTACHED.text if point == "tool" else None),)

    async def model_returned(self, run: RunView) -> None:
        self.received |= self.returned
        self.returned.clear()
        self.turns_left = run.turns_left

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        found = await self._fresh(run)
        notices = [self._handback(found)] if found else []
        running = self._running()
        if running:
            waiting = [{**_named(child.delegation), "state": "running"} for child in running]
            notices.append(Notice(self.name, (), DELEGATIONS_RUNNING.text, waiting))
            # With no turn or room left, the run stops for its own reason; its end
            # cancels them.
            limit = "turn_limit" if run.turns_left <= 0 else "context_limit"
            return Closing("retry", tuple(notices), stopped=run.halted or limit)
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
        """The session's unacknowledged outcomes this run's conversation does not hold,
        as many as fit in ``max_chars`` as summaries."""
        context = self.context
        found = await threaded(self.records.handbacks, context.project_id, context.session_id)
        held = set(run.offered.get(self.name, ())) | self.returned | self.received
        fresh: list[Delegation] = []
        for delegation in found:
            if delegation.delegation_id in held:
                continue
            if fresh and len(dump([summarized(d) for d in [*fresh, delegation]])) > self.max_chars:
                break
            fresh.append(delegation)
        return fresh

    def _handback(self, found: Sequence[Delegation], key: str | None = None) -> Notice:
        ids = tuple(d.delegation_id for d in found)
        return Notice(self.name, ids, key or DELEGATIONS.text, [summarized(d) for d in found])


async def _ended(tasks: set[asyncio.Task[None]], when: str) -> None:
    await asyncio.wait(tasks, return_when=when)


def _named(delegation: Delegation) -> dict[str, object]:
    check = cast(dict[str, object], delegation.brief.get("check") or {})
    return {
        "delegation_id": delegation.delegation_id,
        "run": delegation.run,
        "check": {"entry": check.get("handle"), "revision": check.get("revision")},
    }


def _ending(delegation: Delegation) -> dict[str, object]:
    found = {
        **_named(delegation),
        "ended": delegation.ended,
        "stopped": delegation.stopped,
        "alias": delegation.alias,
        "models": list(delegation.models),
        "read_entry": delegation.read_entry,
        "read_proofs": delegation.read_proofs,
    }
    if delegation.refused is not None:
        found["refused"] = delegation.refused
    return found


def outcome(delegation: Delegation) -> dict[str, object]:
    """A delegation's whole outcome as its parent receives it (PD-20): how it ended,
    whether it read the entry it checked, and its result, or, when that is missing, its
    answer as returned and why it did not validate."""
    if delegation.result is not None:
        return {**_ending(delegation), "result": delegation.result}
    return {
        **_ending(delegation),
        "result": None,
        "errors": list(delegation.errors),
        "answer": delegation.report,
    }


def summarized(delegation: Delegation) -> dict[str, object]:
    """A delegation's outcome as a notice gives it: its verdict and status, or without a
    result the head of its answer."""
    found = _ending(delegation)
    if delegation.result is not None:
        result = delegation.result
        return {**found, "verdict": result.get("verdict"), "status": result.get("status")}
    answer = delegation.report or ""
    head = answer if len(answer) <= SUMMARY_CHARS else answer[:SUMMARY_CHARS] + "…"
    return {**found, "result": None, "answer_head": head, "answer_chars": len(answer)}
