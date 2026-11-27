# SPDX-License-Identifier: MIT
"""The job tools' side of restarts: the opt-in, the wake and the notices of a lost attempt."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from sandbox_science.contracts import (
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionState,
    JobAttempt,
    JobStatus,
    Result,
)
from sandbox_science.contracts.worker import MAX_JOB_RESTARTS
from sandbox_science.orchestrator.consistency.executions import execution_projection
from sandbox_science.orchestrator.jobs import LOST_WAKE, RESTART, start_job_spec, summary
from sandbox_science.testing import FakeClock, eventually

from .test_jobs import CONTEXT, POLL, FakeWorker, execution, tools


def lost_attempt(job: str = "exe-1", number: int = 1) -> JobAttempt:
    return JobAttempt(
        attempt=number,
        attempt_id=f"att-{number}",
        started_at=datetime.now(UTC) - timedelta(seconds=9),
        lost_at=datetime.now(UTC) - timedelta(seconds=1),
        reason="the sandbox executor incarnation stopped during this attempt",
        stdout_path=f".jobs/{job}/stdout.log",
        stderr_path=f".jobs/{job}/stderr.log",
    )


def restarted(job: str = "exe-1", *, survived: tuple[str, ...] | None = None) -> Execution:
    """``job`` with its first attempt lost and its second queued, as the worker has it."""
    base = execution(job, ExecutionState.QUEUED)
    assert base.job is not None
    attempt = lost_attempt(job).model_copy(update={"survived": survived})
    details = base.job.model_copy(
        update={
            "restart": True,
            "attempt": 2,
            "attempts": (attempt,),
            "stdout_path": f".jobs/{job}/attempt-2/stdout.log",
            "stderr_path": f".jobs/{job}/attempt-2/stderr.log",
        }
    )
    return base.model_copy(update={"attempt_id": "att-2", "started_at": None, "job": details})


def ended_lost(job: str = "exe-1", *, restart: bool = False) -> Execution:
    base = execution(job, ExecutionState.LOST)
    message = "the sandbox that was deleted, stopped or replaced stopped during this attempt"
    return base.model_copy(
        update={
            "ended_at": datetime.now(UTC),
            "result": Result(error=ErrorInfo(kind=ErrorKind.INTERNAL, message=message)),
            "job": base.job.model_copy(update={"restart": restart}) if base.job else None,
        }
    )


def test_start_job_offers_restart_with_what_opting_in_states() -> None:
    spec = start_job_spec(())
    described = spec.parameters["properties"]["restart"]
    assert described == {"type": "boolean", "description": RESTART}
    assert "safe to run again" in RESTART
    assert "checkpoints" in RESTART
    assert "starts again from the beginning" in RESTART
    assert f"up to {MAX_JOB_RESTARTS} times" in RESTART
    assert "never run again" not in spec.description
    assert "unless it was started with restart" in spec.description


@pytest.mark.asyncio
async def test_start_job_passes_the_opt_in_to_the_worker(tmp_path: Path) -> None:
    worker = FakeWorker()
    jobs = tools(worker, tmp_path)
    await jobs.start(
        {"script": "python sweep.py", "purpose": "sweep", "expected_progress": "a cell a minute"}
        | {"restart": True},
        "op-1",
    )
    assert worker.requested[0].restart is True


def test_the_job_view_lists_a_restarted_jobs_attempts() -> None:
    value = summary(JobStatus(execution=restarted(survived=("ckpt.json",))), datetime.now(UTC))
    assert value["restart"] is True
    assert value["attempt"] == 2
    [attempt] = cast(list[dict[str, object]], value["attempts"])
    assert attempt["state"] == "lost"
    assert attempt["survived"] == ["ckpt.json"]
    assert attempt["logs"] == {
        "stdout": ".jobs/exe-1/stdout.log",
        "stderr": ".jobs/exe-1/stderr.log",
    }
    assert value["logs"] == {
        "stdout": ".jobs/exe-1/attempt-2/stdout.log",
        "stderr": ".jobs/exe-1/attempt-2/stderr.log",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["restarted", "ended_lost"])
async def test_losing_an_attempt_wakes_a_wait_for_all_jobs(tmp_path: Path, loss: str) -> None:
    worker = FakeWorker()
    worker.records["exe-2"] = execution("exe-2")
    clock = FakeClock(datetime.now(UTC))
    jobs = tools(worker, tmp_path, clock)
    waiting = asyncio.create_task(jobs.wait({"job_ids": ["exe-1", "exe-2"]}, "op"))
    await eventually(lambda: clock.sleepers == 1)
    worker.records["exe-1"] = restarted() if loss == "restarted" else ended_lost()
    clock.advance(POLL)
    text, record = await asyncio.wait_for(waiting, 5)
    value = json.loads(text)
    assert value["reason"] == "job_lost"
    assert value["next"] == LOST_WAKE
    states = {job["job_id"]: job["state"] for job in value["jobs"]}
    assert states == {"exe-1": "queued" if loss == "restarted" else "lost", "exe-2": "running"}
    if loss == "ended_lost":
        assert [e["state"] for e in record["executions"]] == ["lost"]
    assert worker.cancelled == []


@pytest.mark.asyncio
async def test_a_wait_begun_after_a_loss_is_not_woken_by_it(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.records["exe-1"] = restarted()
    clock = FakeClock(datetime.now(UTC))
    jobs = tools(worker, tmp_path, clock)
    waiting = asyncio.create_task(jobs.wait({"job_ids": ["exe-1"]}, "op"))
    await eventually(lambda: clock.sleepers == 1)
    clock.advance(POLL)
    await eventually(lambda: clock.sleepers == 1 and clock.elapsed == POLL)
    assert not waiting.done()
    finished = execution(state=ExecutionState.SUCCEEDED)
    assert finished.job is not None
    assert worker.records["exe-1"].job is not None
    worker.records["exe-1"] = finished.model_copy(update={"job": worker.records["exe-1"].job})
    clock.advance(POLL)
    text, _ = await asyncio.wait_for(waiting, 5)
    assert json.loads(text)["reason"] == "ended"


@pytest.mark.asyncio
async def test_a_lost_attempt_is_noticed_once_with_its_restart(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.records["exe-1"] = restarted()
    jobs = tools(worker, tmp_path)
    [notice] = await jobs.notices()
    assert notice["notice_id"] == "exe-1#1"
    assert notice["job_id"] == "exe-1"
    assert notice["state"] == "queued"
    assert "being restarted: attempt 2 of at most" in str(notice["restart"])
    lost = notice["lost_attempt"]
    assert isinstance(lost, dict)
    lost = cast(dict[str, object], lost)
    assert "incarnation" in str(lost["reason"])
    assert str(lost["survived"]).startswith("not known yet")
    assert await jobs.notices() == []
    # Unacknowledged, it comes again to the next run; acknowledged, never.
    again = tools(worker, tmp_path)
    assert [n["notice_id"] for n in await again.notices()] == ["exe-1#1"]
    assert again.state is not None
    again.state.acknowledge(CONTEXT, "saved-run", ["exe-1#1"])
    assert await tools(worker, tmp_path).notices() == []


@pytest.mark.asyncio
async def test_another_live_runs_lost_attempt_is_its_own(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.records["exe-1"] = restarted()
    owner = tools(worker, tmp_path)
    assert owner.state is not None
    owner.state.origin(CONTEXT, "exe-1", "run-owner")
    owner.state.heartbeat(CONTEXT, "run-owner")
    assert owner.state.pending(CONTEXT, "run-1", ["exe-1#1"]) == []
    assert owner.state.pending(CONTEXT, "run-owner", ["exe-1#1"]) == ["exe-1#1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_a_job_that_ended_lost_says_why_it_was_not_restarted(
    tmp_path: Path, restart: bool
) -> None:
    worker = FakeWorker()
    worker.records["exe-1"] = ended_lost(restart=restart)
    [notice] = await tools(worker, tmp_path).notices()
    assert notice["state"] == "lost"
    if restart:
        assert str(notice["restart"]).startswith("not restarted again after 0 restart(s)")
    else:
        assert "was not started with restart" in str(notice["restart"])
    assert str(notice["survived"]).startswith("not checked: files of the session workspace")


def test_the_review_sees_one_execution_with_its_lost_attempts() -> None:
    record = restarted().model_dump(mode="json")
    payload, _ = execution_projection(record)
    assert payload["execution_id"] == "exe-1"
    assert payload["attempt_id"] == "att-2"
    assert payload["attempt"] == 2
    [lost] = cast(list[dict[str, object]], payload["lost_attempts"])
    assert lost["attempt_id"] == "att-1"
    plain, _ = execution_projection(execution().model_dump(mode="json"))
    assert "lost_attempts" not in plain
