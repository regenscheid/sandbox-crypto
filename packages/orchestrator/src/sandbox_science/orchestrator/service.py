# SPDX-License-Identifier: MIT
"""Durable scheduling and bookkeeping for GUI chat and explicit tree recovery.

The normal launcher delegates agent-loop execution to its separate OpenShell
runtime. This service renews authority, reconciles receipts and expired leases,
validates runtime-selected admissions, and owns cancellation/final cleanup.
Research code executes through scoped worker capabilities.

Fresh chat and unoffered annotations can start new work within current budgets.
Original offered input stays attached to unfinished work: neither restart nor a
retry delay permits fresh paid replay. Explicit Resume reconciles the selected
work tree and reuses its original grants and effect identities.

A research_state context stop retains a consolidation hold. Fresh input can ask
for help consolidating decisions; a confirmed decision change clears that
prerequisite, while unfinished work still requires explicit Resume. A replacement
service first observes the current decision set without treating restart as change.

Figure checks run independently and never hold up a research run.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from pydantic import JsonValue

from sandbox_science.contracts import SYSTEM_CLOCK, Clock

from .aio import owned, threaded
from .chat import SOURCE, session_lease
from .continuity.records import WorkRecords
from .continuity.recovery import Recovery
from .control import ControlError
from .control.errors import BUSY
from .delegation.records import DelegationRecords
from .documents.annotations import Annotations
from .documents.tool import REQUESTS_TASK, WorkRequests
from .figurecheck import FigureChecker
from .figurechecks import FigureChecks
from .harness import Harness, RunRequest, admit, save
from .history import Archive, Folder
from .history.markers import Decided, current_decisions
from .history.receipts import Receipts
from .hooks import TurnHook
from .ledger import Ledger
from .model import GATEWAY_BUDGET
from .projects import ProjectError, run_name
from .questions import INTERRUPTED, Questions
from .runstate import RunState, head_and_tail
from .tools import ToolOutcome

_log = logging.getLogger(__name__)

MAX_ACTIVE_ROOTS: Final = 4
POLL_SECONDS: Final = 1.0
"""The percent of the project's daily MAIN budget one run may use, by default (RS-6a)."""
BEAT_SECONDS: Final = 10.0
RETRY_SECONDS: Final = 30.0
MAX_WAIT: Final = 900.0
"""The longest wait between a session's failing runs."""
MAX_FAILURES: Final = 5
"""Failed runs in a row after which a session's messages are held."""
RESEARCH_STATE: Final = "research_state"
"""Why a session's messages are held when its decisions do not fit its view (CM-45)."""


class LogSink:
    """A run's progress, to the service's log; the GUI reads the run's events."""

    def note(self, text: str) -> None:
        _log.info("%s", text)

    def error(self, text: str) -> None:
        _log.warning("%s", text)

    async def tool(self, name: str, arguments: str, outcome: ToolOutcome) -> None:
        return None


@dataclass
class _Active:
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    run: str | None = None
    """The run's name, once it is admitted."""
    phase: Literal["starting", "running", "saving"] = "starting"
    """Only a ``running`` run is cancelled: never its saving and acknowledgements."""
    message_ids: list[str] = field(default_factory=list[str])
    request_ids: list[str] = field(default_factory=list[str])
    """The work requests that waited when the run began."""
    recovery: Recovery | None = None
    consolidate: bool = False
    """A consolidation run, for a message written after a ``research_state`` stop."""


