# SPDX-License-Identifier: MIT
"""A run whose job's restart waits, against a real worker with the sandbox daemon."""

from __future__ import annotations

import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ExecutionContext, ExecutionState
from sandbox_science.orchestrator.jobs import JobTools
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import eventually
from sandbox_science.worker import sandbox_processes
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.lifecycle import EnvironmentLifecycle
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store
from sandbox_science.worker.timeouts import MANAGER_TIMEOUTS

from .scripted import ScriptedModel

pytestmark = pytest.mark.slow

TOKEN = "test-secret-job-restarts-0123456789abcdef"
CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


class Site:
    """A real worker over the local backend's sandbox daemon, whose environment can be
    held stopped, so that a lost job's next attempt waits."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.stopped = False
        self.store = Store(root / "worker.sqlite3")
        self.manager = ExecutionManager(
            self.store,
            LocalBackend(LocalBackendSettings(python=sys.executable, daemon=True), root),
            Blobs(root / "artifacts"),
            pool_id="test-pool",
            max_artifact_size=1 << 20,
            state_dir=root,
            timeouts=replace(MANAGER_TIMEOUTS, restart_retry=0.05, restart_retry_max=0.2),
            job_restarts_unisolated=True,  # this test's job leaves no process behind
            lifecycle=EnvironmentLifecycle(
                daemon_identity=lambda _environment: ("synthetic-sandbox", "synthetic-id"),
                environment_stopped=lambda _environment: self.stopped,
            ),
        )

    @property
    def workspace(self) -> Path:
        return self.root / "environments" / "env1" / "project" / "sessions" / "s1"

    def kill_daemon(self) -> None:
        """Lose the environment's daemon, found by its command line, and the job with it."""
        for pid in sandbox_processes.process_table():
            try:
                line = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if any(b"daemon" in part for part in line) and str(self.root).encode() in b" ".join(
                line
            ):
                os.kill(pid, signal.SIGKILL)


@pytest_asyncio.fixture
async def site(tmp_path: Path) -> AsyncIterator[tuple[Site, WorkerClient]]:
    value = Site(tmp_path)
    await value.manager.start()
    try:
        server = TestServer(create_app(value.manager, TOKEN))
        await server.start_server()
        try:
            async with WorkerClient(str(server.make_url("/")).rstrip("/"), TOKEN) as client:
                yield value, client
        finally:
            await server.close()
    finally:
        value.stopped = False
        await value.manager.release("env1")
        await value.manager.close()
        value.store.close()


@pytest.mark.asyncio
async def test_a_run_that_ends_while_a_restart_waits_sees_the_loss_and_the_job_waiting(
    site: tuple[Site, WorkerClient], tmp_path: Path
) -> None:
    worker, client = site
    jobs = JobTools(client, CONTEXT, run_id="run-1", state=JobState(tmp_path / "control"))
    started = await jobs.start(
        {
            "script": "touch started-$SANDBOX_JOB_ATTEMPT; sleep 60",
            "purpose": "resumable sweep",
            "expected_progress": "a checkpoint a cell",
            "restart": True,
        },
        "op-start",
    )
    job_id = str(json.loads(started[0])["job_id"])
    await eventually((worker.workspace / "started-1").exists, timeout=20)
    worker.stopped = True  # the next attempt waits: nothing starts the environment
    worker.kill_daemon()
    model = ScriptedModel.from_json(
        [
            {"tool_calls": [{"name": "wait_jobs", "arguments": {"job_ids": [job_id]}}]},
            {"text": "The sweep's attempt was lost; it restarts when the environment runs."},
        ]
    )
    transcript = await run_task(
        model,
        Toolbox(cast(WorkerClient, client), CONTEXT, features=("jobs",), jobs=jobs),
        "wait for the sweep",
        run_id="run-1",
    )
    # The answer stands; the wait said what was lost and that the job waits to restart.
    assert transcript.final_text is not None
    (record,) = transcript.tool_records
    wait = record["wait"]
    assert wait["reason"] == "job_lost"
    [view] = wait["jobs"]
    assert view["state"] == "queued"
    assert view["attempt"] == 2
    assert view["attempts"][0]["state"] == "lost"
    # No execution ended, so the review is given none: only the wait's account of the job.
    assert "executions" not in record
    # The lost attempt's notice reached the run (delivered with the wait's result).
    assert f"{job_id}#1" in transcript.delivered_ids("jobs")
    execution = await client.get(job_id, project_id="p1")
    assert execution.state is ExecutionState.QUEUED
    assert not (worker.workspace / "started-2").exists()
