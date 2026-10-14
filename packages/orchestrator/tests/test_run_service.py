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
import signal
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.harness import Harness as Core
from sandbox_science.orchestrator.harness import Role, Settings, Stores
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.model import OpenAICompatibleModel, ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.service import RunService, earlier_conversation
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock, eventually

from .loop_harness import API_KEY, PROJECT, SESSION, TOKEN, Harness, Reply, call, start_job
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker


@contextlib.asynccontextmanager
async def serving(harness: Harness, *script: Reply) -> AsyncIterator[RunService]:
    """The run service on the harness's state directory, worker and gateway."""
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
                RunState(projects.control),
            )
            core = Core(
                client,
                Role(model, "main"),
                stores=stores,
                settings=Settings(min_isolation_tier=None),
            )
            service = RunService(core, poll=0.02, retry=0.2)
            task = asyncio.create_task(service.serve())
            try:
                yield service
            finally:
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
    assert first[-1] == {"role": "user", "content": "What is $2+2$?"}
    events = runs(harness).events(PROJECT, SESSION, names(harness)[0])
    assert [e.kind for e in events] == ["started", "user", "model", "assistant", "stopped"]
    assert events[0].data["message_ids"] == ["m-1"]
    assert events[1].data == {"message_id": "m-1", "text": "What is $2+2$?"}


async def test_a_later_message_starts_a_run_with_the_earlier_conversation(
    harness: Harness,
) -> None:
    post(harness, "m-1", "First question.")
    async with serving(
        harness, Reply(calls=[call("plan_read")]), Reply("First answer."), Reply("Second answer.")
    ):
        await ended(harness)
        post(harness, "m-2", "Second question.")
        second = (await ended(harness, 2))[-1]
    assert second.final_text == "Second answer."
    *_, last = harness.gateway.main
    conversation = [m for m in last if m["role"] != "system"]
    # The earlier task and answer, without its tool calls, then the new message.
    assert conversation[:2] == [
        {"role": "user", "content": "First question."},
        {"role": "assistant", "content": "First answer."},
    ]
    assert conversation[-1] == {"role": "user", "content": "Second question."}
    assert not any("tool_calls" in m or m["role"] == "tool" for m in conversation)


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


async def test_a_spent_budget_refuses_the_message(harness: Harness) -> None:
    runs(harness).set_budget(PROJECT, 0)
    post(harness, "m-1", "Expensive question.")
    async with serving(harness, Reply("Never sent.")):
        await eventually(lambda: states(harness) == {"m-1": "refused"})
    (message,) = runs(harness).messages(PROJECT, SESSION)
    assert message.reason == "budget"
    assert harness.gateway.main == []
    assert names(harness) == []


async def test_spending_counts_saved_and_interrupted_runs(harness: Harness) -> None:
    record = await harness.run("run-1", [Reply("Answer.")])
    state = runs(harness)
    since = datetime.now(UTC) - timedelta(hours=1)
    (usage,) = record.usage
    assert usage.input_tokens is not None
    assert usage.output_tokens is not None
    saved = usage.input_tokens + usage.output_tokens
    assert state.spent(PROJECT, since) == saved
    run = "29991231T000000000000Z-interrupted"  # an interrupted run: events, no record
    state.append(
        PROJECT, SESSION, run, "assistant", {"usage": {"input_tokens": 10, "output_tokens": 5}}
    )
    state.append(PROJECT, SESSION, run, "assistant", {"usage": {"characters": 400}})
    assert state.spent(PROJECT, since) == saved + 15 + 100
    assert state.spent(PROJECT, datetime(3000, 1, 1, tzinfo=UTC)) == 0  # out of the window


async def test_the_earlier_conversation_keeps_the_newest_runs_that_fit(harness: Harness) -> None:
    for number in (1, 2, 3):
        await harness.run(f"run-{number}", [Reply(f"Answer {number}.")])
    projects = ProjectStore(harness.state)
    state = runs(harness)
    whole = earlier_conversation(projects, state, PROJECT, SESSION, 10_000)
    assert [m["content"] for m in whole if m["role"] == "assistant"] == [
        "Answer 1.",
        "Answer 2.",
        "Answer 3.",
    ]
    task_and_answer = len(whole[0]["content"]) + len("Answer 1.")
    some = earlier_conversation(projects, state, PROJECT, SESSION, 2 * task_and_answer)
    assert some[0] == {"role": "user", "content": "(1 earlier runs of this session are left out.)"}
    assert [m["content"] for m in some if m["role"] == "assistant"] == ["Answer 2.", "Answer 3."]


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
        str(Path(sys.executable).parent / "sandbox-science-harness"),
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
        _, stderr = await asyncio.wait_for(process.communicate(), 20)
    assert b"serving the sessions of" in stderr
    assert b"stopped: runs in progress end interrupted" in stderr
    assert process.returncode == 0
    assert API_KEY.encode() not in stderr
