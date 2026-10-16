# SPDX-License-Identifier: MIT
"""The run service (run-service spec RS-1 to RS-12 and its fixtures).

A ``RunService`` runs in the test's event loop on a harness built as ``serve`` builds
it, against the in-process worker and the scripted gateway of ``loop_harness``. The
test writes messages and cancels through ``RunState``, as the workspace does, and reads
back records, events and message states.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import signal
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator import service as service_module
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.harness import Harness as Core
from sandbox_science.orchestrator.harness import Role, Settings, Stores
from sandbox_science.orchestrator.harness import admit as harness_admit
from sandbox_science.orchestrator.history import body
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.model import OpenAICompatibleModel, ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.runstate import DeadLease, RunState
from sandbox_science.orchestrator.service import RunService
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock, eventually

from .loop_harness import (
    API_KEY,
    PROJECT,
    SESSION,
    TOKEN,
    Harness,
    Reply,
    call,
    start_job,
    user_text,
)
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

INTERRUPTIBLE = (
    sys.executable,
    "-c",
    "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); "
    "os.execv(sys.argv[1], sys.argv[1:])",
)
"""Runs a command with SIGINT as a terminal's foreground process has it. A shell that
starts the tests as a background job, as a script's ``&`` or an agent's background
command does, starts them with SIGINT ignored. A child keeps an ignored signal, and
Python then never turns SIGINT into KeyboardInterrupt, so ``serve`` would not stop."""


@contextlib.asynccontextmanager
async def serving(
    harness: Harness,
    *script: Reply,
    lease_seconds: float = 60.0,
    retry: float = 0.2,
    clock: Clock = SYSTEM_CLOCK,
    run_share: float | None = None,
    serve: bool = True,
) -> AsyncIterator[RunService]:
    """The run service on the harness's state directory, worker and gateway, keeping the
    projects' cost limits during its runs as ``serve`` does."""
    harness.gateway.script = list(script)
    projects = ProjectStore(harness.state)
    documents = DocumentStore(store_path(harness.state), producer="harness")
    model = OpenAICompatibleModel(harness.gateway.url, "main", API_KEY)
    try:
        async with WorkerClient(harness.worker.url, TOKEN) as client:
            stores = Stores(
                projects,
                documents,
                JobState(harness.state, control=documents.control),
                RunState(projects.control, lease_seconds=lease_seconds, clock=clock),
            )
            core = Core(
                client,
                Role(model, "main"),
                stores=stores,
                settings=Settings(min_isolation_tier=None, cost_limits=True, run_share=run_share),
                clock=clock,
            )
            service = RunService(core, poll=0.02, retry=retry, clock=clock)
            # Without ``serve``, the test takes the service's looks itself, with ``tick``.
            task = asyncio.create_task(service.serve()) if serve else None
            try:
                yield service
            finally:
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
    finally:
        await model.close()
        documents.close()
        projects.close()


def runs(harness: Harness) -> RunState:
    return RunState(ProjectStore(harness.state).control)


def records(harness: Harness) -> list[RunRecord]:
    store = ProjectStore(harness.state)
    return [store.run(PROJECT, SESSION, entry.name) for entry in store.runs(PROJECT, SESSION)]


def names(harness: Harness) -> list[str]:
    return [entry.name for entry in ProjectStore(harness.state).runs(PROJECT, SESSION)]


def states(harness: Harness) -> dict[str, str]:
    return {m.message_id: m.state for m in runs(harness).messages(PROJECT, SESSION)}


async def ended(harness: Harness, count: int = 1) -> list[RunRecord]:
    """Wait until the session keeps ``count`` records and the last one's events end."""

    def done() -> bool:
        found = names(harness)
        return len(found) == count and runs(harness).ended(PROJECT, SESSION, found[-1])

    await eventually(done, timeout=20)
    return records(harness)


def post(harness: Harness, message_id: str, text: str) -> None:
    runs(harness).post(PROJECT, SESSION, message_id, text, author="user")


