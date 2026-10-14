# SPDX-License-Identifier: MIT
"""The run service: the session's main agent, run for the GUI (docs/specs/run-service.md).

``sandbox-science-harness serve`` builds one ``Harness`` and runs a ``RunService`` on it
until it is stopped (RS-1). The service and the workspace share the control store
(RS-2): about once a ``poll``, the service

- renews its heartbeat, which the workspace shows (RS-3);
- ends every run whose lease expired: its holder died. The run gets a ``stopped``
  event, ``interrupted``, and its messages are held until the user resumes (RS-12);
- starts a run for each session that has queued messages and no run (RS-6), within the
  project's model budget (RS-6a). The run's conversation begins with the session's
  earlier tasks and answers (RS-10), then the queued messages;
- cancels a run the user asked to cancel (RS-11): the run ends ``cancelled``, its record
  is saved, and the messages it saw but did not answer are held.

Each run is admitted, run, saved and acknowledged as ``run`` does, holding the session's
run lease from admission until its record is saved. A session whose run could not
start, or failed, waits ``retry`` seconds before the next.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from sandbox_science.contracts import SYSTEM_CLOCK, Clock

from .aio import threaded
from .chat import session_lease
from .harness import Harness, RunRequest, admit, save
from .model import Message
from .projects import ProjectStore, run_name
from .runstate import RunState
from .tools import ToolOutcome

_log = logging.getLogger(__name__)

POLL_SECONDS: Final = 1.0
BEAT_SECONDS: Final = 10.0
RETRY_SECONDS: Final = 30.0
HISTORY_CHARS: Final = 100_000
"""The longest earlier conversation a run starts with: a quarter of its input (RS-10)."""
WINDOW: Final = timedelta(hours=24)
"""The model budget's window (RS-6a)."""


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
    message_ids: list[str] = field(default_factory=list[str])


