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
from typing import Any, Final, Literal, Protocol, cast

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
from sandbox_science.orchestrator.session_plans import PlanRef
from sandbox_science.orchestrator.worker_client import WorkerAPIError, dump

from .models import Choice, ModelChoice
from .outcome import Target, proof_reads
from .prompt import brief_text, check_task, evidence_line, task_text
from .records import Delegation, DelegationRecords, DelegationSettings, Ended, Ending
from .specs import CheckBrief, Named, TaskBrief, WaitRequest, brief_of, offered
from .work import work_record

_log = logging.getLogger(__name__)

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
    target: Target | None
    """For a check, the entry and revision it checks, which its result must name;
    ``None`` for a task."""
    alias: str | None = None
    """The model alias it calls (PD-28, PD-30); ``None`` for the delegated role's."""
    track: tuple[PlanRef, str] | None = None
    """For a task on a track, the plan version it works under and the track (PD-9)."""


@dataclass(frozen=True)
class _Planned:
    """A delegation as its brief gives it, before it starts."""

    kind: Literal["check", "task"]
    brief: dict[str, JsonValue]
    """The brief as the delegation keeps it."""
    text: str
    """The child's first message."""
    turns: int
    tokens: int
    target: Target | None
    """For a check, its entry and revision; ``None`` for a task."""
    shown: dict[str, JsonValue]
    """What ``delegate``'s answer says of it."""
    track: tuple[PlanRef, str] | None = None


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
    files: dict[str, JsonValue] | None = None
    """The files its calls recorded, as its record keeps them (RA-36)."""
    created: tuple[str, ...] = ()
    """The ledger entries its changes created, by ID (LG-6's ``produced``)."""


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
        limits: DelegationSettings | None = None,
        models: ModelChoice | None = None,
        track_grant: Callable[[str], Awaitable[PlanRef]] | None = None,
        max_chars: int = 5000,
        output_chars: int = 20000,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """``offer`` says whether the run gets the tools; ``max_turns`` bounds a child's
        turns; ``wake`` returns once the user writes to the session, which ends a wait;
        ``over_budget`` says whether the project reached its daily cost limit, if it has
        one. Nothing is reserved for a child: it is bounded by its own tokens and turns
        (PD-38). Notices
        together take at most ``max_chars``, and a wait's result at most ``output_chars``."""
        self.records, self.ledger, self.context = records, ledger, context
        self.parent, self.runner, self.max_turns = parent, runner, max_turns
        self.wake, self.over_budget, self.clock = wake, over_budget, clock
        self.limits = limits or DelegationSettings()
        """The project's cap and a check's tokens and turns (PD-38)."""
        self.models = models
        """The model each child calls (PD-28, PD-30); without it, the harness's."""
        self.track_grant = track_grant
        """The plan version a task on a track works under (PD-9); without it, a task
        names no track."""
        choices = self.limits.models if models is not None else ()
        self.specs: tuple[ToolSpec, ...] = offered(choices) if offer else ()
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
                return await self._delegate(brief_of(args))
            case "wait_delegations":
                return await self._wait(WaitRequest.model_validate(args))
            case "delegation_status":
                return await self._status(Named.model_validate(args).delegation_id)
            case _:
                return await self._cancel_one(Named.model_validate(args).delegation_id)

    async def _delegate(self, brief: CheckBrief | TaskBrief) -> tuple[str, dict[str, Any]]:
        cap = self.limits.cap
        if len(self._running()) >= cap:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE,
                f"limit_reached: {cap} delegated runs are running in this session; "
                "wait for one to end",
            )
        if self.turns_left <= 0:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE,
                "limit_reached: this run has no turn left to wait for a delegated run",
            )
        choice = await self._model(brief.model)
        if self.over_budget is not None and await self.over_budget():
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE, "limit_reached: the project reached its daily cost limit"
            )
        project, session = self.context.project_id, self.context.session_id
        planned = await (self._check(brief) if isinstance(brief, CheckBrief) else self._task(brief))
        delegation_id, run_id = new_id("dlg"), new_id("run")
        started = self.clock.now().astimezone(UTC)
        limits: dict[str, JsonValue] = {"turns": planned.turns, "tokens": planned.tokens}
        written: dict[str, JsonValue] = {**planned.brief, "limits": limits}  # for the tree
        if choice is not None:
            written["model"] = choice.recorded()
        delegation = Delegation(
            delegation_id=delegation_id,
            parent=self.parent,
            run=run_name(started, run_id),
            kind=planned.kind,
            brief=written,
            started_at=started,
        )
        await threaded(self.records.start, project, session, delegation)
        child = Child(
            delegation_id,
            run_id,
            started,
            planned.text,
            max_turns=planned.turns,
            tokens=planned.tokens,
            target=planned.target,
            alias=None if choice is None else choice.alias,
            track=planned.track,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(self._run(child, stop), name=f"delegation {delegation_id}")
        self.children[delegation_id] = _Running(
            delegation, run_id, task, stop, {"turns": child.max_turns, "tokens": child.tokens}
        )
        started_with: dict[str, JsonValue] = {
            "delegation_id": delegation_id,
            "run": delegation.run,
            "state": "running",
            **planned.shown,
            "limits": {"turns": child.max_turns, "tokens": child.tokens},
        }
        if choice is not None:
            started_with["model"] = choice.recorded()
        return dump(started_with), {"delegation": started_with}

    async def _check(self, brief: CheckBrief) -> _Planned:
        """A check, which the platform writes: its task, and the entry's evidence as its
        inputs; the parent adds only its note, which cannot stand as the task or as
        evidence (LG-17)."""
        project = self.context.project_id
        entry = await threaded(self.ledger.entry, project, brief.check.entry, brief.check.revision)
        check: dict[str, JsonValue] = {
            "entry": entry.entry_id,
            "handle": entry.handle,
            "revision": brief.check.revision,
        }
        task = check_task(entry.handle, brief.check.revision)
        inputs = [evidence_line(link) for link in entry.evidence]
        return _Planned(
            "check",
            {"task": task, "note": brief.note, "inputs": list(inputs), "check": check},
            brief_text(task, inputs, brief.note),
            min(self.limits.check_turns, self.max_turns, self.turns_left),
            self.limits.check_tokens,
            Target(entry.entry_id, entry.handle, brief.check.revision, proof_reads(entry.evidence)),
            {"check": {"entry": entry.handle, "revision": brief.check.revision}},
        )

    async def _task(self, brief: TaskBrief) -> _Planned:
        """A task, which the parent writes (PD-11), under a task's limits: on a track of
        the session's active plan, which no other running task has (PD-9, PD-12), and on
        an issue, which its outcome is recorded on (LG-F8)."""
        head = brief.task if len(brief.task) <= SUMMARY_CHARS else brief.task[:SUMMARY_CHARS] + "…"
        written: dict[str, JsonValue] = {
            "task": brief.task,
            "context": brief.context,
            "inputs": list(brief.inputs),
        }
        shown: dict[str, JsonValue] = {"task": head}
        track = None
        if brief.track is not None:
            if self.track_grant is None:
                raise WorkerAPIError(ErrorKind.UNAVAILABLE, "this run's tasks take no track")
            busy = [
                r.delegation.delegation_id
                for r in self._running()
                if _track(r.delegation) == brief.track
            ]
            if busy:
                raise WorkerAPIError(
                    ErrorKind.CONFLICT,
                    f"track_busy: delegation {busy[0]} works on track {brief.track!r}; a track "
                    "has one running task at most",
                )
            granted = await self.track_grant(brief.track)
            track = (granted, brief.track)
            written["track"] = {"track_id": brief.track, "plan": granted.model_dump(mode="json")}
            shown["track"] = brief.track
        issue = None
        if brief.issue is not None:
            found = await threaded(self.ledger.entry, self.context.project_id, brief.issue)
            if found.type != "issue":
                raise WorkerAPIError(
                    ErrorKind.INVALID_REQUEST, f"{found.handle} is a {found.type}, not an issue"
                )
            issue = found.handle
            written["issue"] = {"entry": found.entry_id, "handle": found.handle}
            shown["issue"] = found.handle
        return _Planned(
            "task",
            written,
            task_text(brief.task, brief.context, brief.inputs, track=brief.track, issue=issue),
            min(self.limits.task_turns, self.max_turns, self.turns_left),
            self.limits.task_tokens,
            None,
            shown,
            track,
        )

    async def _model(self, asked: str | None) -> Choice | None:
        """The model the child calls (PD-28, PD-30); ``None`` for the harness's, when it
        offers no choice."""
        if self.models is not None:
            return await self.models.choose(asked)
        if asked is not None:
            raise WorkerAPIError(
                ErrorKind.POLICY_DENIED, "model_not_allowed: this run offers no choice of model"
            )
        return None

    async def _run(self, child: Child, stop: asyncio.Event) -> None:
        """Run a child to its end, then write its outcome, however it ended."""
        ending = Ending("failed", "error")
        created: tuple[str, ...] = ()
        try:
            end = await self.runner(child, stop)
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
                end.files,
            )
            created = end.created
        except asyncio.CancelledError:
            # Cancelled outside its loop, before it started or while it was saved.
            if (current := asyncio.current_task()) is not None:
                current.uncancel()
            ending = Ending("cancelled", "cancelled")
        except Exception:
            _log.exception("delegated run %s failed", child.run_id)
        await self._end(child.delegation_id, ending, created)

    async def _end(self, delegation_id: str, ending: Ending, created: tuple[str, ...] = ()) -> None:
        """Write an outcome, trying again on failure, then record it as work on the entry
        it checked (LG-F8); one never written is ended at the session's next main run
        (``DelegationRecords.abandon``)."""
        context = self.context
        for attempt in range(WRITES):
            try:
                kept = await threaded(
                    self.records.end, context.project_id, context.session_id, delegation_id, ending
                )
            except Exception:
                _log.exception("the outcome of delegation %s could not be kept", delegation_id)
                if attempt + 1 < WRITES:
                    await self.clock.sleep(0.5 * 2**attempt)
                continue
            await self._worked(kept, created)
            return

    async def _worked(self, delegation: Delegation, created: tuple[str, ...] = ()) -> None:
        """Record an ended delegation's outcome as work on its entry; a record that
        cannot be written leaves the outcome as it is."""
        found = work_record(delegation, self.context.session_id, created)
        if found is None:
            return
        mutation, author = found
        provenance = {"delegation": delegation.delegation_id}
        try:
            await threaded(
                self.ledger.change, self.context.project_id, mutation, author,
                provenance=provenance,
            )  # fmt: skip
        except Exception:
            _log.exception(
                "the work of delegation %s could not be recorded", delegation.delegation_id
            )

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
        # Alone, a result always fits: the list of files is cut first, then a long answer
        # to its head and tail.
        files = whole.get("files")
        while isinstance(files, dict) and len(dump(whole)) > self.output_chars:
            listed = cast(dict[str, object], files).get("files")
            if not isinstance(listed, list) or not listed:
                break
            kept = cast(list[object], listed)[: len(cast(list[object], listed)) // 2]
            files = {**cast(dict[str, object], files), "files": kept, "cut": True}
            whole = {**whole, "files": files}
        results = whole.get("result_files")
        while isinstance(results, list) and results and len(dump(whole)) > self.output_chars:
            kept = cast(list[object], results)[: len(cast(list[object], results)) // 2]
            whole = {**whole, "result_files": kept, "result_files_cut": True}
            results = kept
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
                await self._end(delegation_id, Ending("interrupted", "cancelled"))
            elif child.task.cancelled():  # cancelled before it started: it wrote nothing
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


def _track(delegation: Delegation) -> str | None:
    found = delegation.brief.get("track")
    track = cast(dict[str, object], found).get("track_id") if isinstance(found, dict) else None
    return track if isinstance(track, str) else None


def _named(delegation: Delegation) -> dict[str, object]:
    found: dict[str, object] = {"delegation_id": delegation.delegation_id, "run": delegation.run}
    if delegation.kind == "task":
        task = str(delegation.brief.get("task") or "")
        found["task"] = task if len(task) <= SUMMARY_CHARS else task[:SUMMARY_CHARS] + "…"
        return found
    check = cast(dict[str, object], delegation.brief.get("check") or {})
    return {**found, "check": {"entry": check.get("handle"), "revision": check.get("revision")}}


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
    answer as returned and why it did not validate; then the files its calls recorded,
    the platform's list apart from the child's own claims (RA-36)."""
    files: dict[str, object] = {}
    if delegation.files is not None:
        recorded = dict(delegation.files)
        results = recorded.pop("results", None)
        if results:
            files["result_files"] = results  # the child's results, pinned (RA-38)
        if recorded.get("total") or recorded.get("gaps"):
            files["files"] = recorded
    if delegation.result is not None:
        return {**_ending(delegation), "result": delegation.result, **files}
    return {
        **_ending(delegation),
        "result": None,
        "errors": list(delegation.errors),
        "answer": delegation.report,
        **files,
    }


def summarized(delegation: Delegation) -> dict[str, object]:
    """A delegation's outcome as a notice gives it: a check's verdict and status, a
    task's status and the head of its summary, or without a result the head of its
    answer."""
    found: dict[str, object] = _ending(delegation)
    if delegation.files is not None:
        found["files_recorded"] = delegation.files.get("total")
        results = delegation.files.get("results")
        if isinstance(results, list) and results:
            found["result_files"] = [
                cast(dict[str, object], r).get("path") for r in results[:10] if isinstance(r, dict)
            ]
    if delegation.result is not None:
        result = delegation.result
        if delegation.kind == "task":
            summary = str(result.get("summary") or "")
            head = summary if len(summary) <= SUMMARY_CHARS else summary[:SUMMARY_CHARS] + "…"
            return {**found, "status": result.get("status"), "summary_head": head}
        return {**found, "verdict": result.get("verdict"), "status": result.get("status")}
    answer = delegation.report or ""
    head = answer if len(answer) <= SUMMARY_CHARS else answer[:SUMMARY_CHARS] + "…"
    return {**found, "result": None, "answer_head": head, "answer_chars": len(answer)}