async def test_a_first_message_starts_a_run(harness: Harness) -> None:
    post(harness, "m-1", "What is $2+2$?")
    async with serving(harness, Reply("It is $4$.")):
        (record,) = await ended(harness)
        assert runs(harness).service_running()
    assert (record.task, record.final_text) == ("What is $2+2$?", "It is $4$.")
    assert record.delivered["messages"] == ["m-1"]
    assert states(harness) == {"m-1": "delivered"}
    (first,) = harness.gateway.main
    assert user_text(first[-1]) == "What is $2+2$?"
    events = runs(harness).events(PROJECT, SESSION, names(harness)[0])
    assert [e.kind for e in events] == ["started", "user", "model", "assistant", "stopped"]
    assert events[0].data["message_ids"] == ["m-1"]
    assert events[1].data == {"message_id": "m-1", "text": "What is $2+2$?"}


async def test_a_dedicated_session_runs_in_its_own_environment(harness: Harness) -> None:
    """A session opened with an environment of its own keeps it for its runs (lab 38):
    the service does not reopen it as one of the project's."""
    projects = ProjectStore(harness.state)
    opened = projects.open_session(PROJECT, "d", dedicated=True)
    default = projects.get(PROJECT).default_environment_id
    assert opened.environment_id != default
    runs(harness).post(PROJECT, "d", "m-d", "Hello.", author="user")
    async with serving(harness, Reply("Hello.")):

        def saved() -> bool:
            found = projects.runs(PROJECT, "d")
            return len(found) == 1 and runs(harness).ended(PROJECT, "d", found[0].name)

        await eventually(saved, timeout=20)
    (entry,) = projects.runs(PROJECT, "d")
    record = projects.run(PROJECT, "d", entry.name)
    assert record.context.environment_id == opened.environment_id
    assert record.final_text == "Hello."
    assert projects.get(PROJECT).sessions["d"].dedicated


async def test_a_later_message_starts_a_run_from_the_sessions_view(
    harness: Harness,
) -> None:
    """CM-F17, CM-29 (replacing run-service fixture 4): the second run's view holds the
    first run's messages as they were sent, its tool calls and results included."""
    post(harness, "m-1", "First question.")
    async with serving(
        harness, Reply(calls=[call("plan_read")]), Reply("First answer."), Reply("Second answer.")
    ):
        await ended(harness)
        post(harness, "m-2", "Second question.")
        second = (await ended(harness, 2))[-1]
    assert second.final_text == "Second answer."
    _, first_run, last = harness.gateway.main
    earlier = [m for m in first_run if m["role"] != "system"]
    conversation = [m for m in last if m["role"] != "system"]
    assert conversation[: len(earlier) + 1] == [
        *earlier,
        {"role": "assistant", "content": "First answer."},
    ]
    assert any(m["role"] == "tool" for m in conversation[: len(earlier)])
    assert user_text(conversation[-1]) == "Second question."
    # Lab 41: the review controller observes the second run's own messages, not the
    # earlier conversation it starts with, which the first run's window already holds.
    documents = DocumentStore(store_path(harness.state), producer="test")
    try:
        with documents.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT locator, payload FROM review_feed WHERE kind='transcript' ORDER BY seq"
            ).fetchall()
    finally:
        documents.close()
    observed: dict[str, list[str]] = {}
    for locator, payload in rows:
        message = json.loads(json.loads(payload)["content"])
        content = message["content"]  # after its archive header (CM-4a)
        observed.setdefault(json.loads(locator)["run"], []).append(content and body(content))
    first_run, second_run = observed
    assert "First question." in observed[first_run]
    assert "Second question." in observed[second_run]
    assert "First question." not in observed[second_run]
    assert "First answer." not in observed[second_run]


async def test_a_session_another_run_holds_waits(harness: Harness) -> None:
    state = runs(harness)
    state.acquire(PROJECT, SESSION, "cli-run", "cli-holder")
    post(harness, "m-1", "Hello.")
    async with serving(harness, Reply("Hello to you.")):
        await asyncio.sleep(0.2)
        assert harness.gateway.main == []
        state.release(PROJECT, SESSION, "cli-holder")
        await ended(harness)
    assert states(harness) == {"m-1": "delivered"}


