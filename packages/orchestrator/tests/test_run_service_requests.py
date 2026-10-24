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
from datetime import UTC, datetime, timedelta

import pytest

from sandbox_science.orchestrator.context.history import Archive, Entry, body
from sandbox_science.orchestrator.context.history.usage import AnsweredUsage
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.documents.tool import REQUESTS_TASK
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.orchestrator.runstate import RunState
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


async def test_consumed_chat_and_annotation_survive_without_final_record(harness: Harness) -> None:
    request = harness.work_request()
    past = datetime.now(UTC) - timedelta(minutes=5)
    projects = ProjectStore(harness.state)
    documents = harness.documents()
    run = "20261014T000000000000Z-receipt-crash"
    try:
        dead = RunState(projects.control, clock=FakeClock(past))
        dead.acquire(PROJECT, SESSION, run, "a-dead-process")
        dead.post(PROJECT, SESSION, "already-read", "Consumed question", author="user")
        dead.offer(PROJECT, SESSION, ["already-read"], run)
        Annotations(documents, clock=past.timestamp).pending(
            PROJECT, SESSION, "receipt-crash", seen=set()
        )
        Archive(projects.control).append(
            PROJECT,
            SESSION,
            run,
            Entry("assistant", "assistant", "Both incorporated"),
            model_usage=AnsweredUsage(
                ordinal=1, observed_at=datetime.now(UTC), usage=ModelUsage(cost=0)
            ),
            consumed={"messages": {"already-read"}, "requests": {request}},
        )
    finally:
        documents.close()
        projects.close()
    async with serving(harness, Reply("Must not repeat"), serve=False) as service:
        await service.maintenance_tick()
        assert states(harness)["already-read"] == "delivered"
        assert harness.request_states()[request] == "delivered"
        assert not service.held.get((PROJECT, SESSION))
        assert harness.gateway.main == []


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
    # The archive keeps it as the comment it is, naming its request (CM-3).
    projects = ProjectStore(harness.state)
    try:
        kept = Archive(projects.control).messages(PROJECT, SESSION)
    finally:
        projects.close()
    (comment,) = [row for row in kept if row.entry.kind == "comment"]
    assert comment.entry.refs == (request,)


async def test_a_comment_that_rode_along_is_in_the_next_runs_view(
    harness: Harness,
) -> None:
    """A comment the run delivered with a message is in the next run's view, as it was
    sent, before the message it was written before (CM-29)."""
    harness.work_request(body="Also check the bound in section 2.")
    post(harness, "m-1", "And a question.")
    async with serving(harness, Reply("Both answered."), Reply("Done.")):
        await ended(harness)
        post(harness, "m-2", "One more.")
        await ended(harness, 2)
    first, last = harness.gateway.main
    earlier = [m for m in first if m["role"] != "system"]
    conversation = [m for m in last if m["role"] != "system"]
    assert conversation[: len(earlier)] == earlier
    said = [body(str(m["content"])) for m in earlier]
    comment = next(i for i, text in enumerate(said) if "Also check the bound" in text)
    assert said.index("And a question.") > comment


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
    assert json.loads(body(str(result["content"])))["reason"] == "user_message"
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
    assert harness.gateway.main == []  # its first paid call was refused locally (D102)
    assert len(names(harness)) == 2  # the earlier run and the refused one
    assert harness.request_states() == {request: "queued"}


async def test_a_failing_session_stops_starting_runs_for_its_requests(harness: Harness) -> None:
    request = harness.work_request()
    async with serving(harness, *[Reply(status=500) for _ in range(20)], retry=0.05) as service:
        await eventually(lambda: len(names(harness)) == 1, timeout=20)
        await asyncio.sleep(0.5)
        # An unanswered paid intent belongs to the original unfinished work;
        # retrying it as fresh work could silently repeat a charged request.
        assert await service.waiting_work() == ()
        assert len(names(harness)) == 1
        from sandbox_science.orchestrator.continuity.records import WorkRecords

        works = WorkRecords(service.stores.projects.control)
        original = names(harness)[0]
        assert works.get(PROJECT, SESSION, original).state == "paused"
        assert works.request(PROJECT, SESSION, original) is not None
        assert works.get(PROJECT, SESSION, original).state == "paused"
        assert len(names(harness)) == 1
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


