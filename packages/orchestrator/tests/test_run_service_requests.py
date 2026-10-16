# SPDX-License-Identifier: MIT
"""Work requests and the run service: a comment the user addresses to a session's main
agent reaches it as a chat message does (LD-22). It starts a run for an idle session
(RS-6) and ends a job wait (RS-7). A request has no held or refused state, so where a
message would be held (RS-9, RS-11, RS-12) the service keeps it from starting a run by
itself, and a spent budget (RS-6a) makes it wait.

Lab 38 found the gap: a comment addressed to the agent waited until a chat message came.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.documents.tool import REQUESTS_TASK
from sandbox_science.orchestrator.model import Message
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.service import earlier_conversation
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock, eventually

from .loop_harness import (
    PROJECT,
    SESSION,
    SYSTEM,
    TOKEN,
    Harness,
    Reply,
    call,
    notice,
    plans,
    shape,
    start_job,
)
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import Gate, ended, names, post, runs, serving, states

COMMENT = r"\(A comment addressed to you on the document [^:\s]+:\)\n"


async def test_a_comment_to_an_idle_session_starts_a_run(harness: Harness) -> None:
    request = harness.work_request()
    async with serving(harness, Reply("Explained.")):
        (record,) = await ended(harness)
    assert (record.task, record.final_text) == (REQUESTS_TASK, "Explained.")
    assert record.delivered["requests"] == [request]
    assert harness.request_states() == {request: "delivered"}
    # The run begins with the request, as its hook offers it: there is no task message.
    (first,) = harness.gateway.main
    assert shape(first) == [SYSTEM, notice("requests", request), plans("no_current_plan", False)]
    events = runs(harness).events(PROJECT, SESSION, names(harness)[0])
    assert events[0].data["message_ids"] == []
    assert "user" not in [e.kind for e in events]
    assert {"source": "requests", "ids": [request]} in [e.data for e in events]
    # A later run sees it as an earlier turn: the comment, as the user's words, in place
    # of the run's stand-in task (RS-10).
    question, answer = _history(harness, 100_000)
    assert re.fullmatch(COMMENT + r"Explain the result\.", str(question["content"]))
    assert answer == {"role": "assistant", "content": "Explained."}
    # Without the documents, the stand-in task is all it can show.
    without = earlier_conversation(
        ProjectStore(harness.state), runs(harness), PROJECT, SESSION, 100_000
    )
    assert without[0] == {"role": "user", "content": REQUESTS_TASK}


async def test_a_comment_that_rode_along_is_kept_with_the_users_messages(
    harness: Harness,
) -> None:
    """A comment the run delivered with a message is the user's words in the earlier
    conversation, in the order written, and gives way last, as the messages do (RS-10)."""
    harness.work_request(body="Also check the bound in section 2.")
    post(harness, "m-1", "And a question.")
    answer = "Both answered. " + "x" * 500
    async with serving(harness, Reply(answer), Reply("Done.")):
        await ended(harness)
        question, kept = _history(harness, 100_000)
        assert re.fullmatch(
            COMMENT + r"Also check the bound in section 2\.\n\nAnd a question\.",
            str(question["content"]),
        )
        assert kept == {"role": "assistant", "content": answer}
        tight, short = _history(harness, len(str(question["content"])) + 200)
        assert tight == question
        assert str(short["content"]).startswith("(The agent's answer here, of 515 characters")
        post(harness, "m-2", "One more.")
        await ended(harness, 2)
    *_, last = harness.gateway.main
    conversation = [m for m in last if m["role"] != "system"]
    assert conversation[:2] == [question, kept]  # the service gives it to the next run


def _history(harness: Harness, budget: int) -> list[Message]:
    documents = harness.documents()
    try:
        return earlier_conversation(
            ProjectStore(harness.state),
            runs(harness),
            PROJECT,
            SESSION,
            budget,
            documents=documents,
        )
    finally:
        documents.close()


async def test_queued_messages_start_the_run_and_it_is_offered_the_requests(
    harness: Harness,
) -> None:
    request = harness.work_request()
    post(harness, "m-1", "And a question.")
    async with serving(harness, Reply("Both answered.")):
        (record,) = await ended(harness)
    assert record.task == "And a question."
    assert (record.delivered["messages"], record.delivered["requests"]) == (["m-1"], [request])


async def test_a_comment_ends_a_job_wait(harness: Harness) -> None:
    waiting = Reply()
    jobs: list[str] = []

    async def wait_on_it() -> None:
        async with WorkerClient(harness.worker.url, TOKEN) as client:
            listing = await client.jobs(harness.context())
        jobs.extend(j.execution_id for j in listing.jobs)
        waiting.calls = [call("wait_jobs", job_ids=jobs)]

    waiting.before = wait_on_it
    post(harness, "m-1", "Run the long job.")
    async with serving(harness, Reply(calls=[start_job()]), waiting, Reply("Answered it.")):
        state = runs(harness)

        def in_wait() -> bool:
            lease = state.lease(PROJECT, SESSION)
            return lease is not None and any(
                e.kind == "waiting" and e.data["job_ids"]
                for e in state.events(PROJECT, SESSION, lease.run)
            )

        await eventually(in_wait, timeout=20)
        request = harness.work_request()
        (record,) = await ended(harness)
    assert record.final_text == "Answered it."
    assert record.delivered["requests"] == [request]
    *_, last = harness.gateway.main
    result, offered = last[-2:]
    assert json.loads(str(result["content"]))["reason"] == "user_message"
    assert shape([offered]) == [notice("requests", request)]
    await harness.end_job()


async def test_a_cancelled_run_leaves_its_requests_to_the_next_run(harness: Harness) -> None:
    gate = Gate()
    request = harness.work_request()
    async with serving(harness, Reply("Never.", before=gate), Reply("Both answered.")):
        await asyncio.wait_for(gate.entered.wait(), 20)
        lease = runs(harness).lease(PROJECT, SESSION)
        assert lease is not None
        runs(harness).cancel(PROJECT, SESSION, lease.run)
        (cancelled,) = await ended(harness)
        gate.release.set()
        await asyncio.sleep(0.3)
        assert len(names(harness)) == 1  # the cancel stands: no run starts by itself
        assert harness.request_states() == {request: "queued"}
        post(harness, "m-1", "Go on.")
        records = await ended(harness, 2)
    assert cancelled.stopped == "cancelled"
    assert records[-1].delivered["requests"] == [request]
    assert states(harness) == {"m-1": "delivered"}


async def test_requests_an_interrupted_run_held_start_no_run(harness: Harness) -> None:
    """RS-12: what a run that died was offered waits for the user, here after a long
    outage, with its lease on the request expired."""
    request = harness.work_request()
    past = datetime.now(UTC) - timedelta(minutes=5)
    dead = RunState(ProjectStore(harness.state).control, clock=FakeClock(past))
    run = "20261014T000000000000Z-crashed"
    dead.acquire(PROJECT, SESSION, run, "a-dead-process")
    documents = DocumentStore(store_path(harness.state))
    try:
        leased = Annotations(documents, clock=past.timestamp).pending(
            PROJECT, SESSION, "crashed", seen=set()
        )
    finally:
        documents.close()
    assert [found["request_id"] for found in leased] == [request]
    async with serving(harness, Reply("Answered both.")) as service:
        await eventually(lambda: runs(harness).ended(PROJECT, SESSION, run), timeout=10)
        await asyncio.sleep(0.3)
        assert harness.gateway.main == []
        assert service.held == {(PROJECT, SESSION): {request}}
        post(harness, "m-1", "Are you there?")
        (record,) = await ended(harness)
    assert record.delivered["requests"] == [request]


async def test_a_reached_cost_limit_makes_a_request_wait(harness: Harness) -> None:
    await harness.run("run-1", [Reply("A priced answer.", cost="0.75")])
    runs(harness).set_cost_limit(PROJECT, 0.5)
    harness.gateway.main.clear()
    request = harness.work_request()
    async with serving(harness, Reply("Never sent.")) as service:
        await eventually(lambda: (PROJECT, SESSION) in service.waiting_until, timeout=10)
        await asyncio.sleep(0.3)
    assert harness.gateway.main == []
    assert len(names(harness)) == 1  # the earlier, priced run alone
    assert harness.request_states() == {request: "queued"}


async def test_a_failing_session_stops_starting_runs_for_its_requests(harness: Harness) -> None:
    request = harness.work_request()
    async with serving(harness, *[Reply(status=500) for _ in range(20)], retry=0.05) as service:
        await eventually(lambda: service.held.get((PROJECT, SESSION)) == {request}, timeout=20)
        await asyncio.sleep(0.5)
        assert len(names(harness)) == 5
    assert harness.request_states() == {request: "queued"}


async def test_a_comment_does_not_reopen_a_closed_session(harness: Harness) -> None:
    request = harness.work_request()
    ProjectStore(harness.state).close_session(PROJECT, SESSION)
    async with serving(harness, Reply("Reopened.")) as service:
        await eventually(lambda: service.held.get((PROJECT, SESSION)) == {request}, timeout=10)
        await asyncio.sleep(0.3)
        assert names(harness) == []
        post(harness, "m-1", "Back to work.")  # a message reopens it, and its run answers both
        (record,) = await ended(harness)
    assert (record.delivered["messages"], record.delivered["requests"]) == (["m-1"], [request])
