# SPDX-License-Identifier: MIT
"""The review's observation of a run never ends the run (docs/specs/consistency-review.md,
CR-50): a job read twice in one state is two readings, and an observation the feed refuses
is a gap the review is told of. From the cluster's live test of 2026-11-27, where a main
run that read a job with ``job_status``, waited for it with ``wait_jobs`` and read it again
stopped with a harness failure and no answer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import (
    Execution,
    ExecutionContext,
    ExecutionState,
    JobDetails,
    JobListing,
    JobRequest,
    JobStatus,
    Result,
    StreamOutput,
)
from sandbox_science.orchestrator.consistency.controller import ReviewController, Reviews
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.documents.errors import CONFLICT
from sandbox_science.orchestrator.jobs import JobTools
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock

from .scripted import ScriptedModel
from .test_consistency import context_for, dispatch
from .test_consistency import store as store

JOB = "exe-" + "1" * 24


def record(context: ExecutionContext, state: ExecutionState) -> Execution:
    """The worker's whole record of the job in ``state``: once it has ended, its result
    holds the output and the files the job wrote."""
    now = datetime(2026, 11, 27, 4, 5, tzinfo=UTC)
    ended = state is ExecutionState.SUCCEEDED
    output = "tick 1\ntick 2\njob done\n" if ended else ""
    return Execution(
        execution_id=JOB,
        attempt_id="att-" + "1" * 24,
        operation_id=JOB,
        kind="job",
        context=context,
        state=state,
        submitted_at=now - timedelta(seconds=300),
        started_at=now - timedelta(seconds=299),
        ended_at=now if ended else None,
        result=Result(
            exit_code=0,
            stdout=StreamOutput(name="stdout", text=output, total_bytes=len(output)),
            elapsed_seconds=300.3,
            written_files=(f".jobs/{JOB}/stdout.log",),
        )
        if ended
        else None,
        job=JobDetails(
            purpose="platform test: a five-minute tick loop",
            expected_progress="one tick line every 20 s",
            timeout_seconds=600,
            stdout_path=f".jobs/{JOB}/stdout.log",
            stderr_path=f".jobs/{JOB}/stderr.log",
        ),
    )


class JournalWorker:
    """A worker whose job status is the journal's (``worker/journal.py``): an ended job's
    status carries its execution with the result's output and files left out, which a
    ``get`` of the execution holds. The job ends after its first status read."""

    def __init__(self, context: ExecutionContext) -> None:
        self.context = context
        self.state = ExecutionState.RUNNING
        self.reads = 0

    async def submit(self, request: JobRequest) -> Execution:
        return record(self.context, ExecutionState.QUEUED)

    async def jobs(
        self, context: ExecutionContext, *, limit: int = 100, after: str | None = None
    ) -> JobListing:
        return JobListing(jobs=(record(self.context, self.state),), truncated=False)

    async def job_status(self, job: str, *, project_id: str) -> JobStatus:
        execution = record(self.context, self.state)
        self.reads += 1
        self.state = ExecutionState.SUCCEEDED
        if execution.result is None:
            return JobStatus(execution=execution)
        compact = execution.result.model_copy(update={"stdout": None, "written_files": ()})
        return JobStatus(
            execution=execution.model_copy(update={"result": compact}),
            stdout=execution.result.stdout,
        )

    async def get(self, job: str, *, project_id: str) -> Execution:
        return record(self.context, self.state)


def readings(reviews: ReviewStore) -> list[tuple[str, str]]:
    """The execution items of the session's feed: each one's state and its output."""
    with reviews.documents.control.read() as tx:
        rows = tx.sql.execute(
            "SELECT payload FROM review_feed WHERE kind='execution' ORDER BY seq"
        ).fetchall()
    return [(json.loads(p)["state"], json.loads(p)["stdout"]) for (p,) in rows]


def unobserved(reviews: ReviewStore) -> list[dict[str, Any]]:
    with reviews.documents.control.read() as tx:
        rows = tx.sql.execute(
            "SELECT payload FROM review_feed WHERE source_key LIKE 'unobserved:%' ORDER BY seq"
        ).fetchall()
    return [json.loads(p) for (p,) in rows]


async def run_jobs(
    reviews: ReviewStore, tmp_path: Path, calls: list[tuple[str, dict[str, Any]]]
) -> tuple[Any, ReviewController]:
    """A main run with consistency reviews that makes ``calls`` of the job tools against
    a journal worker, then answers."""
    context = context_for(reviews)
    worker = JournalWorker(context)
    clock = FakeClock(datetime.now(UTC))
    jobs = JobTools(
        cast(WorkerClient, worker),
        context,
        run_id="author-1",
        state=JobState(tmp_path / "jobs", clock=clock),
        poll_seconds=1.0,
        clock=clock,
    )
    controller = ReviewController(reviews, context, run_id="author-1", model=None, alias=None)
    model = ScriptedModel.from_json(
        [{"tool_calls": [{"name": name, "arguments": args}]} for name, args in calls]
        + [{"text": "The job printed its ticks and ended."}]
    )
    transcript = await run_task(
        model,
        Toolbox(
            cast(WorkerClient, worker), context, features=("jobs",), jobs=jobs, reviews=controller
        ),
        "Run the tick loop and report on it.",
        run_id="author-1",
        hooks=[Reviews(controller)],
    )
    await controller.close()
    return transcript, controller


READ_TWICE = [
    ("job_status", {"job_id": JOB}),
    ("wait_jobs", {"job_ids": [JOB]}),
    ("job_status", {"job_id": JOB}),
]
"""The live run's reads of one job: running, then its end from a wait, then its end again."""