@pytest.mark.parametrize("incorporated", [False, True])
async def test_confined_disconnect_consumes_receipts_before_replacement_queue(
    harness: Harness,
    incorporated: bool,
) -> None:
    from typing import Any

    from sandbox_science.orchestrator.context.history.log import RunArchive
    from sandbox_science.orchestrator.loop import Transcript

    request = harness.work_request()
    entered = asyncio.Event()
    original: list[str] = []
    async with serving(harness, serve=False) as service:
        service.runs.post(PROJECT, SESSION, "runtime-input", "One original input", author="user")

        async def lost_runtime(*args: Any, **options: Any) -> Transcript:
            log = options["log"]
            assert isinstance(log, RunArchive)
            original.append(log.run)
            service.runs.offer(PROJECT, SESSION, ["runtime-input"], log.run)
            # A lease is transient. The receipt survives even if its lease was
            # already released when the replacement runtime connects.
            annotations = Annotations(service.stores.documents)
            annotations.pending(PROJECT, SESSION, log.run.partition("-")[2], seen=set())
            if incorporated:
                Archive(service.stores.projects.control).append(
                    PROJECT,
                    SESSION,
                    log.run,
                    Entry("assistant", "assistant", "Original input incorporated"),
                    model_usage=AnsweredUsage(
                        ordinal=1, observed_at=datetime.now(UTC), usage=ModelUsage(cost=0)
                    ),
                    consumed={"messages": {"runtime-input"}, "requests": {request}},
                )
            annotations.release(PROJECT, SESSION, log.run.partition("-")[2])
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("runtime must have disconnected")

        service.harness.loop_runner = lost_runtime
        assert await service.start_selected(PROJECT, SESSION)
        await entered.wait()
        await service.close()
        assert states(harness)["runtime-input"] == ("delivered" if incorporated else "held")
        assert harness.request_states()[request] == ("delivered" if incorporated else "queued")
        assert not service.active
    async with serving(harness, Reply("Must never repeat"), serve=False) as replacement:
        await replacement.maintenance_tick()
        assert await replacement.waiting_work() == ()
        assert not await replacement.start_selected(PROJECT, SESSION)
        assert harness.gateway.main == []
        if incorporated:
            assert runs(harness).messages(PROJECT, SESSION)[0].run == original[0]
        else:
            replacement.runs.post(
                PROJECT, SESSION, "fresh-request", "New explicit work", author="user"
            )
            assert await replacement.waiting_work() == ((PROJECT, SESSION),)
            assert states(harness)["runtime-input"] == "held"
            from sandbox_science.orchestrator.continuity.records import WorkRecords

            resume = WorkRecords(replacement.stores.projects.control).request(
                PROJECT, SESSION, original[0]
            )
            assert resume is not None
            replacement.runs.resume(PROJECT, SESSION)
            await replacement.waiting_work()
            assert states(harness)["runtime-input"] == "queued"
            assert await replacement.start_selected(PROJECT, SESSION, request=resume)

            async def ready() -> tuple[str, ...]:
                return await replacement.resume_next(PROJECT, SESSION, resume)

            selected = await eventually(ready, timeout=10)
            assert selected == (original[0],)
            await replacement.waiting_work()
            assert states(harness)["runtime-input"] == "queued"
            assert await replacement.resume_branch(PROJECT, SESSION, resume, original[0])
            active = replacement.active[PROJECT, SESSION].task
            assert active is not None
            await active
            assert states(harness)["runtime-input"] == "delivered"
            await replacement.close()


