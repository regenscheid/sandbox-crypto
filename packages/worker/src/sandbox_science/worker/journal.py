# SPDX-License-Identifier: MIT
"""The execution journal, for the execution manager.

Every execution is stored before it is dispatched, and every event before it is
delivered, so a client can reconnect and replay. ``ExecutionJournal`` keeps the
executions in memory while they run, numbers and stores their events, ends each
exactly once with a result that survives storage, and answers reads: one
execution, its events as a stream, waiting for its end, and a session's jobs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Final

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import (
    TERMINAL_STATES,
    Clock,
    ErrorInfo,
    ErrorKind,
    Event,
    Execution,
    ExecutionContext,
    ExecutionState,
    JobListing,
    JobStatus,
    Result,
    ResultEvent,
    StateEvent,
)
from sandbox_science.contracts.worker import DEFAULT_OUTPUT_LIMIT, MAX_EVENT_BYTES

from .connections import EnvironmentConnections
from .errors import WorkerError
from .live import Live
from .replies import clipped, integer, job_stream, shrunk, stream
from .store import Store
from .timeouts import ManagerTimeouts

_log = logging.getLogger(__name__)
EVENT: Final = TypeAdapter[Event](Event)
SOURCE_EVENT: ContextVar[tuple[str, int] | None] = ContextVar("executor_source", default=None)
"""The executor event being applied, by execution and source sequence: what it stores
acknowledges that event, so a daemon replays it no more."""

EVENT_PAGE = 64
"""Events read from the store at a time when a client replays a stream."""


class ExecutionJournal:
    """The executions in memory, and the store behind them."""

    def __init__(
        self,
        store: Store,
        connections: EnvironmentConnections,
        *,
        clock: Clock,
        timeouts: Callable[[], ManagerTimeouts],
    ) -> None:
        self.store = store
        self.connections = connections
        self.clock = clock
        self.timeouts = timeouts
        """The manager's timeouts, as they are when used."""
        self.live: dict[str, Live] = {}
        """Executions not ended, and those ended lately, by ID."""
        self.streams_ended = False
        """Set by ``end_streams``: event streams end without waiting for more."""

    def track(self, execution: Execution, limit: int = DEFAULT_OUTPUT_LIMIT) -> Live:
        live = Live(
            execution,
            execution.context.environment_id,
            asyncio.get_running_loop().create_future(),
            limit=limit,
        )
        self.live[execution.execution_id] = live
        return live

    def save(self, live: Live, **changes: Any) -> None:
        live.execution = live.execution.model_copy(update=changes)
        self.store.update_execution(live.execution)

    def changed(self, live: Live) -> None:
        changed, live.changed = live.changed, asyncio.get_running_loop().create_future()
        changed.set_result(None)

    def emit(self, live: Live, make: Callable[[int, datetime], Event]) -> None:
        seq = live.execution.last_seq + 1
        event = make(seq, self.clock.now())
        execution = live.execution.model_copy(update={"last_seq": seq})
        source = SOURCE_EVENT.get()
        acknowledged = (
            source[1] if source is not None and source[0] == execution.execution_id else None
        )
        self.store.commit_event(execution, event, acknowledged)
        # Only a stored event takes its number: a failed write leaves no gap.
        live.execution = execution
        self.changed(live)

    def state(self, live: Live, state: ExecutionState) -> None:
        execution_id = live.execution.execution_id
        self.emit(
            live,
            lambda seq, time: StateEvent(
                execution_id=execution_id, seq=seq, time=time, state=state
            ),
        )

    def finish(self, live: Live, state: ExecutionState, result: Result) -> None:
        """End an execution: its state event, result event and record are stored together."""
        if live.execution.state in TERMINAL_STATES:
            return
        execution_id = live.execution.execution_id
        final, events = self.final(live.execution, state, result)
        source = SOURCE_EVENT.get()
        acknowledged = live.final_source_seq
        if acknowledged is None and source is not None and source[0] == execution_id:
            acknowledged = source[1]
        self.store.finish(
            final,
            events,
            acknowledged,
            observation=live.observation,
            observation_gap=live.observation_gap,
        )
        live.execution = final
        self.changed(live)
        environment = self.connections.get(live.environment_id)
        if environment is not None:
            environment.running.discard(execution_id)
            environment.last_used = self.clock.monotonic()
        if live.watchdog is not None and live.watchdog is not asyncio.current_task():
            live.watchdog.cancel()
        if live.cancel_watchdog is not None and live.cancel_watchdog is not asyncio.current_task():
            live.cancel_watchdog.cancel()
        asyncio.get_running_loop().call_later(
            self.timeouts().retain_live, self.live.pop, execution_id, None
        )

    def final(
        self, execution: Execution, state: ExecutionState, result: Result
    ) -> tuple[Execution, tuple[StateEvent, ResultEvent]]:
        """The terminal record and its last two events, checked before anything is stored.

        Every value must survive a round trip through JSON and fit in one event
        line; a result that does not is shrunk, and failing that replaced.
        """
        unstorable = Result(
            error=ErrorInfo(
                kind=ErrorKind.INTERNAL, message="the attempt's result could not be stored"
            )
        )
        candidates = (
            (state, result),
            (state, shrunk(result)),
            (ExecutionState.FAILED, unstorable),
        )
        now = self.clock.now()
        seq = execution.last_seq
        for final_state, candidate in candidates:
            final = execution.model_copy(
                update={
                    "state": final_state,
                    "ended_at": now,
                    "result": candidate,
                    "last_seq": seq + 2,
                }
            )
            try:
                event = ResultEvent(
                    execution_id=execution.execution_id, seq=seq + 2, time=now, execution=final
                )
                encoded = EVENT.dump_json(event)
                if len(encoded) > MAX_EVENT_BYTES:
                    raise ValueError(f"{len(encoded)} bytes")
                checked = EVENT.validate_json(encoded)
            except ValueError as exc:  # PydanticSerializationError and ValidationError too
                _log.warning(
                    "execution %s: result not storable as reported (%s)",
                    execution.execution_id,
                    str(exc)[:300],
                )
                continue
            if not isinstance(checked, ResultEvent):
                continue
            state_event = StateEvent(
                execution_id=execution.execution_id, seq=seq + 1, time=now, state=final_state
            )
            return checked.execution, (state_event, checked)
        raise RuntimeError("the fallback result could not be stored")

    def get(self, execution_id: str, project_id: str | None = None) -> Execution:
        """The execution, which must belong to ``project_id`` when one is given."""
        live = self.live.get(execution_id)
        if live is not None:
            execution = live.execution
        else:
            try:
                found = self.store.execution(execution_id)
            except ValidationError as exc:
                raise WorkerError(
                    ErrorKind.INTERNAL, f"the stored record of {execution_id} is unreadable"
                ) from exc
            if found is None:
                raise WorkerError(ErrorKind.NOT_FOUND, f"no execution {execution_id}")
            execution = found
        if project_id is not None and execution.context.project_id != project_id:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no execution {execution_id}")
        return execution

    def running_jobs(self, environment_id: str) -> list[Execution]:
        return [
            live.execution
            for live in self.live.values()
            if live.environment_id == environment_id
            and live.execution.kind == "job"
            and live.execution.state not in TERMINAL_STATES
        ]

    def jobs(
        self, context: ExecutionContext, limit: int = 100, after: str | None = None
    ) -> JobListing:
        """List only the requested session's jobs, including recently ended attempts."""
        if not 1 <= limit <= 500:
            raise WorkerError(
                ErrorKind.INVALID_REQUEST, "job listing limit must be between 1 and 500"
            )
        try:
            jobs = self.store.session_jobs(context, limit + 1, after)
        except LookupError as exc:
            raise WorkerError(
                ErrorKind.NOT_FOUND, "no job listing cursor for this session"
            ) from exc
        selected: list[Execution] = []
        total = 0
        for job in jobs[:limit]:
            size = len(job.model_dump_json().encode())
            if total + size > 1024 * 1024:
                break
            selected.append(job)
            total += size
        return JobListing(
            jobs=tuple(selected),
            truncated=len(jobs) > len(selected),
            next_after=selected[-1].execution_id if selected else after,
        )

    async def job_status(self, execution_id: str, project_id: str | None = None) -> JobStatus:
        """Read bounded live output without opening a new executor or waiting for the job."""
        execution = self.get(execution_id, project_id)
        if execution.kind != "job":
            raise WorkerError(ErrorKind.NOT_FOUND, f"no job {execution_id}")
        result = execution.result
        if result is not None:
            error = result.error
            if error is not None:
                error = ErrorInfo(kind=error.kind, message=clipped(error.message, 4096) or "")
            compact = result.model_copy(
                update={
                    "stdout": None,
                    "stderr": None,
                    "outputs": (),
                    "written_files": (),
                    "notices": (),
                    "error": error,
                }
            )
            return JobStatus(
                execution=execution.model_copy(update={"result": compact}),
                stdout=job_stream(result.stdout),
                stderr=job_stream(result.stderr),
            )
        live = self.live.get(execution_id)
        channel = live.channel if live is not None else None
        if live is None or channel is None or channel.closed:
            return JobStatus(execution=execution)
        reply = await channel.request("job_status", execution=execution_id, reply_timeout=10)
        cpu = reply.get("cpu_seconds")
        cpu = (
            float(cpu)
            if isinstance(cpu, int | float) and not isinstance(cpu, bool) and 0 <= cpu <= 1e15
            else None
        )
        memory = integer(reply.get("memory_bytes"), 0, 2**62)
        execution = self.get(execution_id)
        if execution.result is not None:
            return await self.job_status(execution_id, project_id)
        return JobStatus(
            execution=execution,
            stdout=stream(reply.get("stdout"), "stdout", min(live.limit, 16384)),
            stderr=stream(reply.get("stderr"), "stderr", min(live.limit, 16384)),
            cpu_seconds=cpu,
            memory_bytes=memory,
        )

    async def events(
        self, execution_id: str, after: int, *, heartbeat: float | None = None
    ) -> AsyncGenerator[Event | None, None]:
        """Yield the events after ``after``, then new ones, ending with the result.

        While nothing happens, ``None`` comes every ``heartbeat`` seconds. The
        stream also ends when the execution is over, whatever ``after`` was, and
        without a result once ``end_streams`` was called.
        """
        self.get(execution_id)
        while True:
            live = self.live.get(execution_id)
            changed = live.changed if live is not None else None
            over = live is None or live.execution.state in TERMINAL_STATES or self.streams_ended
            while True:
                try:
                    page = self.store.events_after(execution_id, after, EVENT_PAGE)
                except ValidationError as exc:
                    raise WorkerError(
                        ErrorKind.INTERNAL, f"a stored event of {execution_id} is unreadable"
                    ) from exc
                for event in page:
                    yield event
                    after = event.seq
                    if isinstance(event, ResultEvent):
                        return
                if len(page) < EVENT_PAGE:
                    break
            if over or changed is None:
                return
            # Every reader shares the future: one that goes away must not cancel it.
            if heartbeat is None:
                await asyncio.shield(changed)
                continue
            try:
                await asyncio.wait_for(asyncio.shield(changed), heartbeat)
            except TimeoutError:
                yield None

    def end_streams(self) -> None:
        """End every event stream, so that shutdown does not wait for its readers.

        Readers ask again with ``after``, from this worker once it runs again.
        """
        self.streams_ended = True
        for live in self.live.values():
            self.changed(live)

    async def wait(self, execution_id: str) -> Execution:
        """The execution once it is terminal."""
        while True:
            live = self.live.get(execution_id)
            if live is None:
                return self.get(execution_id)
            if live.execution.state in TERMINAL_STATES:
                return live.execution
            # Every waiter shares the future: one that goes away must not cancel it.
            await asyncio.shield(live.changed)