@pytest.mark.asyncio
async def test_a_job_read_after_its_wait_ended_keeps_the_run(store, tmp_path: Path) -> None:
    """The live run's sequence: a wait's ended job was its compact status, and a later
    ``job_status`` the whole record, two different readings under one key, the second of
    which the feed refused and the run died of. Both reads now give the whole record, which
    is one reading of the ended job, with its output."""
    reviews, _, _ = store
    transcript, _ = await run_jobs(reviews, tmp_path, READ_TWICE)
    assert (transcript.stopped, transcript.final_text) == (
        "answered",
        "The job printed its ticks and ended.",
    )
    assert readings(reviews) == [("running", ""), ("succeeded", "tick 1\ntick 2\njob done\n")]
    assert unobserved(reviews) == []


@pytest.mark.asyncio
async def test_two_readings_of_one_state_are_two_items(store) -> None:
    """A job seen in one state twice, with different content (a status before its output
    was kept, then the whole record), is two items, not a refused second observation."""
    reviews, _, _ = store
    context = context_for(reviews)
    controller = ReviewController(reviews, context, run_id="author-1", model=None, alias=None)
    whole = record(context, ExecutionState.SUCCEEDED)
    assert whole.result is not None
    compact = whole.model_copy(
        update={"result": whole.result.model_copy(update={"stdout": None, "written_files": ()})}
    )
    for execution in (compact, whole, whole):
        await controller.safe_point(
            [], tool={"execution": execution.model_dump(mode="json")}, call=None
        )
    assert readings(reviews) == [("succeeded", ""), ("succeeded", "tick 1\ntick 2\njob done\n")]
    assert unobserved(reviews) == []


@pytest.mark.parametrize("refused", ["transcript", "action", "execution"])
@pytest.mark.asyncio
async def test_an_observation_the_feed_refuses_is_a_gap_not_a_failed_run(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused: str
) -> None:
    """A feed that refuses an observation, as it refuses a key that names different
    evidence, leaves the run answering; the item is a gap the review is given: a marker
    with nothing to read, which names the item and why."""
    reviews, _, _ = store
    original = reviews.observe

    def observe(project: str, session: str, key: str, kind: str, *args: Any, **kw: Any) -> int:
        if kind == refused and not key.startswith("unobserved:"):
            raise DocumentError(CONFLICT, "observation key names different evidence")
        return original(project, session, key, kind, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(reviews, "observe", observe)
    calls = [("start_job", {"script": "sleep 1", "purpose": "p", "expected_progress": "e"})]
    transcript, _ = await run_jobs(reviews, tmp_path, [*calls, ("job_status", {"job_id": JOB})])
    assert transcript.stopped == "answered"
    gaps = unobserved(reviews)
    assert gaps
    assert all(gap["kind"] == refused for gap in gaps)
    assert gaps[0]["omitted"] == "not observed: observation key names different evidence"
    # The review is given the gap as an item it cannot read.
    review = dispatch(reviews, "checkpoint")
    held = [
        i
        for i in reviews.manifest("p", "s", review, limit=50)["evidence"]
        if i["kind"] == "unobserved" and not i["complete"]
    ]
    assert held


@pytest.mark.asyncio
async def test_an_ingestion_that_fails_is_a_gap_and_the_run_goes_on(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documents' ingestion failing at a safe point, or at the run's end, leaves the
    window incomplete and the run answering."""
    reviews, _, _ = store

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk I/O error")

    monkeypatch.setattr(reviews, "ingest_documents", broken)
    transcript, controller = await run_jobs(reviews, tmp_path, [("job_status", {"job_id": JOB})])
    assert transcript.stopped == "answered"
    assert not controller.documents_complete
    assert [gap["omitted"] for gap in unobserved(reviews)] == ["not observed: OSError"]