class RunService:
    """Runs a harness's sessions for the GUI. The harness needs its stores."""

    def __init__(
        self,
        harness: Harness,
        *,
        poll: float = POLL_SECONDS,
        retry: float = RETRY_SECONDS,
        clock: Clock = SYSTEM_CLOCK,
        figures: FigureChecker | None = None,
    ) -> None:
        if harness.stores is None:
            raise ValueError("the run service needs the state directory's stores")
        self.harness, self.stores = harness, harness.stores
        self.poll, self.retry, self.clock = poll, retry, clock
        self.runs: RunState = harness.stores.runs
        self.active: dict[tuple[str, str], _Active] = {}
        self.waiting_until: dict[tuple[str, str], float] = {}
        self.failures: dict[tuple[str, str], int] = {}
        self.annotations = Annotations(harness.stores.documents)
        self.delegations = DelegationRecords(harness.stores.documents, clock=clock)
        self.questions = Questions(harness.stores.documents, clock=clock)
        self.held: dict[tuple[str, str], set[str]] = {}
        """Work requests that start no run, as held messages do not (RS-9, RS-11, RS-12):
        the session's next run is offered them with the others."""
        self.holder = secrets.token_hex(16)
        self._beaten = -BEAT_SECONDS
        self.ledger = Ledger(harness.stores.documents)
        self.figures = FigureChecks(harness.stores.documents, figures)
        self.local_branches: set[asyncio.Task[bool]] = set()
        self.checking: asyncio.Task[int] | None = None
        """The figure checks running, which the next look does not start again."""
        self.marks: dict[tuple[str, str], tuple[tuple[int, int], tuple[Decided, ...]]] = {}
        """When each session's messages were held for its research state: the ledger's
        mark, and the session's current decisions, whose change queues them again."""

    async def serve(self) -> None:
        """Run until cancelled. The runs in progress then end ``interrupted``."""
        # Texts saved before figure checks were built are checked once now.
        await threaded(self.figures.backfill)
        try:
            while True:
                await self.tick()
                await self.clock.sleep(self.poll)
        finally:
            await self.close()

    async def close(self) -> None:
        """Join admitted work and reconcile inputs before another runtime may select it."""
        await owned(self._close())

    async def _close(self) -> None:
        tasks: list[asyncio.Task[object]] = [*self.local_branches]
        if self.checking is not None:
            tasks.append(self.checking)
        active = tuple(self.active.items())
        tasks.extend(value.task for _, value in active if value.task is not None)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for (project, session), value in active:
            if value.run is not None:
                await self._ended(project, session, value.run)
        self.active.clear()
        self.local_branches.clear()

    async def tick(self) -> None:
        """One local-service cycle; confined runtimes select work through capabilities."""
        await self.maintenance_tick()
        for project, session, request in await self.waiting_resumes():
            await self.start_selected(project, session, request=request)
            for work in await self.resume_next(project, session, request):
                task = asyncio.create_task(self.resume_branch(project, session, request, work))
                self.local_branches.add(task)
                task.add_done_callback(self.local_branches.discard)
        for project, session in await self.waiting_work():
            await self.start_selected(project, session)

    async def maintenance_tick(self) -> None:
        """Renew/reconcile existing authority without selecting or admitting new roots."""
        now = self.clock.monotonic()
        if now - self._beaten >= BEAT_SECONDS:
            await threaded(self.runs.beat, self.holder)
            self._beaten = now
        if self.checking is None or self.checking.done():
            ended = self.checking
            if ended is not None and not ended.cancelled() and ended.exception() is not None:
                _log.warning("figure checks failed: %r", ended.exception())
            self.checking = asyncio.create_task(self.figures.drain())
        for dead in await threaded(self.runs.dead):
            mine = self.active.get((dead.project, dead.session))
            if mine is not None and mine.run == dead.run:
                continue  # alive: its renewals lag, as after a suspend
            if await threaded(self.runs.reap, dead):
                _log.warning("run %s of session %s was interrupted", dead.run, dead.session)
                await self._ended(dead.project, dead.session, dead.run)
        for _, session, question in await threaded(self.questions.close_orphans):
            _log.warning("question %s of session %s had no run: closed", question, session)
        for key, active in list(self.active.items()):
            if active.task is not None and active.task.done():
                del self.active[key]
            elif (
                active.phase == "running"
                and not active.stop.is_set()
                and active.run is not None
                and await threaded(self.runs.cancelled, *key, active.run)
                and active.phase == "running"  # still, after the read
            ):
                active.stop.set()
                if active.task is not None:
                    active.task.cancel()
        for key in await threaded(self.runs.held_for, RESEARCH_STATE):
            if key not in self.active:
                await self._consolidated(*key)

    async def waiting_work(self) -> tuple[tuple[str, str], ...]:
        """Enumerate eligible work without admitting or starting an agent loop."""
        waiting = set(await threaded(self.runs.waiting))
        for project, session, request in await threaded(self.annotations.waiting):
            if request not in self.held.get((project, session), ()):
                waiting.add((project, session))
        for project, session in waiting:
            await threaded(self.runs.reconcile_inputs, project, session)
            await threaded(self.annotations.reconcile_receipts, project, session)
        eligible = set(await threaded(self.runs.waiting))
        eligible.update(
            (p, s)
            for p, s, request in await threaded(self.annotations.waiting)
            if request not in self.held.get((p, s), ())
        )
        now = self.clock.monotonic()
        return tuple(
            key
            for key in sorted(waiting & eligible)
            if key not in self.active and self.waiting_until.get(key, now) <= now
        )

    async def waiting_resumes(self) -> tuple[tuple[str, str, str], ...]:
        queued = await threaded(WorkRecords(self.stores.projects.control).queued)
        waiting = {(p, s, q) for p, s, q, _ in queued if (p, s) not in self.active}
        waiting.update(
            (p, s, active.recovery.request)
            for (p, s), active in self.active.items()
            if active.recovery is not None
        )
        return tuple(sorted(waiting))

    async def resume_next(self, project: str, session: str, request: str) -> tuple[str, ...]:
        active = self.active.get((project, session))
        recovery = active.recovery if active else None
        return () if recovery is None or recovery.request != request else await recovery.next()

    async def resume_branch(self, project: str, session: str, request: str, work: str) -> bool:
        active = self.active.get((project, session))
        recovery = active.recovery if active else None
        if recovery is None or recovery.request != request:
            return False
        return await recovery.branch(work)

    async def start_selected(
        self, project: str, session: str, *, request: str | None = None
    ) -> bool:
        """Validate a runtime-selected queue entry and own its trusted bookkeeping.

        Selection grants nothing: recheck queue eligibility and the session lease.
        The configured harness loop runner owns actual agent-loop execution.
        """
        key = project, session
        await threaded(self.runs.reconcile_inputs, project, session)
        await threaded(self.annotations.reconcile_receipts, project, session)
        if request is not None:
            if (project, session, request) not in await self.waiting_resumes():
                return False
            if await threaded(self.runs.leased, *key):
                return False
            if key in self.active or len(self.active) >= MAX_ACTIVE_ROOTS:
                return False
            recovery = Recovery(self.harness, project, session, request)
            active = self.active[key] = _Active(
                recovery=recovery, run=recovery.run_name, phase="running"
            )
            active.task = asyncio.create_task(self._run(*key, active))
            return True
        if any((p, s) == key for p, s, _ in await self.waiting_resumes()):
            return False
        if key not in await self.waiting_work():
            return False
        consolidate = False
        if key in self.marks:
            queued = await threaded(self.runs.messages, *key, state="queued")
            held = [m.message_id for m in queued if m.reason == RESEARCH_STATE]
            new = [m for m in queued if m.reason != RESEARCH_STATE]
            if held and not new:
                self.marks.pop(key)
            else:
                await threaded(self.runs.hold, *key, held, RESEARCH_STATE)
                self._hold(*key, await self._requests(*key))
                if not new:
                    return False
                consolidate = True
        if await threaded(self.runs.leased, *key):
            return False
        # No await separates this final active check from assignment. Concurrent
        # runtime admissions cannot create duplicate bookkeeping tasks here.
        if key in self.active or len(self.active) >= MAX_ACTIVE_ROOTS:
            return False
        active = self.active[key] = _Active(consolidate=consolidate)
        active.task = asyncio.create_task(self._run(*key, active))
        return True

    async def _ended(self, project: str, session: str, run: str, *, hold: bool = True) -> None:
        """End a run whose holder is gone (RS-12): from its record if it was saved,
        otherwise as ``interrupted``, with the messages it was offered held, or, for a
        run that stopped itself (``hold`` false), left queued for the next run (RS-9).
        Its tree ends with it, and its open questions close, whatever fails before
        (PD-40, PD-23)."""
        works = WorkRecords(self.stores.projects.control)
        work = await threaded(works.for_run, project, session, run)
        cancelled = await threaded(self.runs.cancelled, project, session, run)
        if work is not None:
            await threaded(works.interrupt_run, project, session, run, stopped=cancelled)
        try:
            await self._root_ended(project, session, run, hold=hold)
        finally:
            try:
                for delegation_id, _ in await threaded(
                    self.delegations.abandon, project, session, parent=run
                ):
                    _log.warning("delegation %s of run %s was interrupted", delegation_id, run)
            finally:
                if cancelled or work is None or work.state in ("completed", "stopped"):
                    for question in await threaded(
                        self.questions.close,
                        project,
                        session,
                        run=run,
                        state="interrupted",
                        reason=INTERRUPTED,
                    ):
                        _log.warning("question %s of run %s was interrupted", question, run)

    async def _root_ended(self, project: str, session: str, run: str, *, hold: bool) -> None:
        runs = self.runs
        try:
            record = await threaded(self.stores.projects.run, project, session, run)
        except ProjectError:
            record = None
        # An assistant receipt is durable before final save. Recover it even when
        # the process died with no RunRecord, so consumed user messages stay consumed.
        receipts = Receipts(self.stores.projects.control, project, session, run)
        after = ""
        while page := await threaded(receipts.page, SOURCE, after=after):
            await threaded(runs.deliver, project, session, list(page), run)
            after = page[-1]
        after = ""
        while page := await threaded(receipts.page, WorkRequests.name, after=after):
            await threaded(
                self.annotations.acknowledge, project, session, run.partition("-")[2], list(page)
            )
            after = page[-1]
        await threaded(self.annotations.reconcile_receipts, project, session)
        stopped: dict[str, JsonValue]
        if record is not None:
            # It died after saving its record, before acknowledging it.
            if record.stored_history is None:
                await threaded(
                    runs.deliver, project, session, record.delivered.get(SOURCE, []), run
                )
            stopped = {
                "stopped": record.stopped,
                "failure": record.failure,
                "final_text": head_and_tail(record.final_text),
                "record": run,
            }
        else:
            stopped = {
                "stopped": (
                    "cancelled"
                    if await threaded(runs.cancelled, project, session, run)
                    else "interrupted"
                ),
                "failure": None,
                "final_text": None,
                "record": None,
            }
        if hold:
            offered = await threaded(runs.offered_to, project, session, run)
            await threaded(runs.hold, project, session, offered)
            # A run's name is its start time, a dash and its ID, which leased them.
            run_id = run.partition("-")[2]
            claimed = await threaded(self.annotations.claimed, project, session, run_id)
            self._hold(project, session, claimed)
        if not await threaded(runs.ended, project, session, run):
            await threaded(runs.append, project, session, run, "stopped", stopped)

    async def _run(self, project: str, session: str, active: _Active) -> None:
        key = project, session
        try:
            failed = (
                await active.recovery.run(LogSink(), active.stop)
                if active.recovery is not None
                else await self._started(project, session, active)
            )
        except asyncio.CancelledError:
            if not active.stop.is_set():
                raise
            # Cancelled outside its loop: what it was offered waits for the user too.
            await threaded(self.runs.hold, project, session, active.message_ids)
            self._hold(project, session, active.request_ids)
            if active.run is not None:
                await self._ended(project, session, active.run)
            return
        except ControlError as error:
            if error.kind == BUSY:
                # Another run, or another service, took the session first.
                _log.info("session %s is busy: %s", session, error.message)
                self.waiting_until[key] = self.clock.monotonic() + self.retry
                return
            _log.warning("a run of session %s stopped: %s", session, error.message)
            failed = True
        except Exception:
            _log.exception("a run of session %s could not run", session)
            failed = True
        if failed and active.run is not None:
            # A run that stopped without its record (a lost lease) still ends.
            await self._ended(project, session, active.run, hold=False)
        if not failed:
            self.failures.pop(key, None)
            return
        count = self.failures[key] = self.failures.get(key, 0) + 1
        if count >= MAX_FAILURES:
            _log.error("%d runs of session %s failed in a row: its messages wait", count, session)
            queued = await threaded(self.runs.messages, project, session, state="queued")
            ids = [message.message_id for message in queued]
            await threaded(self.runs.hold, project, session, ids, f"{count} runs failed in a row")
            self._hold(project, session, await self._requests(project, session))
            self.failures.pop(key)
            return
        wait = min(self.retry * 2 ** (count - 1), MAX_WAIT)
        self.waiting_until[key] = self.clock.monotonic() + wait

    async def _consolidated(self, project: str, session: str) -> None:
        """Observe the consolidation prerequisite, retaining holds across restart.

        A changed decision set clears that prerequisite, but durable work still
        requires explicit Resume. Read decisions only when the ledger mark moved.
        """
        key = project, session
        mark = await threaded(self.ledger.mark, project)
        seen = self.marks.get(key)
        if seen is not None and seen[0] == mark:
            return
        decided = await threaded(self._decided, project, session)
        if seen is None:
            self.marks[key] = (mark, decided)
            return  # Restart is not evidence that the prerequisite changed.
        if seen[1] == decided:
            self.marks[key] = (mark, decided)  # another change of the ledger
            return
        self.marks.pop(key, None)
        self.held.pop(key, None)
        # The changed prerequisite no longer calls for consolidation. Original
        # durable work still needs explicit Resume and is re-held by reconciliation.
        count = await threaded(
            self.runs.resume, project, session, reason=RESEARCH_STATE, clear_reason=True
        )
        _log.info("session %s: %d messages queued again for its research state", session, count)

    def _decided(self, project: str, session: str) -> tuple[Decided, ...]:
        """The session's current decisions, as its fold records name them and the ledger
        holds them now."""
        return current_decisions(
            Archive(self.stores.projects.control), self.ledger, project, session
        )

    def _hold(self, project: str, session: str, requests: Iterable[str]) -> None:
        self.held.setdefault((project, session), set()).update(requests)

    async def _requests(self, project: str, session: str) -> list[str]:
        """The session's waiting work requests that may start a run."""
        found = await threaded(self.annotations.waiting, (project, session))
        held = self.held.get((project, session), set())
        return [request for _, _, request in found if request not in held]

    async def _started(self, project: str, session: str, active: _Active) -> bool:
        """Run the session's queued messages, or else its waiting work requests; whether
        the run failed."""
        runs, stores = self.runs, self.stores
        sink = LogSink()
        # The session runs where it was opened: in the project's environment, or its own.
        known = (await threaded(stores.projects.get, project)).sessions.get(session)
        dedicated = known is not None and known.dedicated
        if (
            known is not None
            and known.closed_at is not None
            and not await threaded(runs.messages, project, session, state="queued")
        ):
            # A comment does not reopen a closed session: it waits for the session's next run.
            self._hold(project, session, await self._requests(project, session))
            return False
        admitted = await admit(
            self.harness.client,
            stores.projects,
            RunRequest(project, session, "", dedicated=dedicated),
            sink,
            min_isolation_tier=self.harness.settings.min_isolation_tier,
        )
        active.run = name = run_name(admitted.started_at, admitted.run_id)
        async with session_lease(runs, admitted.context, name, clock=self.clock):
            # Read once the session is held: another run may have answered them.
            queued = await threaded(runs.messages, project, session, state="queued")
            # A consolidation run is for the new messages alone: the requests wait.
            requests = [] if active.consolidate else await self._requests(project, session)
            if not queued and not requests:
                return False
            ids = active.message_ids = [message.message_id for message in queued]
            active.request_ids = requests
            if await threaded(runs.over_cost_limit, project, self.clock.now()):
                _log.warning("project %s has spent its model budget", project)
                if queued:
                    await threaded(runs.refuse, project, session, ids, "budget")
                else:
                    # A request cannot be refused: it waits for the window to move on.
                    self.waiting_until[project, session] = self.clock.monotonic() + MAX_WAIT
                return False
            # The run is offered every request that waits, held or not.
            self.held.pop((project, session), None)
            request = dataclasses.replace(
                admitted.request,
                task="\n\n".join(message.text for message in queued) if queued else REQUESTS_TASK,
                message_ids=tuple(ids),
                requests=not queued,
                consolidate=active.consolidate,
            )
            active.phase = "running"
            finished = await self.harness.start(
                dataclasses.replace(admitted, request=request), sink, stop=active.stop
            )
            active.phase = "saving"
            status = await save(
                finished.fields(),
                finished.status,
                finished.hooks,
                projects=stores.projects,
                error=sink.error,
            )
            if finished.transcript.stopped == GATEWAY_BUDGET:
                # Every call fails the same way until the gateway's budget is raised or
                # resets: what waits for the session waits that long (RS-6d).
                _log.warning("the model gateway's budget is spent; session %s waits", session)
                self.waiting_until[project, session] = self.clock.monotonic() + MAX_WAIT
            if finished.transcript.stopped == "context_limit" and any(
                isinstance(hook, Folder) and hook.stopped == RESEARCH_STATE
                for hook in finished.hooks
            ):
                # The next run would stop the same way: what waits, waits for a change of
                # the ledger or the user (CM-45).
                mark = await threaded(self.ledger.mark, project)
                self.marks[project, session] = (
                    mark,
                    await threaded(self._decided, project, session),
                )
                # UserMessages already held queued input before the stopped event.
                # Do not capture fresh chat arriving after that acknowledged stop.
                self._hold(project, session, [*active.request_ids, *unanswered(finished.hooks)])
            if finished.transcript.stopped == "cancelled":
                # What it was offered and did not answer waits for the user (RS-11).
                offered = await threaded(runs.offered_to, project, session, name)
                await threaded(runs.hold, project, session, offered)
                self._hold(project, session, [*active.request_ids, *unanswered(finished.hooks)])
        return finished.transcript.failure is not None or status == 1


def unanswered(hooks: Sequence[TurnHook]) -> set[str]:
    """The work requests a run was offered and no model call of it saw."""
    found: set[str] = set()
    for hook in hooks:
        if isinstance(hook, WorkRequests):
            found |= hook.unanswered()
    return found
