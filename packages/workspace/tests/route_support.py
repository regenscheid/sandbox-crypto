# SPDX-License-Identifier: MIT
"""Shared pieces of the route characterization tests (``test_routes_*.py``).

The tests drive the workspace only through HTTP and set up state only through public
store APIs. The worker side is ``FakeWorker``: a small HTTP server that speaks the
worker API, so the workspace's real ``WorkerClient`` talks to it over loopback.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentState,
    Execution,
    ExecutionContext,
    ExecutionState,
    JobDetails,
    JobListing,
    JobResultManifest,
    JobSandboxRelease,
    JobSandboxReleased,
    JobStatus,
    KernelInfo,
    KernelObservation,
    KernelStatusRequest,
    StreamOutput,
)
from sandbox_science.contracts.child_environments import ChildEnvironment, ChildEnvironmentList
from sandbox_science.contracts.worker import KERNEL_OBSERVATION_BUSY
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import fake_worker_app
from sandbox_science.workspace.app import create_app
from sandbox_science.workspace.classifications import ClassificationSource
from sandbox_science.workspace.context import TIMEOUTS, Timeouts
from sandbox_science.workspace.mcp_metadata import AvailabilityClient
from sandbox_science.workspace.mcp_services import ListingSource, ServiceConfig

TOKEN = "test-secret-route-characterization-login-0123456789"
WORKER_TOKEN = "test-secret-route-characterization-worker"
WORKER_SECRET = "test-secret-worker-detail-never-forwarded"
"""Put in every refusal the fake worker sends, to check what the workspace forwards."""
PIN = "a" * 64
PIN_POLICY = "sha256:" + "c" * 64
OTHER_PIN = "b" * 64

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
ServerFactory = Callable[..., Awaitable[TestServer]]
Json = dict[str, Any]

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
"""The security headers every workspace response carries, besides the CSP."""


def assert_security_headers(headers: Any) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert headers[name] == value, name
    policy = headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy


@contextmanager
def refusing_port() -> Iterator[str]:
    """A loopback URL that refuses connections: its port is bound but never listens,
    so nothing else can take it while the test runs."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{held.getsockname()[1]}"


def environment_of(state_dir: Path, project: str, session: str) -> str:
    return ProjectStore(state_dir).get(project).sessions[session].environment_id


def context_of(state_dir: Path, project: str = "p1", session: str = "a") -> ExecutionContext:
    return ExecutionContext(
        project_id=project,
        session_id=session,
        environment_id=environment_of(state_dir, project, session),
    )


def job(context: ExecutionContext, execution_id: str = "exe-1") -> Execution:
    started = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    return Execution(
        execution_id=execution_id,
        attempt_id="att-1",
        operation_id=f"op-{execution_id}",
        context=context,
        kind="job",
        state=ExecutionState.RUNNING,
        submitted_at=started,
        started_at=started + timedelta(seconds=1),
        job=JobDetails(
            purpose="lattice search",
            expected_progress="one line a minute",
            timeout_seconds=86400,
            stdout_path=f".jobs/{execution_id}/stdout.log",
            stderr_path=f".jobs/{execution_id}/stderr.log",
        ),
    )