async def test_queue_reconciles_consumed_input_when_original_lease_already_disappeared(
    harness: Harness,
) -> None:
    request = harness.work_request()
    projects = ProjectStore(harness.state)
    documents = harness.documents()
    original = "20261017T000000000000Z-run-receipt-before-disconnect"
    try:
        state = RunState(projects.control)
        state.acquire(PROJECT, SESSION, original, "vanished-holder")
        state.post(PROJECT, SESSION, "orphaned-input", "Consumed once", author="user")
        state.offer(PROJECT, SESSION, ["orphaned-input"], original)
        annotations = Annotations(documents)
        annotations.pending(PROJECT, SESSION, original.partition("-")[2], seen=set())
        Archive(projects.control).append(
            PROJECT,
            SESSION,
            original,
            Entry("assistant", "assistant", "Committed before losing its lease"),
            model_usage=AnsweredUsage(
                ordinal=1, observed_at=datetime.now(UTC), usage=ModelUsage(cost=0)
            ),
            consumed={"messages": {"orphaned-input"}, "requests": {request}},
        )
        annotations.release(PROJECT, SESSION, original.partition("-")[2])
        state.release(PROJECT, SESSION, "vanished-holder")
    finally:
        documents.close()
        projects.close()
    async with serving(harness, Reply("Must not dispatch"), serve=False) as replacement:
        assert await replacement.waiting_work() == ()
        assert not await replacement.start_selected(PROJECT, SESSION)
        assert states(harness)["orphaned-input"] == "delivered"
        assert harness.request_states()[request] == "delivered"
        assert harness.gateway.main == []


async def test_annotation_only_receipt_survives_a_released_lease(harness: Harness) -> None:
    from sandbox_science.orchestrator.continuity.records import WorkRecords

    request = harness.work_request()
    projects = ProjectStore(harness.state)
    documents = harness.documents()
    original = "20261017T000000000000Z-run-annotation-receipt"
    try:
        works = WorkRecords(projects.control)
        works.admit(PROJECT, SESSION, original)
        state = RunState(projects.control)
        state.acquire(PROJECT, SESSION, original, "annotation-holder")
        annotations = Annotations(documents)
        assert annotations.pending(PROJECT, SESSION, original.partition("-")[2], seen=set())
        Archive(projects.control).append(
            PROJECT,
            SESSION,
            original,
            Entry("assistant", "assistant", "Annotation incorporated"),
            model_usage=AnsweredUsage(
                ordinal=1, observed_at=datetime.now(UTC), usage=ModelUsage(cost=0)
            ),
            consumed={"requests": {request}},
        )
        annotations.release(PROJECT, SESSION, original.partition("-")[2])
        works.interrupt_run(PROJECT, SESSION, original)
        state.release(PROJECT, SESSION, "annotation-holder")
        # Ordinary queue reads may exclude unfinished offered work. The same-work
        # notice path used by Resume must still reconcile its committed receipt.
        assert annotations.waiting() == []
        assert annotations.pending(PROJECT, SESSION, original.partition("-")[2], seen=set()) == []
        assert harness.request_states()[request] == "delivered"
    finally:
        documents.close()
        projects.close()


async def test_annotation_offer_uses_exact_attempt_when_short_run_id_is_reused(
    harness: Harness,
) -> None:
    from sandbox_science.orchestrator.context.history.collections import Members
    from sandbox_science.orchestrator.continuity.records import WorkRecords

    request = harness.work_request()
    projects = ProjectStore(harness.state)
    documents = harness.documents()
    first = "20261016T000000000000Z-run-reused"
    second = "20261017T000000000000Z-run-reused"
    try:
        works = WorkRecords(projects.control)
        works.admit(PROJECT, SESSION, first)
        works.interrupt_run(PROJECT, SESSION, first)
        works.admit(PROJECT, SESSION, second)
        state = RunState(projects.control)
        state.acquire(PROJECT, SESSION, second, "exact-attempt-holder")
        annotations = Annotations(documents)
        assert annotations.pending(PROJECT, SESSION, "run-reused", seen=set())
        assert request not in Members(
            projects.control, (PROJECT, SESSION, first), "offered:requests"
        )
        assert request in Members(projects.control, (PROJECT, SESSION, second), "offered:requests")
        annotations.release(PROJECT, SESSION, "run-reused")
        state.release(PROJECT, SESSION, "exact-attempt-holder")
    finally:
        documents.close()
        projects.close()