async def test_cancel_during_a_tool_call_saves_a_cancelled_record(harness: Harness) -> None:
    waiting = Reply()
    jobs: list[str] = []

    async def wait_on_it() -> None:
        async with WorkerClient(harness.worker.url, TOKEN) as client:
            listing = await client.jobs(harness.context())
        jobs.extend(j.execution_id for j in listing.jobs)
        waiting.calls = [call("wait_jobs", job_ids=jobs)]

    waiting.before = wait_on_it
    post(harness, "m-1", "Run the long job.")
    async with serving(harness, Reply(calls=[start_job()]), waiting):
        state = runs(harness)

        def in_wait() -> bool:
            lease = state.lease(PROJECT, SESSION)
            return lease is not None and any(
                e.kind == "waiting" and e.data["job_ids"]
                for e in state.events(PROJECT, SESSION, lease.run)
            )

        await eventually(in_wait, timeout=20)
        lease = state.lease(PROJECT, SESSION)
        assert lease is not None
        state.cancel(PROJECT, SESSION, lease.run)
        (record,) = await ended(harness)
    assert (record.stopped, record.failure, record.final_text) == ("cancelled", None, None)
    assert [t["tool"] for t in record.tools] == ["start_job"]  # the wait never ended
    stopped = runs(harness).events(PROJECT, SESSION, names(harness)[0])[-1]
    assert stopped.data["stopped"] == "cancelled"
    assert states(harness) == {"m-1": "delivered"}
    async with WorkerClient(harness.worker.url, TOKEN) as client:
        status = await client.job_status(jobs[0], project_id=PROJECT)
    assert status.execution.state == "running"  # jobs outlive their run
    await harness.end_job()


async def test_an_interrupted_run_ends_and_its_messages_wait_for_resume(
    harness: Harness,
) -> None:
    past = FakeClock(datetime.now(UTC) - timedelta(minutes=5))
    dead = RunState(ProjectStore(harness.state).control, clock=past)
    run = "20261014T000000000000Z-crashed"
    post(harness, "m-1", "Are you there?")
    dead.acquire(PROJECT, SESSION, run, "a-dead-process")
    dead.append(PROJECT, SESSION, run, "started", {"message_ids": ["m-1"]})
    dead.offer(PROJECT, SESSION, ["m-1"], run)
    async with serving(harness, Reply("Yes.")):
        await eventually(lambda: runs(harness).ended(PROJECT, SESSION, run), timeout=10)
        await eventually(lambda: states(harness) == {"m-1": "held"})
        await asyncio.sleep(0.2)
        assert harness.gateway.main == []  # held: no run starts by itself
        assert runs(harness).resume(PROJECT, SESSION) == 1
        (record,) = await ended(harness)
    assert record.delivered["messages"] == ["m-1"]
    stopped = runs(harness).events(PROJECT, SESSION, run)[-1]
    assert stopped.data == {
        "stopped": "interrupted",
        "failure": None,
        "final_text": None,
        "record": None,
    }


async def test_a_reached_cost_limit_refuses_the_message(harness: Harness) -> None:
    await harness.run("run-1", [Reply("A priced answer.", cost="0.75")])
    runs(harness).set_cost_limit(PROJECT, 0.5)
    post(harness, "m-1", "Expensive question.")
    async with serving(harness, Reply("Never sent.")):
        await eventually(lambda: states(harness) == {"m-1": "refused"})
    (message,) = runs(harness).messages(PROJECT, SESSION)
    assert message.reason == "budget"
    assert len(harness.gateway.main) == 1  # the earlier run's call alone
    assert len(names(harness)) == 1


async def test_the_projects_cost_counts_what_the_gateway_priced(harness: Harness) -> None:
    state = runs(harness)
    assert state.model_cost(PROJECT) is None  # nothing priced yet
    await harness.run("run-1", [Reply("Answer.", cost="0.25")])
    run = "29991231T000000000000Z-interrupted"  # an interrupted run: events, no record
    state.append(PROJECT, SESSION, run, "assistant", {"usage": {"input_tokens": 1, "cost": 0.5}})
    state.append(PROJECT, SESSION, run, "assistant", {"usage": {"input_tokens": 9}})
    assert state.model_cost(PROJECT) == 0.75
    assert state.model_cost(PROJECT, datetime.now(UTC) - timedelta(hours=1)) == 0.75
    assert state.model_cost(PROJECT, datetime(3000, 1, 1, tzinfo=UTC)) is None