class FakeWorker:
    """The worker API's routes these tests reach, answering from fixed records.

    ``refusal`` makes every route answer that worker error instead. Every request
    is recorded in ``calls`` as (method, path, query).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.refusal: tuple[int, str] | None = None
        self.kernels: tuple[KernelInfo, ...] = (
            KernelInfo(
                kernel_id="k-1",
                language="python",
                generation=1,
                start_revision=1,
                current_revision=2,
                revision_status="predates",
            ),
        )
        self.observed: list[ExecutionContext] = []
        self.busy = False
        """Refuse each kernel observation as the worker does while its lane is taken."""
        self.environment: EnvironmentState | None = None
        """The state the worker records for any environment; none: it knows no such one."""
        self.environment_delay = 0.0
        self.jobs: dict[str, Execution] = {}
        self.cancelled: list[str] = []
        self.manifests: dict[str, JobResultManifest] = {}
        self.released: list[tuple[str, str]] = []
        """(job, operation ID) of each early release of a job sandbox that was accepted."""
        self.children: dict[str, ChildEnvironment] = {}
        """Child environments by ID; each is listed and found for its own session only."""

    def application(self) -> web.Application:
        app = fake_worker_app(middlewares=[self._record])
        app.router.add_post("/v1/kernels/observe", self._observe)
        app.router.add_get("/v1/environments/{environment}", self._environment)
        app.router.add_get("/v1/jobs", self._jobs)
        app.router.add_get("/v1/jobs/{job}", self._job)
        app.router.add_post("/v1/executions/{job}/cancel", self._cancel)
        app.router.add_post("/v1/job-sandboxes/{job}/release", self._release)
        app.router.add_get("/v1/child-environments", self._children)
        app.router.add_get("/v1/child-environments/{environment}", self._child)
        app.router.add_get(
            "/v1/environments/{environment}/provisioning/observations{tail:.*}", self._provisioning
        )
        return app

    def provisioning_calls(self) -> list[tuple[str, dict[str, str]]]:
        return [(path, query) for _, path, query in self.calls if "/provisioning/" in path]

    @web.middleware
    async def _record(self, request: web.Request, handler: Any) -> web.StreamResponse:
        assert request.headers["Authorization"] == f"Bearer {WORKER_TOKEN}"
        self.calls.append((request.method, request.path, dict(request.query)))
        if self.refusal is not None:
            status, kind = self.refusal
            return web.json_response(
                {"error": {"kind": kind, "message": f"{WORKER_SECRET} at 10.9.8.7:8100"}},
                status=status,
            )
        return await handler(request)

    def _of_session(self, request: web.Request) -> list[ChildEnvironment]:
        query = request.query
        return [
            child
            for child in self.children.values()
            if (child.project_id, child.session_id, child.session_environment)
            == (query["project_id"], query["session_id"], query["environment_id"])
        ]

    async def _children(self, request: web.Request) -> web.Response:
        listing = ChildEnvironmentList(environments=tuple(self._of_session(request)))
        return web.Response(text=listing.model_dump_json(), content_type="application/json")

    async def _child(self, request: web.Request) -> web.Response:
        wanted = request.match_info["environment"]
        for child in self._of_session(request):
            if child.environment_id == wanted:
                return web.Response(text=child.model_dump_json(), content_type="application/json")
        return web.json_response(
            {"error": {"kind": "not_found", "message": "no such child environment"}}, status=404
        )

    async def _environment(self, request: web.Request) -> web.Response:
        await asyncio.sleep(self.environment_delay)
        if self.environment is None:
            return web.json_response(
                {"error": {"kind": "not_found", "message": "no such environment"}}, status=404
            )
        now = datetime.now(UTC)
        info = EnvironmentInfo(
            environment_id=request.match_info["environment"],
            project_id="p1",
            profile="minimal",
            image="minimal:test",
            sandbox="sandbox-test",
            policy=PIN_POLICY,
            state=self.environment,
            created_at=now,
            updated_at=now,
        )
        return web.Response(text=info.model_dump_json(), content_type="application/json")

    async def _observe(self, request: web.Request) -> web.Response:
        context = KernelStatusRequest.model_validate_json(await request.read()).context
        self.observed.append(context)
        if self.busy:
            return web.json_response(
                {"error": {"kind": "unavailable", "message": KERNEL_OBSERVATION_BUSY}},
                status=503,
            )
        observed = KernelObservation(
            environment_id=context.environment_id,
            session_id=context.session_id,
            current_revision=2,
            kernels=self.kernels,
            observed_at=datetime.now(UTC),
        )
        return web.Response(text=observed.model_dump_json(), content_type="application/json")

    async def _jobs(self, request: web.Request) -> web.Response:
        query = request.query
        listing = JobListing(
            jobs=tuple(
                found
                for found in self.jobs.values()
                if (
                    found.context.project_id,
                    found.context.session_id,
                    found.context.environment_id,
                )
                == (query["project_id"], query["session_id"], query["environment_id"])
            )
        )
        return web.Response(text=listing.model_dump_json(), content_type="application/json")

    def _found(self, request: web.Request) -> Execution | None:
        found = self.jobs.get(request.match_info["job"])
        if found is None or found.context.project_id != request.query.get("project_id"):
            return None
        return found

    async def _job(self, request: web.Request) -> web.Response:
        found = self._found(request)
        if found is None:
            return web.json_response(
                {"error": {"kind": "not_found", "message": "no such job"}}, status=404
            )
        status = JobStatus(
            execution=found,
            stdout=StreamOutput(name="stdout", text="<b>step 3</b>\n", total_bytes=14),
            cpu_seconds=1.5,
            memory_bytes=4096,
            manifest=self.manifests.get(found.execution_id),
        )
        return web.Response(text=status.model_dump_json(), content_type="application/json")

    async def _release(self, request: web.Request) -> web.Response:
        """The worker's early release: only the session's own retained sandbox."""
        body = JobSandboxRelease.model_validate_json(await request.read())
        found = self.jobs.get(request.match_info["job"])
        if found is None or found.context != body.context or found.job is None:
            return web.json_response(
                {"error": {"kind": "not_found", "message": "no such job sandbox"}}, status=404
            )
        sandbox = found.job.sandbox
        if sandbox is None or sandbox.phase != "retained":
            return web.json_response(
                {"error": {"kind": "conflict", "message": "only a retained one can be released"}},
                status=409,
            )
        self.released.append((found.execution_id, body.operation_id))
        released = JobSandboxReleased(execution_id=found.execution_id)
        return web.Response(text=released.model_dump_json(), content_type="application/json")

    async def _cancel(self, request: web.Request) -> web.Response:
        found = self._found(request)
        if found is None:
            return web.json_response(
                {"error": {"kind": "not_found", "message": "no such job"}}, status=404
            )
        self.cancelled.append(found.execution_id)
        ended = found.model_copy(
            update={"state": ExecutionState.CANCELLED, "ended_at": datetime.now(UTC)}
        )
        return web.Response(text=ended.model_dump_json(), content_type="application/json")

    async def _provisioning(self, request: web.Request) -> web.Response:
        tail = request.match_info["tail"]
        value = provisioning_payload(
            request.query["project_id"], request.match_info["environment"], tail, request.query
        )
        return web.json_response(value)


def _operation(session: str) -> Json:
    return {
        "provision_id": "prv-1",
        "operation_id": "op-1",
        "session_id": session,
        "seq": 3,
        "state": "awaiting_permission",
        "outcome": "awaiting_permission",
        "reason": None,
        "stage": None,
        "planned_revision": 1,
        "revision_before": 1,
        "revision_after": None,
        "permission_request": 7,
        "submitted_at": "2026-10-12T09:00:00+00:00",
        "started_at": None,
        "ended_at": None,
        "files_total": 9,
        "changes_total": 0,
        "probes_total": 0,
        "executions_total": 0,
        "notes_total": 0,
    }


def _lockfile(digest: str, packages: int = 2) -> Json:
    return {"sha256": digest, "size": 100, "conda_packages": packages, "pip_distributions": 0}


def _header(digest: str, packages: int = 2) -> Json:
    return {**_lockfile(digest, packages), "python": None, "platform": None, "skipped": 0}


def provisioning_payload(project: str, environment: str, tail: str, query: Any) -> Json:
    """The worker's answer for one provisioning observation route, echoing the selection."""
    value: Json = {
        "project_id": project,
        "environment_id": environment,
        "observed_at": "2026-10-12T09:30:00+00:00",
    }
    offset, limit = int(query.get("offset", "0")), int(query.get("limit", "8"))
    if tail == "":
        value.update(
            {
                "gate": {
                    "generation": 1,
                    "mode": "hold",
                    "operation": "prv-1",
                    "revision": 1,
                    "recorded": True,
                },
                "current_revision": 1,
                "queue_total": 1,
                "operations": [_operation("a")],
            }
        )
    elif tail == "/operations/prv-1":
        value.update(
            {
                "summary": _operation("a"),
                "request": {
                    "operation_id": "op-1",
                    "context": {
                        "project_id": project,
                        "environment_id": environment,
                        "session_id": "a",
                    },
                    "purpose": "Install networkx",
                    "conda": ["networkx"],
                },
                "policy": "sha256:" + "c" * 64,
                "catalog": None,
                "next_step": "Wait for operator",
                "error_present": False,
                "error_kind": None,
            }
        )
    elif tail == "/operations/prv-1/files":
        value.update(
            {
                "provision_id": "prv-1",
                "seq": int(query["seq"]),
                "total": 9,
                "offset": offset,
                "limit": limit,
                "next_offset": offset + limit if offset + limit < 9 else None,
                "files": [
                    {
                        "ecosystem": "conda",
                        "name": f"package-{n}",
                        "version": "1",
                        "file": f"package-{n}-1.conda",
                        "action": "escalate",
                        "license": None,
                    }
                    for n in range(offset, min(offset + limit, 9))
                ],
            }
        )
    elif tail == "/revisions":
        value.update(
            {
                "retained_total": 1,
                "first_retained": 2,
                "target": 2,
                "after": int(query["after"]),
                "limit": limit,
                "next_after": None,
                "revisions": [
                    {
                        "revision": 2,
                        "record_sha256": PIN,
                        "fingerprint": "d" * 32,
                        "managed": True,
                        "changed_by": "att-1",
                        "operation": "prv-1",
                        "recorded_at": "2026-10-12T09:10:00+00:00",
                        "lockfile": None,
                        "commands_total": 0,
                        "omitted_commands": 0,
                        "probes_total": 1,
                    }
                ],
            }
        )
    elif tail == "/revisions/2/probes":
        value.update(
            {
                "revision": 2,
                "record_sha256": query["pin"],
                "total": 1,
                "offset": offset,
                "limit": limit,
                "next_offset": None,
                "probes": [
                    {
                        "probe": "import-networkx",
                        "kind": "functional",
                        "status": "passed",
                        "revision": 2,
                        "recorded_at": "2026-10-12T09:11:00+00:00",
                        "output": "ok",
                    }
                ],
            }
        )
    elif tail == "/revisions/1/packages":
        value.update(
            {
                "selection": {
                    "revision": 1,
                    "record_sha256": query["pin"],
                    "lockfile": _lockfile("e" * 64, 1),
                },
                "header": _header("e" * 64, 1),
                "status": "complete",
                "holds": [],
                "total": 1,
                "offset": offset,
                "limit": limit,
                "next_offset": None,
                "packages": [
                    {
                        "ecosystem": "conda",
                        "name": "networkx",
                        "location": None,
                        "package": {
                            "name": "networkx",
                            "version": "3.4",
                            "build": "0",
                            "license": "BSD-3-Clause",
                        },
                        "license": {"declared": "BSD-3-Clause", "agpl": False},
                    }
                ],
            }
        )
    elif tail == "/lockfile-diff":
        value.update(
            {
                "before": {
                    "revision": int(query["before"]),
                    "record_sha256": query["before_pin"],
                    "lockfile": _lockfile("e" * 64),
                },
                "after": {
                    "revision": int(query["after"]),
                    "record_sha256": query["after_pin"],
                    "lockfile": _lockfile("f" * 64),
                },
                "before_header": _header("e" * 64),
                "after_header": _header("f" * 64),
                "status": "complete",
                "holds": [],
                "header_changes": [],
                "total": 0,
                "added": 0,
                "removed": 0,
                "changed": 0,
                "offset": offset,
                "limit": limit,
                "next_offset": None,
                "differences": [],
            }
        )
    else:
        raise web.HTTPNotFound()
    return value


