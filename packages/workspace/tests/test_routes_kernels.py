# SPDX-License-Identifier: MIT
"""Characterization of ``GET …/sessions/{session}/kernels`` through HTTP only.

These pin what the browser sees, so that the E-01/E-02 rewrite of the kernel proxy
keeps it: the observation's shape, the status codes, no-store and the guards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .route_support import (
    ClientFactory,
    FakeWorker,
    ServerFactory,
    assert_no_leak,
    assert_security_headers,
    context_of,
    error_of,
    fake_worker,
    refusing_port,
    workspace,
)

PATH = "/api/v0/projects/{project}/sessions/{session}/kernels"


def kernels(project: str = "p1", session: str = "a") -> str:
    return PATH.format(project=project, session=session)


async def test_observation_has_the_shape_the_page_reads(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    response = await client.get(kernels())
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content_type == "application/json"
    assert_security_headers(response.headers)
    body = await response.json()
    # kernels.ts accepts exactly these keys and no others.
    assert set(body) == {
        "project_id",
        "environment_id",
        "session_id",
        "current_revision",
        "kernels",
        "observed_at",
    }
    context = context_of(state_dir)
    assert body["project_id"] == "p1"
    assert body["session_id"] == "a"
    assert body["environment_id"] == context.environment_id
    assert body["current_revision"] == 2
    assert body["kernels"] == [
        {
            "kernel_id": "k-1",
            "language": "python",
            "generation": 1,
            "start_revision": 1,
            "current_revision": 2,
            "revision_status": "predates",
        }
    ]
    assert body["observed_at"].endswith(("Z", "+00:00"))
    # The environment comes from the project record, never from the browser.
    assert [(c.project_id, c.session_id, c.environment_id) for c in worker.observed] == [
        ("p1", "a", context.environment_id)
    ]


async def test_dedicated_session_is_observed_in_its_own_environment(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    response = await client.get(kernels(session="c"))
    assert response.status == 200
    body = await response.json()
    dedicated = context_of(state_dir, session="c").environment_id
    assert dedicated != context_of(state_dir).environment_id
    assert body["environment_id"] == dedicated
    assert worker.observed[0].environment_id == dedicated


async def test_no_running_kernels_is_an_empty_list(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = FakeWorker()
    worker.kernels = ()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    response = await client.get(kernels())
    assert response.status == 200
    assert (await response.json())["kernels"] == []


@pytest.mark.parametrize(
    ("project", "session"),
    [("missing", "a"), ("p1", "missing"), ("p2", "a"), ("p1", "b")],
    ids=["unknown-project", "unknown-session", "session-of-another-project", "closed-session"],
)
async def test_unknown_or_closed_session_is_not_found_without_asking_the_worker(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    project: str,
    session: str,
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    await error_of(await client.get(kernels(project, session)), 404, "not_found")
    assert worker.calls == []


@pytest.mark.parametrize(
    "path",
    [
        kernels() + "?environment_id=forged",
        kernels(project="-bad"),
        kernels(session="-bad"),
        kernels(session="a" * 129),
    ],
    ids=["query", "invalid-project-id", "invalid-session-id", "overlong-session-id"],
)
async def test_invalid_request_is_refused_without_asking_the_worker(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    path: str,
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    await error_of(await client.get(path), 400, "invalid_request")
    assert worker.calls == []


async def test_without_a_worker_connection_it_is_unsupported(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(await client.get(kernels()), 422, "unsupported")


async def test_unreachable_worker_is_unavailable_and_names_no_address(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    with refusing_port() as url:
        client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
        response = await client.get(kernels())
        await error_of(response, 503, "unavailable")
        assert_no_leak(await response.text(), url.removeprefix("http://"), "127.0.0.1")


@pytest.mark.parametrize(
    ("worker_status", "worker_kind", "status", "kind"),
    [
        (422, "unsupported", 422, "unsupported"),
        (503, "unavailable", 503, "unavailable"),
        (500, "internal", 503, "unavailable"),
        (404, "not_found", 503, "unavailable"),
    ],
)
async def test_worker_refusals_map_to_unsupported_or_unavailable(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    worker_status: int,
    worker_kind: str,
    status: int,
    kind: str,
) -> None:
    worker = FakeWorker()
    worker.refusal = (worker_status, worker_kind)
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    response = await client.get(kernels())
    await error_of(response, status, kind)
    assert_no_leak(await response.text(), url.removeprefix("http://"))


async def test_damaged_project_record_is_unavailable(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    response = await client.get(kernels(project="broken"))
    # E-02 changes this to the shared mapping of a damaged record (the jobs routes
    # answer 500 for the same record today).
    await error_of(response, 503, "unavailable")
    assert_no_leak(await response.text(), str(state_dir))
    assert worker.calls == []


async def test_login_is_required(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url, login=False)
    response = await client.get(kernels())
    await error_of(response, 401, "policy_denied")
    assert_security_headers(response.headers)
    assert worker.calls == []


async def test_logout_ends_access(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    assert (await client.get(kernels())).status == 200
    logout = await client.post(
        "/api/v0/logout", headers={"Origin": str(client.make_url("/")).rstrip("/")}
    )
    assert logout.status == 204
    await error_of(await client.get(kernels()), 401, "policy_denied")


async def test_a_foreign_host_name_is_refused(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    response = await client.get(kernels(), headers={"Host": "rebound.example"})
    assert response.status == 403
    assert_security_headers(response.headers)
    assert worker.calls == []


async def test_the_route_is_read_only(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    # Without the page's Origin, the guard refuses before routing.
    assert (await client.post(kernels())).status == 403
    response = await client.post(
        kernels(), headers={"Origin": str(client.make_url("/")).rstrip("/")}
    )
    assert response.status == 405
    assert worker.calls == []