def test_the_service_needs_stores() -> None:
    with pytest.raises(ValueError, match="stores"):
        RunService(Core(cast(WorkerClient, object()), Role(ScriptedModel([]), "scripted")))


async def test_serve_runs_the_sessions_until_stopped(harness: Harness) -> None:
    """``sandbox-science-harness serve`` as a process: it answers a message, then stops
    on an interrupt."""
    token, key = harness.tmp / "worker.token", harness.tmp / "gateway.key"
    token.write_text(TOKEN)
    key.write_text(API_KEY)
    harness.gateway.script = [Reply("Served.")]
    post(harness, "m-1", "Are you serving?")
    process = await asyncio.create_subprocess_exec(
        *INTERRUPTIBLE, str(Path(sys.executable).parent / "sandbox-science-harness"),
        "serve", "--worker", harness.worker.url, "--token-file", str(token),
        "--state-dir", str(harness.state), "--allow-unisolated",
        "--model-url", harness.gateway.url, "--api-key-file", str(key), "--poll", "0.05",
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        (record,) = await ended(harness)
        assert record.final_text == "Served."
        assert runs(harness).service_running()
    finally:
        process.send_signal(signal.SIGINT)
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), 20)
        finally:
            if process.returncode is None:  # it did not stop: it must not outlive the test
                process.kill()
                await process.wait()
    assert b"serving the sessions of" in stderr
    assert b"stopped: runs in progress end interrupted" in stderr
    assert process.returncode == 0
    assert API_KEY.encode() not in stderr


class Gate:
    """A model call that waits until the test lets it answer."""

    def __init__(self) -> None:
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self) -> None:
        self.entered.set()
        await self.release.wait()


async def test_the_service_never_ends_its_own_live_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RS-5, RS-12: a lease that looks expired while its holder lives (a suspend) is not
    the service's to end."""
    gate = Gate()
    post(harness, "m-1", "Think hard.")
    original = RunState.dead

    def dead(self: RunState) -> list[DeadLease]:
        found = original(self)
        lease = self.lease(PROJECT, SESSION)
        if lease is not None:  # the live run's lease, seen as expired
            found.append(DeadLease(project=PROJECT, session=SESSION, run=lease.run, holder="x"))
        return found

    monkeypatch.setattr(RunState, "dead", dead)
    async with serving(harness, Reply("Thought.", before=gate)):
        await asyncio.wait_for(gate.entered.wait(), 20)
        await asyncio.sleep(0.3)  # several passes see the "expired" lease
        assert states(harness) == {"m-1": "queued"}
        gate.release.set()
        (record,) = await ended(harness)
    stops = [
        e.data["stopped"]
        for e in runs(harness).events(PROJECT, SESSION, names(harness)[0])
        if e.kind == "stopped"
    ]
    assert record.final_text == "Thought."
    assert stops == ["answered"]
    assert states(harness) == {"m-1": "delivered"}