async def test_stopped_annotation_is_not_restarted_by_replacement_service(harness: Harness) -> None:
    from sandbox_science.orchestrator.continuity.records import WorkRecords

    original_request = harness.work_request()
    projects = ProjectStore(harness.state)
    documents = harness.documents()
    original = "20261017T000000000000Z-run-stopped-annotation"
    try:
        works = WorkRecords(projects.control)
        works.admit(PROJECT, SESSION, original)
        state = RunState(projects.control)
        state.acquire(PROJECT, SESSION, original, "stopped-holder")
        annotations = Annotations(documents)
        assert annotations.pending(PROJECT, SESSION, original.partition("-")[2], seen=set())
        annotations.release(PROJECT, SESSION, original.partition("-")[2])
        works.interrupt_run(PROJECT, SESSION, original, stopped=True)
        state.release(PROJECT, SESSION, "stopped-holder")
    finally:
        documents.close()
        projects.close()
    async with serving(harness, Reply("Must not restart stopped work"), serve=False) as replacement:
        assert await replacement.waiting_work() == ()
        assert not await replacement.start_selected(PROJECT, SESSION)
        assert harness.gateway.main == []
        fresh = harness.work_request("fresh-after-stop", "A new independent request")
        waiting = replacement.annotations.waiting()
        assert (PROJECT, SESSION, fresh) in waiting
        assert (PROJECT, SESSION, original_request) not in waiting


async def test_annotation_offer_membership_failure_rolls_back_its_claim(harness: Harness) -> None:
    import sqlite3

    from sandbox_science.orchestrator.context.history.collections import Members
    from sandbox_science.orchestrator.continuity.records import WorkRecords

    request = harness.work_request()
    projects = ProjectStore(harness.state)
    documents = harness.documents()
    run = "20261017T000000000000Z-run-atomic-offer"
    try:
        WorkRecords(projects.control).admit(PROJECT, SESSION, run)
        state = RunState(projects.control)
        state.acquire(PROJECT, SESSION, run, "atomic-offer-holder")
        with projects.control.write() as tx:
            tx.sql.execute(
                "CREATE TRIGGER fail_original_offer BEFORE INSERT ON run_members "
                "WHEN NEW.collection='offered:requests' BEGIN "
                "SELECT RAISE(ABORT,'membership persistence failed'); END"
            )
        annotations = Annotations(documents)
        with pytest.raises(sqlite3.IntegrityError, match="membership persistence failed"):
            annotations.pending(PROJECT, SESSION, "run-atomic-offer", seen=set())
        assert annotations.claimed(PROJECT, SESSION, "run-atomic-offer") == []
        assert request not in Members(projects.control, (PROJECT, SESSION, run), "offered:requests")
        with projects.control.write() as tx:
            tx.sql.execute("DROP TRIGGER fail_original_offer")
        assert annotations.pending(PROJECT, SESSION, "run-atomic-offer", seen=set())
        assert annotations.claimed(PROJECT, SESSION, "run-atomic-offer") == [request]
        assert request in Members(projects.control, (PROJECT, SESSION, run), "offered:requests")
        annotations.release(PROJECT, SESSION, "run-atomic-offer")
        state.release(PROJECT, SESSION, "atomic-offer-holder")
    finally:
        documents.close()
        projects.close()