class RunService:
    """Runs a harness's sessions for the GUI. The harness needs its stores."""

    def __init__(
        self,
        harness: Harness,
        *,
        poll: float = POLL_SECONDS,
        retry: float = RETRY_SECONDS,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if harness.stores is None:
            raise ValueError("the run service needs the state directory's stores")
        self.harness, self.stores = harness, harness.stores
        self.poll, self.retry, self.clock = poll, retry, clock
        self.runs: RunState = harness.stores.runs
        self.active: dict[tuple[str, str], _Active] = {}
        self.waiting_until: dict[tuple[str, str], float] = {}
        self.holder = secrets.token_hex(16)
        self._beaten = -BEAT_SECONDS

    async def serve(self) -> None:
        """Run until cancelled. The runs in progress then end ``interrupted``."""
        try:
            while True:
                await self.tick()
                await self.clock.sleep(self.poll)
        finally:
            for (project, session), active in list(self.active.items()):
                if active.task is not None:
                    active.task.cancel()
                    with contextlib.suppress(BaseException):
                        await active.task
                if active.run is not None:
                    await self._interrupted(project, session, active.run)

    async def tick(self) -> None:
        """One look at the control store."""
        now = self.clock.monotonic()
        if now - self._beaten >= BEAT_SECONDS:
            await threaded(self.runs.beat, self.holder)
            self._beaten = now
        for dead in await threaded(self.runs.dead):
            _log.warning("run %s of session %s was interrupted", dead.run, dead.session)
            await self._interrupted(dead.project, dead.session, dead.run)
            await threaded(self.runs.release, dead.project, dead.session, dead.holder)
        for key, active in list(self.active.items()):
            if active.task is not None and active.task.done():
                del self.active[key]
            elif active.run is not None and await threaded(self.runs.cancelled, *key, active.run):
                active.stop.set()
                if active.task is not None:
                    active.task.cancel()
        for key in await threaded(self.runs.waiting):
            if key in self.active or self.waiting_until.get(key, now) > now:
                continue
            if await threaded(self.runs.lease, *key) is None:
                active = self.active[key] = _Active()
                active.task = asyncio.create_task(self._run(*key, active))

    async def _interrupted(self, project: str, session: str, run: str) -> None:
        """End a run that left no record (RS-12): its messages wait for the user."""
        if not await threaded(self.runs.ended, project, session, run):
            await threaded(
                self.runs.append,
                project,
                session,
                run,
                "stopped",
                {"stopped": "interrupted", "failure": None, "final_text": None, "record": None},
            )
        ids = await threaded(self.runs.run_messages, project, session, run)
        await threaded(self.runs.hold, project, session, ids)

    async def _run(self, project: str, session: str, active: _Active) -> None:
        try:
            if await self._started(project, session, active) == 1:
                self.waiting_until[project, session] = self.clock.monotonic() + self.retry
        except asyncio.CancelledError:
            if not active.stop.is_set():
                raise
            # Cancelled before the run began: its messages wait for the user too.
            await threaded(self.runs.hold, project, session, active.message_ids)
        except Exception:
            _log.exception("a run of session %s could not run", session)
            self.waiting_until[project, session] = self.clock.monotonic() + self.retry

    async def _started(self, project: str, session: str, active: _Active) -> int:
        runs, stores = self.runs, self.stores
        queued = await threaded(runs.messages, project, session, state="queued")
        if not queued:
            return 0
        ids = active.message_ids = [message.message_id for message in queued]
        since = self.clock.now() - WINDOW
        if await threaded(runs.spent, project, since) >= await threaded(runs.budget, project):
            _log.warning("project %s has spent its model budget", project)
            await threaded(runs.refuse, project, session, ids, "budget")
            return 0
        history = await threaded(
            earlier_conversation, stores.projects, runs, project, session, HISTORY_CHARS
        )
        request = RunRequest(
            project,
            session,
            "\n\n".join(message.text for message in queued),
            history=tuple(history),
            message_ids=tuple(ids),
        )
        sink = LogSink()
        admitted = await admit(
            self.harness.client,
            stores.projects,
            request,
            sink,
            min_isolation_tier=self.harness.settings.min_isolation_tier,
        )
        active.run = name = run_name(admitted.started_at, admitted.run_id)
        async with session_lease(runs, admitted.context, name, clock=self.clock):
            finished = await self.harness.start(admitted, sink, stop=active.stop)
            status = await save(
                finished.fields(),
                finished.status,
                finished.hooks,
                projects=stores.projects,
                error=sink.error,
            )
            if finished.transcript.stopped == "cancelled":
                # What it saw and did not answer waits for the user (RS-11).
                await threaded(
                    runs.hold,
                    project,
                    session,
                    await threaded(runs.run_messages, project, session, name),
                )
        return 1 if finished.transcript.failure is not None else status


def earlier_conversation(
    projects: ProjectStore, runs: RunState, project: str, session: str, budget: int
) -> list[Message]:
    """The session's earlier runs as conversation (RS-10): each one's task and the
    user's messages it delivered, then its answer, oldest first. The newest that fit in
    ``budget`` characters are kept, and a line says how many were left out."""
    turns: list[list[Message]] = []
    used = left_out = 0
    for entry in reversed(projects.runs(project, session)):
        if left_out:
            left_out += 1
            continue
        record = projects.run(project, session, entry.name)
        summary = record.summaries.get("messages")
        initial = isinstance(summary, dict) and bool(summary.get("initial"))
        parts = ([] if initial else [record.task]) + runs.delivered_texts(
            project, session, entry.name
        )
        question = "\n\n".join(parts)
        answer = record.final_text or f"(The run ended without an answer: {record.stopped}.)"
        if used + len(question) + len(answer) > budget:
            left_out = 1
            continue
        used += len(question) + len(answer)
        turns.append(
            [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
        )
    found: list[Message] = []
    if left_out:
        found.append(
            {"role": "user", "content": f"({left_out} earlier runs of this session are left out.)"}
        )
    for turn in reversed(turns):
        found.extend(turn)
    return found