async def fake_worker(aiohttp_server: ServerFactory, worker: FakeWorker) -> str:
    """Serve ``worker`` on loopback; its base URL."""
    server = await aiohttp_server(worker.application())
    return str(server.make_url("/")).rstrip("/")


async def workspace(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    *,
    worker_url: str | None = None,
    login: bool = True,
    job_state: bool = False,
    timeouts: Timeouts = TIMEOUTS,
    skills_metadata: str | None = None,
    mcp_project: str | None = None,
    mcp_inventory: tuple[ServiceConfig, ...] = (),
    mcp_checks: AvailabilityClient | None = None,
    mcp_listings: ListingSource | None = None,
    mcp_classifications: ClassificationSource | None = None,
) -> TestClient[Any, Any]:
    """The workspace app under test, optionally connected to a worker and logged in."""
    worker = WorkerClient(worker_url, WORKER_TOKEN) if worker_url is not None else None
    client = await aiohttp_client(
        create_app(
            ProjectStore(state_dir, readonly=True),
            static_dir,
            login_token=TOKEN,
            job_worker=worker,
            job_state=JobState(state_dir) if job_state else None,
            timeouts=timeouts,
            skills_metadata=skills_metadata,
            mcp_project=mcp_project,
            mcp_inventory=mcp_inventory,
            mcp_checks=mcp_checks,
            mcp_listings=mcp_listings,
            mcp_classifications=mcp_classifications,
        )
    )
    if login:
        await log_in(client)
    return client


async def log_in(client: TestClient[Any, Any]) -> None:
    response = await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert response.status == 303


def origin(client: TestClient[Any, Any]) -> dict[str, str]:
    """The ``Origin`` header the workspace's own page sends."""
    return {"Origin": str(client.make_url("/")).rstrip("/")}


async def error_of(response: Any, status: int, kind: str) -> Json:
    """Check an API error's status, envelope, kind and no-store; its ``error`` object."""
    assert response.status == status, await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content_type == "application/json"
    body = await response.json()
    assert body["error"]["kind"] == kind
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]
    return body["error"]


def assert_no_leak(text: str, *addresses: str) -> None:
    """An error body names neither the worker's address nor its own refusal text."""
    for address in (WORKER_SECRET, "10.9.8.7", "Cannot connect", *addresses):
        assert address not in text, address
