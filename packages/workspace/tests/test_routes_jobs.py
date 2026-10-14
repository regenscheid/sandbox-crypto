# SPDX-License-Identifier: MIT
"""Characterization of the background job routes through HTTP only.

``GET …/sessions/{session}/jobs``, ``GET …/jobs/{job}`` and
``POST …/jobs/{job}/cancel``. These pin what the browser sees, so that the E-02
rewrite of the error mapping keeps it: shapes, status codes, no-store, the session
ownership check and the guards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.projects import ProjectStore

from .route_support import (
    ClientFactory,
    FakeWorker,
    ServerFactory,
    assert_no_leak,
    assert_security_headers,
    context_of,
    error_of,
    fake_worker,
    job,
    origin,
    refusing_port,
    workspace,
)

JOBS = "/api/v0/projects/{project}/sessions/{session}/jobs"
ROUTES = ["list", "status", "cancel"]


def jobs_path(project: str = "p1", session: str = "a") -> str:
    return JOBS.format(project=project, session=session)


async def call(client: Any, route: str, path: str | None = None, job_id: str = "exe-1") -> Any:
    """One of the three job routes, as the page sends it."""
    base = path or jobs_path()
    if route == "list":
        return await client.get(base)
    if route == "status":
        return await client.get(f"{base}/{job_id}")
    return await client.post(f"{base}/{job_id}/cancel", headers=origin(client))


async def connected(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    *,
    login: bool = True,
    job_state: bool = False,
) -> tuple[Any, FakeWorker]:
    worker = FakeWorker()
    context = context_of(state_dir)
    worker.jobs["exe-1"] = job(context)
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(
        aiohttp_client, state_dir, static_dir, worker_url=url, login=login, job_state=job_state
    )
    return client, worker


JOB_KEYS = {
    "job_id",
    "state",
    "purpose",
    "expected_progress",
    "elapsed_seconds",
    "exit_code",
    "error",
    "stdout",
    "stderr",
    "written_files",
    "written_files_truncated",
    "platform_notices",
    "cpu_seconds",
    "memory_bytes",
    "logs",
    "worker_restart",
}


def assert_job(value: dict[str, Any]) -> None:
    """The fields of a job that the page's ``BackgroundJob`` reads."""
    assert set(value) >= JOB_KEYS
    assert value["job_id"] == "exe-1"
    assert value["state"] == "running"
    assert value["purpose"] == "lattice search"
    assert value["expected_progress"] == "one line a minute"
    assert value["elapsed_seconds"] >= 0
    assert value["exit_code"] is None
    assert value["stdout"] == "<b>step 3</b>\n"
    assert value["stderr"] == ""
    assert value["cpu_seconds"] == 1.5
    assert value["memory_bytes"] == 4096
    assert value["logs"] == {"stdout": ".jobs/exe-1/stdout.log", "stderr": ".jobs/exe-1/stderr.log"}
    assert isinstance(value["worker_restart"], str)


