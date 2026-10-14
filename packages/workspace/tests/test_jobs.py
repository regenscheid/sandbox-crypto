# SPDX-License-Identifier: MIT
"""Live job views keep credentials server-side and enforce recorded session ownership."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import (
    Execution,
    ExecutionContext,
    ExecutionState,
    JobDetails,
    JobListing,
    JobStatus,
    StreamOutput,
)
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.workspace.app import create_app

TOKEN = "test-secret-workspace-login-0123456789"
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


class Worker:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context
        self.execution = Execution(
            execution_id="exe-1",
            attempt_id="att-1",
            operation_id="op-1",
            context=context,
            kind="job",
            state=ExecutionState.RUNNING,
            submitted_at=datetime.now(UTC),
            job=JobDetails(
                purpose="lattice search",
                expected_progress="one line a minute",
                timeout_seconds=86400,
                stdout_path=".jobs/exe-1/stdout.log",
                stderr_path=".jobs/exe-1/stderr.log",
            ),
        )
        self.cancelled: list[str] = []
        self.queries: list[ExecutionContext] = []

    async def jobs(
        self, context: ExecutionContext, *, limit: int = 100, after: str | None = None
    ) -> JobListing:
        self.queries.append(context)
        return JobListing(jobs=(self.execution,), truncated=False)

    async def job_status(self, job: str, *, project_id: str) -> JobStatus:
        return JobStatus(
            execution=self.execution,
            stdout=StreamOutput(name="stdout", text="<script>untrusted</script>", total_bytes=26),
        )

    async def cancel(self, job: str, *, project_id: str) -> Execution:
        self.cancelled.append(job)
        return self.execution

    async def close(self) -> None:
        pass


async def create(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, worker: Worker | None
) -> TestClient[Any, Any]:
    app = create_app(
        ProjectStore(state_dir),
        static_dir,
        login_token=TOKEN,
        job_worker=cast(WorkerClient, worker),
        job_state=JobState(state_dir),
    )
    client = await aiohttp_client(app)
    response = await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert response.status == 303
    return client


def context(state_dir: Path) -> ExecutionContext:
    record = ProjectStore(state_dir).get("p1")
    return ExecutionContext(
        project_id="p1", session_id="a", environment_id=record.sessions["a"].environment_id
    )


@pytest.mark.asyncio
async def test_views_jobs_wait_checkins_and_cancels(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = Worker(context(state_dir))
    state = JobState(state_dir)
    state.heartbeat(worker.context, "run-3")
    state.waiting(
        worker.context,
        "run-3",
        {
            "run_id": "run-3",
            "state": "waiting",
            "job_ids": ["exe-1"],
            "watcher": None,
            "checkins": [],
        },
    )
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    response = await client.get("/api/v0/projects/p1/sessions/a/jobs")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = await response.json()
    assert payload["jobs"][0]["purpose"] == "lattice search"
    assert payload["jobs"][0]["stdout"] == "<script>untrusted</script>"
    assert payload["waits"][0]["watcher"] is None
    assert payload["waits"][0]["connected"] is True
    assert worker.queries[0] == worker.context
    response = await client.post(
        "/api/v0/projects/p1/sessions/a/jobs/exe-1/cancel",
        headers={"Origin": str(client.make_url("/")).rstrip("/")},
    )
    assert response.status == 200
    assert worker.cancelled == ["exe-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["", "/exe-1", "/exe-1/cancel"])
async def test_wrong_session_job_cannot_be_viewed_or_cancelled(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, path: str
) -> None:
    worker = Worker(context(state_dir).model_copy(update={"session_id": "b"}))
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    request = client.post if path.endswith("cancel") else client.get
    response = await request(
        "/api/v0/projects/p1/sessions/a/jobs" + path,
        headers={"Origin": str(client.make_url("/")).rstrip("/")},
    )
    assert response.status == 404
    assert worker.cancelled == []


@pytest.mark.asyncio
async def test_no_worker_is_explicit(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await create(aiohttp_client, state_dir, static_dir, None)
    response = await client.get("/api/v0/projects/p1/sessions/a/jobs")
    payload = await response.json()
    assert payload["error"]["kind"] == "unsupported"
    assert "configured worker" in payload["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["", "/exe-1", "/exe-1/cancel"])
async def test_unreachable_worker_is_unavailable_without_its_address(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, path: str
) -> None:
    # Port 9 (discard) on loopback refuses the connection.
    worker = WorkerClient("http://127.0.0.1:9", "test-secret-worker-token")
    app = create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN, job_worker=worker)
    client = await aiohttp_client(app)
    await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    request = client.post if path.endswith("cancel") else client.get
    response = await request(
        "/api/v0/projects/p1/sessions/a/jobs" + path,
        headers={"Origin": str(client.make_url("/")).rstrip("/")},
    )
    assert response.status == 503
    assert response.headers["Cache-Control"] == "no-store"
    text = await response.text()
    assert "127.0.0.1" not in text
    assert (await response.json())["error"]["kind"] == "unavailable"


@pytest.mark.asyncio
async def test_worker_refusals_keep_their_status(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = Worker(context(state_dir))
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    response = await client.get("/api/v0/projects/p1/sessions/a/jobs/" + "x" * 300)
    assert response.status == 400
    assert (await response.json())["error"] == {
        "kind": "invalid_request",
        "message": "invalid job ID",
    }


@pytest.mark.asyncio
async def test_cancellation_requires_login_and_same_origin(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = Worker(context(state_dir))
    app = create_app(
        ProjectStore(state_dir),
        static_dir,
        login_token=TOKEN,
        job_worker=cast(WorkerClient, worker),
    )
    client = await aiohttp_client(app)
    route = "/api/v0/projects/p1/sessions/a/jobs/exe-1/cancel"
    assert (await client.get("/api/v0/projects/p1/sessions/a/jobs")).status == 401
    await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert (await client.post(route)).status == 403
    assert (await client.post(route, headers={"Origin": "https://other.example"})).status == 403
    assert worker.cancelled == []