async def test_a_lease_that_expires_between_two_looks_is_ended_not_taken_over(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RS-12. A tick looks for dead leases, then for sessions to run. A lease that expires
    between the two looks used to be taken over by the session's next run, so the dead
    run never ended and its message went to the new run instead of being held."""
    past = FakeClock(datetime.now(UTC) - timedelta(minutes=5))
    dead_state = RunState(ProjectStore(harness.state).control, clock=past)
    run = "20261014T000000000000Z-crashed"
    post(harness, "m-1", "Are you there?")
    dead_state.acquire(PROJECT, SESSION, run, "a-dead-process")
    dead_state.offer(PROJECT, SESSION, ["m-1"], run)
    looks: list[None] = []
    dead = RunState.dead

    def later(self: RunState) -> list[DeadLease]:
        looks.append(None)
        return [] if len(looks) == 1 else dead(self)  # the first look, just before it expired

    monkeypatch.setattr(RunState, "dead", later)
    async with serving(harness, Reply("Not this run."), serve=False) as service:
        await service.tick()  # no dead lease yet; by the look for sessions, it has expired
        assert service.active == {}  # so no run takes the session over
        await service.tick()  # the next look ends the dead run first
        assert runs(harness).ended(PROJECT, SESSION, run)
        assert states(harness) == {"m-1": "held"}
    assert names(harness) == []
    assert harness.gateway.main == []
    stopped = runs(harness).events(PROJECT, SESSION, run)[-1]
    assert stopped.data["stopped"] == "interrupted"


def test_a_lease_renewed_since_it_was_seen_dead_is_not_reaped(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 14, tzinfo=UTC))
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    state = RunState(projects.control, clock=clock)
    state.acquire("p", "s", "run-a", "holder-a")
    clock.advance(61)
    (dead,) = state.dead()
    state.renew("p", "s", "holder-a")  # it was alive after all
    assert not state.reap(dead)
    assert state.lease("p", "s") is not None
    clock.advance(61)
    assert state.reap(dead)
    assert state.lease("p", "s") is None
    projects.close()


async def test_a_run_starts_only_for_messages_still_queued_once_it_holds_the_session(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = harness_admit

    async def admit(*args: Any, **kwargs: Any) -> Any:
        admitted = await real(*args, **kwargs)
        # Another run answers the message while this one is admitted.
        runs(harness).deliver(PROJECT, SESSION, ["m-1"], "20261014T000000000000Z-other")
        return admitted

    monkeypatch.setattr(service_module, "admit", admit)
    post(harness, "m-1", "Only once, please.")
    async with serving(harness, Reply("A second answer to nothing.")):
        await asyncio.sleep(1.0)
        assert runs(harness).lease(PROJECT, SESSION) is None
    assert harness.gateway.main == []
    assert names(harness) == []


async def test_a_cancel_racing_the_end_leaves_the_saved_record_alone(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RunState.deliver

    def deliver(
        self: RunState, project: str, session: str, ids: list[str], run: str, **kw: Any
    ) -> None:
        self.cancel(project, session, run)  # the user's cancel arrives as it acknowledges
        time.sleep(0.3)
        original(self, project, session, ids, run, **kw)

    monkeypatch.setattr(RunState, "deliver", deliver)
    post(harness, "m-1", "Quick one.")
    async with serving(harness, Reply("Quick answer.")):
        (record,) = await ended(harness)
        await asyncio.sleep(0.3)
    events = runs(harness).events(PROJECT, SESSION, names(harness)[0])
    assert record.stopped == "answered"
    assert events[-1].data["stopped"] == "answered"
    assert states(harness) == {"m-1": "delivered"}


async def test_a_cancel_during_a_model_call_holds_what_the_run_did_not_answer(
    harness: Harness,
) -> None:
    gate = Gate()
    post(harness, "m-1", "Take your time.")
    async with serving(harness, Reply("Never.", before=gate)):
        await asyncio.wait_for(gate.entered.wait(), 20)
        lease = runs(harness).lease(PROJECT, SESSION)
        assert lease is not None
        runs(harness).cancel(PROJECT, SESSION, lease.run)
        (record,) = await ended(harness)
        gate.release.set()
    assert (record.stopped, record.final_text) == ("cancelled", None)
    assert states(harness) == {"m-1": "held"}  # offered, never answered


async def test_a_cancel_during_a_foreground_tool_call(harness: Harness) -> None:
    post(harness, "m-1", "Sleep a while.")
    async with serving(
        harness, Reply(calls=[call("shell", script="sleep 3; echo late")]), Reply("Never.")
    ):
        state = runs(harness)

        def calling() -> bool:
            lease = state.lease(PROJECT, SESSION)
            return lease is not None and any(
                e.kind == "assistant" for e in state.events(PROJECT, SESSION, lease.run)
            )

        await eventually(calling, timeout=20)
        lease = state.lease(PROJECT, SESSION)
        assert lease is not None
        state.cancel(PROJECT, SESSION, lease.run)
        (record,) = await ended(harness)
    assert record.stopped == "cancelled"
    assert record.tools == []  # the call never returned
    assert len(harness.gateway.main) == 1
    assert states(harness) == {"m-1": "delivered"}  # the first call saw it


async def test_a_failing_session_waits_longer_each_time_then_holds_its_messages(
    harness: Harness,
) -> None:
    post(harness, "m-1", "Hello?")
    async with serving(harness, *[Reply(status=500) for _ in range(20)], retry=0.05):
        await eventually(lambda: states(harness) == {"m-1": "held"}, timeout=20)
        await asyncio.sleep(0.5)
        assert len(names(harness)) == 5
    (message,) = runs(harness).messages(PROJECT, SESSION)
    assert message.reason == "5 runs failed in a row"
    started = [datetime.strptime(name.split("-")[0], "%Y%m%dT%H%M%S%fZ") for name in names(harness)]
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(started)]
    # Each run starts at least the doubled wait after the one before; a run's own
    # duration, which varies under load, only adds to it.
    assert [gap >= 0.05 * 2**n - 0.01 for n, gap in enumerate(gaps)] == [True] * 4, gaps


async def test_sigterm_stops_serve_and_its_run_ends_interrupted(harness: Harness) -> None:
    token, key = harness.tmp / "worker.token", harness.tmp / "gateway.key"
    token.write_text(TOKEN)
    key.write_text(API_KEY)
    gate = Gate()
    harness.gateway.script = [Reply("never", before=gate)]
    post(harness, "m-1", "Take your time.")
    process = await asyncio.create_subprocess_exec(
        str(Path(sys.executable).parent / "sandbox-science-harness"),
        "serve", "--worker", harness.worker.url, "--token-file", str(token),
        "--state-dir", str(harness.state), "--allow-unisolated",
        "--model-url", harness.gateway.url, "--api-key-file", str(key), "--poll", "0.05",
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        await asyncio.wait_for(gate.entered.wait(), 30)
        lease = runs(harness).lease(PROJECT, SESSION)
        assert lease is not None
        process.send_signal(signal.SIGTERM)
        _, stderr = await asyncio.wait_for(process.communicate(), 20)
    finally:
        gate.release.set()
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()[-500:]
    stopped = [
        e.data for e in runs(harness).events(PROJECT, SESSION, lease.run) if e.kind == "stopped"
    ]
    assert [data["stopped"] for data in stopped] == ["interrupted"]
    assert runs(harness).lease(PROJECT, SESSION) is None
    assert states(harness) == {"m-1": "held"}


async def test_a_killed_service_leaves_a_run_the_next_one_ends_interrupted(
    harness: Harness,
) -> None:
    """RS-12: the process dies without cleaning up; the next service finds its lease."""
    token, key = harness.tmp / "worker.token", harness.tmp / "gateway.key"
    token.write_text(TOKEN)
    key.write_text(API_KEY)
    gate = Gate()
    harness.gateway.script = [Reply("never", before=gate)]
    post(harness, "m-1", "Are you there?")
    process = await asyncio.create_subprocess_exec(
        str(Path(sys.executable).parent / "sandbox-science-harness"),
        "serve", "--worker", harness.worker.url, "--token-file", str(token),
        "--state-dir", str(harness.state), "--allow-unisolated",
        "--model-url", harness.gateway.url, "--api-key-file", str(key), "--poll", "0.05",
        stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        await asyncio.wait_for(gate.entered.wait(), 30)
        lease = runs(harness).lease(PROJECT, SESSION)
        assert lease is not None
    finally:
        process.kill()
        await process.wait()
        gate.release.set()
    # The next service reads leases as expired after half a second without renewal.
    async with serving(harness, Reply("Yes."), lease_seconds=0.5):
        await eventually(lambda: runs(harness).ended(PROJECT, SESSION, lease.run), timeout=10)
        await eventually(lambda: states(harness) == {"m-1": "held"})
        runs(harness).post(PROJECT, SESSION, "m-2", "Hello again.", author="user")
        (record,) = await ended(harness)
    assert record.delivered["messages"] == ["m-1", "m-2"]  # writing again resumed m-1


async def test_a_service_run_holds_the_session_against_the_command_line(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fixture 5 against a real active run."""
    gate = Gate()
    post(harness, "m-1", "Busy?")
    async with serving(harness, Reply("Not any more.", before=gate)):
        await asyncio.wait_for(gate.entered.wait(), 20)
        token, key = harness.tmp / "worker.token", harness.tmp / "gateway.key"
        token.write_text(TOKEN)
        key.write_text(API_KEY)
        argv = [
            "run", "--worker", harness.worker.url, "--token-file", str(token),
            "--project", PROJECT, "--session", SESSION, "--state-dir", str(harness.state),
            "--allow-unisolated", "--model-url", harness.gateway.url, "--api-key-file",
            str(key), "--model", "main", "--run-id", "cli-run", "From the command line.",
        ]  # fmt: skip
        assert await asyncio.to_thread(cli.main, argv) == 1
        assert "has a run in progress" in capsys.readouterr().err
        gate.release.set()
        (record,) = await ended(harness)
    assert record.final_text == "Not any more."
    assert len(harness.gateway.main) == 1


async def test_a_crash_after_the_save_is_acknowledged_from_the_record(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_deliver, original_append = RunState.deliver, RunState.append

    def crash(*args: Any, **kwargs: Any) -> None:
        raise SystemError("the process dies after the record is saved")

    def no_stopped(self: RunState, *args: Any, **kwargs: Any) -> int:
        if args[3] == "stopped":
            raise SystemError("the process died")
        return original_append(self, *args, **kwargs)

    monkeypatch.setattr(RunState, "deliver", crash)
    monkeypatch.setattr(RunState, "append", no_stopped)

    async def write() -> None:
        post(harness, "m-1", "Mid-run question.")

    await harness.run(
        "run-1", [Reply(calls=[call("plan_read")], before=write), Reply("Answered.")], status=1
    )
    monkeypatch.setattr(RunState, "deliver", original_deliver)
    monkeypatch.setattr(RunState, "append", original_append)
    (name,) = names(harness)
    past = RunState(
        ProjectStore(harness.state).control,
        clock=FakeClock(datetime.now(UTC) - timedelta(minutes=5)),
    )
    past.acquire(PROJECT, SESSION, name, "dead-holder")
    async with serving(harness, Reply("A second answer.")):
        await eventually(lambda: runs(harness).ended(PROJECT, SESSION, name), timeout=10)
        await asyncio.sleep(0.3)
    stopped = [e.data for e in runs(harness).events(PROJECT, SESSION, name) if e.kind == "stopped"]
    assert stopped[-1]["record"] == name
    assert stopped[-1]["stopped"] == "answered"
    assert states(harness)["m-1"] == "delivered"
    assert len(names(harness)) == 1  # nothing was left to answer


def test_a_new_message_queues_the_held_ones_again(tmp_path: Path) -> None:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    state = RunState(projects.control)
    state.post("p", "s", "m-1", "first", author="user")
    state.hold("p", "s", ["m-1"])
    state.post("p", "s", "m-2", "second", author="user")
    assert {m.message_id: m.state for m in state.messages("p", "s")} == {
        "m-1": "queued",
        "m-2": "queued",
    }
    projects.close()


async def test_a_service_that_loses_the_session_logs_one_line(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = harness_admit

    async def admit(*args: Any, **kwargs: Any) -> Any:
        admitted = await real(*args, **kwargs)
        # Another service takes the session first.
        runs(harness).acquire(PROJECT, SESSION, "other-run", "other-holder")
        return admitted

    def busy_lines() -> list[logging.LogRecord]:
        return [record for record in caplog.records if "is busy" in record.getMessage()]

    monkeypatch.setattr(service_module, "admit", admit)
    post(harness, "m-1", "Who answers?")
    # The test takes the service's looks itself and waits for the line, not for a time:
    # half a second used to pass before a loaded runner's admission did (lane K, -n 4).
    with caplog.at_level(logging.INFO):
        async with serving(harness, Reply("Not this one."), retry=60, serve=False) as service:
            await service.tick()  # starts the run, which finds the session taken
            await eventually(busy_lines, timeout=60, describe="the busy line")
            await eventually(lambda: all(a.task and a.task.done() for a in service.active.values()))
            for _ in range(3):  # later looks wait out the retry: no second run, no line
                await service.tick()
            assert service.active == {}
    busy = busy_lines()
    assert len(busy) == 1
    assert busy[0].exc_info is None
    assert harness.gateway.main == []
    assert not isinstance(busy[0].msg, ControlError)


class Suspended:
    """The system clock with wall time moved on: a suspend, as a lease sees it."""

    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.offset)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def test_a_run_that_lost_its_session_after_a_suspend_makes_no_effect(
    harness: Harness,
) -> None:
    """RS-5: after a suspend, another holder may take the session; the old run's next
    effect is refused, and it stops."""
    wall, gate = Suspended(), Gate()
    marker = harness.tmp / "after-the-takeover"
    post(harness, "m-1", "Run a command.")
    async with serving(
        harness,
        Reply(calls=[call("shell", script=f"touch {marker}")], before=gate),
        Reply("Done."),
        lease_seconds=4.0,
        clock=wall,
    ):
        await asyncio.wait_for(gate.entered.wait(), 20)
        other = RunState(ProjectStore(harness.state).control, lease_seconds=4.0, clock=wall)
        lease = other.lease(PROJECT, SESSION)
        assert lease is not None
        wall.offset = 30.0  # the machine slept: by wall time the lease expired
        other.acquire(PROJECT, SESSION, "second-run", "second-holder")
        gate.release.set()  # the old run's model call returns, asking for a shell command
        await eventually(lambda: other.ended(PROJECT, SESSION, lease.run), timeout=10)
        stopped = [e.data for e in other.events(PROJECT, SESSION, lease.run) if e.kind == "stopped"]
        other.release(PROJECT, SESSION, "second-holder")
    assert not marker.exists()
    assert [data["stopped"] for data in stopped] == ["error"]  # it stopped at the call
    assert [data["failure"] for data in stopped] == ["harness"]
    assert len(harness.gateway.main) == 1  # and asked its model nothing more


async def test_a_cancel_as_the_run_closes_leaves_its_answer_and_record(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ArtifactPublisher.aclose

    async def slow_close(self: ArtifactPublisher) -> None:
        state = runs(harness)
        lease = state.lease(PROJECT, SESSION)
        if lease is not None:
            state.cancel(PROJECT, SESSION, lease.run)  # the user cancels as the run closes
        await asyncio.sleep(0.3)
        await original(self)

    monkeypatch.setattr(ArtifactPublisher, "aclose", slow_close)
    post(harness, "m-1", "Quick one.")
    async with serving(harness, Reply("Quick answer.")):
        (record,) = await ended(harness)
    assert (record.stopped, record.final_text) == ("answered", "Quick answer.")
    events = runs(harness).events(PROJECT, SESSION, names(harness)[0])
    assert events[-1].data["stopped"] == "answered"
    assert states(harness) == {"m-1": "delivered"}


async def test_a_run_that_stops_for_its_lost_lease_ends_and_backs_off(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = Gate()
    original = RunState.renew
    stalled = {"on": False}

    def renew(self: RunState, project: str, session: str, holder: str) -> None:
        if stalled["on"]:
            raise ControlError("busy", "the store is held")
        original(self, project, session, holder)

    monkeypatch.setattr(RunState, "renew", renew)
    post(harness, "m-1", "Think.")
    async with serving(
        harness, Reply("Thought.", before=gate), lease_seconds=0.8, retry=60
    ) as service:
        await asyncio.wait_for(gate.entered.wait(), 20)
        lease = runs(harness).lease(PROJECT, SESSION)
        assert lease is not None
        stalled["on"] = True
        await eventually(lambda: runs(harness).ended(PROJECT, SESSION, lease.run), timeout=10)
        stalled["on"] = False
        gate.release.set()
        # The stopped event is written before the failure is counted.
        await eventually(lambda: service.failures == {(PROJECT, SESSION): 1})
        assert (PROJECT, SESSION) in service.waiting_until
    stopped = [
        e.data for e in runs(harness).events(PROJECT, SESSION, lease.run) if e.kind == "stopped"
    ]
    assert [data["stopped"] for data in stopped] == ["interrupted"]
    assert states(harness) == {"m-1": "queued"}
    assert names(harness) == []