async def test_listing_has_the_shape_the_page_reads(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    response = await call(client, "list")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content_type == "application/json"
    assert_security_headers(response.headers)
    body = await response.json()
    assert set(body) == {"enabled", "jobs", "truncated", "waits"}
    assert body["enabled"] is True
    assert body["truncated"] is False
    assert body["waits"] == []
    assert len(body["jobs"]) == 1
    assert_job(body["jobs"][0])
    assert body["jobs"][0]["checkin"] is None
    # The worker is asked for the recorded session's own environment.
    context = context_of(state_dir)
    listed = [query for method, path, query in worker.calls if path == "/v1/jobs"]
    assert [
        (query["project_id"], query["session_id"], query["environment_id"]) for query in listed
    ] == [("p1", "a", context.environment_id)]


async def test_listing_shows_the_harness_waits(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    context = context_of(state_dir)
    state = JobState(state_dir)
    state.heartbeat(context, "run-3")
    state.waiting(
        context,
        "run-3",
        {
            "run_id": "run-3",
            "state": "waiting",
            "job_ids": ["exe-1"],
            "watcher": None,
            "checkins": [],
        },
    )
    client, _ = await connected(
        aiohttp_client, aiohttp_server, state_dir, static_dir, job_state=True
    )
    body = await (await call(client, "list")).json()
    assert len(body["waits"]) == 1
    wait = body["waits"][0]
    assert (wait["run_id"], wait["state"], wait["job_ids"]) == ("run-3", "waiting", ["exe-1"])
    assert wait["watcher"] is None
    assert wait["connected"] is True


async def test_listing_shows_only_this_sessions_jobs(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    worker.jobs["exe-2"] = job(context_of(state_dir, session="c"), "exe-2")
    body = await (await call(client, "list")).json()
    assert [found["job_id"] for found in body["jobs"]] == ["exe-1"]
    other = await (await call(client, "list", jobs_path(session="c"))).json()
    assert [found["job_id"] for found in other["jobs"]] == ["exe-2"]


async def test_one_job_has_the_shape_the_page_reads(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    client, _ = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    response = await call(client, "status")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert_job(await response.json())


async def test_cancel_reaches_the_worker_and_answers_the_job(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    response = await call(client, "cancel")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content_type == "application/json"
    assert_security_headers(response.headers)
    body = await response.json()
    assert body["job_id"] == "exe-1"
    assert body["state"] == "cancelled"
    assert body["purpose"] == "lattice search"
    assert body["ended_at"] is not None
    assert worker.cancelled == ["exe-1"]


@pytest.mark.parametrize("route", ROUTES)
async def test_another_sessions_job_is_not_found(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
) -> None:
    # The worker finds jobs by project; the workspace checks the session.
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    worker.jobs.clear()
    worker.jobs["exe-1"] = job(context_of(state_dir, session="c"))
    response = await call(client, route)
    if route == "list":
        assert (await response.json())["jobs"] == []
    else:
        await error_of(response, 404, "not_found")
    assert worker.cancelled == []


@pytest.mark.parametrize("route", ["status", "cancel"])
async def test_a_job_the_worker_does_not_know_is_not_found(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    await error_of(await call(client, route, job_id="exe-9"), 404, "not_found")
    assert worker.cancelled == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    ("project", "session"),
    [("missing", "a"), ("p1", "missing"), ("p2", "a")],
    ids=["unknown-project", "unknown-session", "session-of-another-project"],
)
async def test_unknown_session_is_not_found_without_asking_the_worker(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
    project: str,
    session: str,
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    await error_of(await call(client, route, jobs_path(project, session)), 404, "not_found")
    assert worker.calls == []


@pytest.mark.parametrize("route", ["status", "cancel"])
@pytest.mark.parametrize("job_id", ["-bad", "x" * 129], ids=["invalid", "overlong"])
async def test_invalid_job_id_is_refused_without_asking_the_worker(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
    job_id: str,
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    await error_of(await call(client, route, job_id=job_id), 400, "invalid_request")
    assert worker.calls == []


@pytest.mark.parametrize("route", ROUTES)
async def test_invalid_project_id_is_refused(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    await error_of(await call(client, route, jobs_path(project="-bad")), 400, "invalid_request")
    assert worker.calls == []


@pytest.mark.parametrize("route", ROUTES)
async def test_invalid_session_id_is_not_found(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    response = await call(client, route, jobs_path(session="-bad"))
    # E-02 changes this to 400 invalid_request, as the kernel and plan routes answer
    # for an invalid session ID: the job routes look the ID up without validating it.
    await error_of(response, 404, "not_found")
    assert worker.calls == []


@pytest.mark.parametrize("route", ROUTES)
async def test_without_a_worker_connection_jobs_are_unsupported(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, route: str
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(await call(client, route), 422, "unsupported")


@pytest.mark.parametrize("route", ROUTES)
async def test_unreachable_worker_is_unavailable_and_names_no_address(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, route: str
) -> None:
    with refusing_port() as url:
        client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
        response = await call(client, route)
        await error_of(response, 503, "unavailable")
        assert_no_leak(await response.text(), url.removeprefix("http://"), "127.0.0.1")


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    ("worker_status", "worker_kind", "status", "kind", "forwarded"),
    [
        (400, "invalid_request", 400, "invalid_request", True),
        (403, "policy_denied", 403, "policy_denied", True),
        (404, "not_found", 404, "not_found", True),
        (409, "conflict", 409, "conflict", True),
        (422, "unsupported", 422, "unsupported", True),
        (503, "unavailable", 503, "unavailable", False),
        (500, "internal", 503, "unavailable", False),
    ],
)
async def test_worker_refusals_keep_their_status(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
    worker_status: int,
    worker_kind: str,
    status: int,
    kind: str,
    forwarded: bool,
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    worker.refusal = (worker_status, worker_kind)
    response = await call(client, route)
    error = await error_of(response, status, kind)
    if not forwarded:
        # Unavailability has a fixed message, without the worker's text or address.
        assert error["message"] == "the worker is unavailable"
        assert_no_leak(await response.text())


@pytest.mark.parametrize("route", ROUTES)
async def test_damaged_project_record_is_an_internal_error(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    response = await call(client, route, jobs_path(project="broken"))
    # E-02 changes this to the shared mapping of a damaged record (the kernel, plan
    # and provisioning routes answer 503 for the same record today).
    await error_of(response, 500, "internal")
    assert worker.calls == []


@pytest.mark.parametrize("route", ROUTES)
async def test_job_routes_need_a_login(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
) -> None:
    client, worker = await connected(
        aiohttp_client, aiohttp_server, state_dir, static_dir, login=False
    )
    response = await call(client, route)
    await error_of(response, 401, "policy_denied")
    assert_security_headers(response.headers)
    assert worker.calls == []


@pytest.mark.parametrize("sent", [None, "http://evil.example", "null"])
async def test_cancel_needs_the_pages_own_origin(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    sent: str | None,
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    headers = {} if sent is None else {"Origin": sent}
    response = await client.post(jobs_path() + "/exe-1/cancel", headers=headers)
    assert response.status == 403
    assert_security_headers(response.headers)
    assert worker.calls == []


@pytest.mark.parametrize("route", ROUTES)
async def test_job_routes_refuse_a_foreign_host_name(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
) -> None:
    client, worker = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    headers = {"Host": "rebound.example", "Origin": "http://rebound.example"}
    if route == "list":
        response = await client.get(jobs_path(), headers=headers)
    elif route == "status":
        response = await client.get(jobs_path() + "/exe-1", headers=headers)
    else:
        response = await client.post(jobs_path() + "/exe-1/cancel", headers=headers)
    assert response.status == 403
    assert_security_headers(response.headers)
    assert worker.calls == []


async def test_job_views_do_not_touch_the_project_record(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    record = ProjectStore(state_dir).get("p1")
    client, _ = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    for route in ROUTES:
        assert (await call(client, route)).status == 200
    assert ProjectStore(state_dir).get("p1") == record
