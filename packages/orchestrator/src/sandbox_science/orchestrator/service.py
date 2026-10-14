# SPDX-License-Identifier: MIT
"""The run service: the session's main agent, run for the GUI (docs/specs/run-service.md).

``sandbox-science-harness serve`` builds one ``Harness`` and runs a ``RunService`` on it
until it is stopped (RS-1). The service and the workspace share the control store
(RS-2): about once a ``poll``, the service

- renews its heartbeat, which the workspace shows (RS-3);
- ends every run whose lease expired, its holder dead, unless the run is one of its
  own (a lease can look expired while its holder lives, as after a suspend). A run
  whose record was saved is acknowledged from the record; any other gets a ``stopped``
  event, ``interrupted``, and the messages it was offered are held until the user
  resumes or writes again (RS-12);
- starts a run for each session that has queued messages and no run (RS-6), within the
  project's model budget (RS-6a). The run's conversation begins with the session's
  earlier tasks and answers (RS-10), then the messages queued once it holds the lease;
- cancels a run the user asked to cancel (RS-11), once, while it runs: the run ends
  ``cancelled``, its record is saved, and the messages it was offered and did not
  answer are held.

Each run is admitted, run, saved and acknowledged as ``run`` does, holding the session's
run lease from admission until its record is saved. A session whose runs fail waits
``retry`` seconds, doubling with each failure in a row up to ``MAX_WAIT``; after
``MAX_FAILURES`` in a row, its queued messages are held with the reason ``failed`` (RS-9).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Literal

from pydantic import JsonValue

from sandbox_science.contracts import SYSTEM_CLOCK, Clock

from .aio import threaded
from .chat import SOURCE, session_lease
from .control import ControlError
from .control.errors import BUSY
from .harness import Harness, RunRequest, admit, save
from .model import Message
from .projects import ProjectError, ProjectStore, run_name
from .runstate import RunState
from .tools import ToolOutcome

_log = logging.getLogger(__name__)

POLL_SECONDS: Final = 1.0
BEAT_SECONDS: Final = 10.0
RETRY_SECONDS: Final = 30.0
MAX_WAIT: Final = 900.0
"""The longest wait between a session's failing runs."""
MAX_FAILURES: Final = 5
"""Failed runs in a row after which a session's messages are held."""
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
    phase: Literal["starting", "running", "saving"] = "starting"
    """Only a ``running`` run is cancelled: never its saving and acknowledgements."""
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
        self.failures: dict[tuple[str, str], int] = {}
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
                    await self._ended(project, session, active.run)

    async def tick(self) -> None:
        """One look at the control store."""
        now = self.clock.monotonic()
        if now - self._beaten >= BEAT_SECONDS:
            await threaded(self.runs.beat, self.holder)
            self._beaten = now
        for dead in await threaded(self.runs.dead):
            mine = self.active.get((dead.project, dead.session))
            if mine is not None and mine.run == dead.run:
                continue  # alive: its renewals lag, as after a suspend
            if await threaded(self.runs.reap, dead):
                _log.warning("run %s of session %s was interrupted", dead.run, dead.session)
                await self._ended(dead.project, dead.session, dead.run)
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
        for key in await threaded(self.runs.waiting):
            if key in self.active or self.waiting_until.get(key, now) > now:
                continue
            if await threaded(self.runs.lease, *key) is None:
                active = self.active[key] = _Active()
                active.task = asyncio.create_task(self._run(*key, active))

    async def _ended(self, project: str, session: str, run: str) -> None:
        """End a run whose holder is gone (RS-12): from its record if it was saved,
        otherwise as ``interrupted``, with the messages it was offered held."""
        runs = self.runs
        try:
            record = await threaded(self.stores.projects.run, project, session, run)
        except ProjectError:
            record = None
        stopped: dict[str, JsonValue]
        if record is not None:
            # It died after saving its record, before acknowledging it.
            await threaded(runs.deliver, project, session, record.delivered.get(SOURCE, []), run)
            stopped = {
                "stopped": record.stopped,
                "failure": record.failure,
                "final_text": record.final_text,
                "record": run,
            }
        else:
            stopped = {
                "stopped": "interrupted",
                "failure": None,
                "final_text": None,
                "record": None,
            }
            offered = await threaded(runs.offered_to, project, session, run)
            await threaded(runs.hold, project, session, offered)
        if not await threaded(runs.ended, project, session, run):
            await threaded(runs.append, project, session, run, "stopped", stopped)

    async def _run(self, project: str, session: str, active: _Active) -> None:
        key = project, session
        try:
            failed = await self._started(project, session, active)
        except asyncio.CancelledError:
            if not active.stop.is_set():
                raise
            # Cancelled before the run began: its messages wait for the user too.
            await threaded(self.runs.hold, project, session, active.message_ids)
            return
        except ControlError as error:
            if error.kind != BUSY:
                raise
            # Another run, or another service, took the session first.
            _log.info("session %s is busy: %s", session, error.message)
            self.waiting_until[key] = self.clock.monotonic() + self.retry
            return
        except Exception:
            _log.exception("a run of session %s could not run", session)
            failed = True
        if not failed:
            self.failures.pop(key, None)
            return
        count = self.failures[key] = self.failures.get(key, 0) + 1
        if count >= MAX_FAILURES:
            _log.error("%d runs of session %s failed in a row: its messages wait", count, session)
            queued = await threaded(self.runs.messages, project, session, state="queued")
            ids = [message.message_id for message in queued]
            await threaded(self.runs.hold, project, session, ids, f"{count} runs failed in a row")
            self.failures.pop(key)
            return
        wait = min(self.retry * 2 ** (count - 1), MAX_WAIT)
        self.waiting_until[key] = self.clock.monotonic() + wait

    async def _started(self, project: str, session: str, active: _Active) -> bool:
        """Run the session's queued messages; whether the run failed."""
        runs, stores = self.runs, self.stores
        sink = LogSink()
        admitted = await admit(
            self.harness.client,
            stores.projects,
            RunRequest(project, session, ""),
            sink,
            min_isolation_tier=self.harness.settings.min_isolation_tier,
        )
        active.run = name = run_name(admitted.started_at, admitted.run_id)
        async with session_lease(runs, admitted.context, name, clock=self.clock):
            # Read once the session is held: another run may have answered them.
            queued = await threaded(runs.messages, project, session, state="queued")
            if not queued:
                return False
            ids = active.message_ids = [message.message_id for message in queued]
            since = self.clock.now() - WINDOW
            if await threaded(runs.spent, project, since) >= await threaded(runs.budget, project):
                _log.warning("project %s has spent its model budget", project)
                await threaded(runs.refuse, project, session, ids, "budget")
                return False
            history = await threaded(
                earlier_conversation, stores.projects, runs, project, session, HISTORY_CHARS
            )
            request = dataclasses.replace(
                admitted.request,
                task="\n\n".join(message.text for message in queued),
                history=tuple(history),
                message_ids=tuple(ids),
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
            if finished.transcript.stopped == "cancelled":
                # What it was offered and did not answer waits for the user (RS-11).
                offered = await threaded(runs.offered_to, project, session, name)
                await threaded(runs.hold, project, session, offered)
        return finished.transcript.failure is not None or status == 1


def earlier_conversation(
    projects: ProjectStore, runs: RunState, project: str, session: str, budget: int
) -> list[Message]:
    """The session's earlier runs as conversation (RS-10): each one's task and the
    user's messages it delivered, then its answer, oldest first. A run that delivered
    nothing is left out. The newest that fit in ``budget`` characters are kept, and a
    line says how many were left out."""
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
        question = "\n\n".join(part for part in parts if part)
        if not question:
            continue
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
