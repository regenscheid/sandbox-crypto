# SPDX-License-Identifier: MIT
"""Characterization of the provisioning observation routes through HTTP only.

The environment list, and the worker-backed views of one environment: summary,
operation detail and files, revisions, probes, recorded packages and lockfile
comparisons. These pin what the browser sees, so that the E-01/E-02 rewrite of the
provisioning proxy keeps it: shapes, forwarded selections, status codes, no-store
and the guards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.projects import ProjectStore

from .route_support import (
    OTHER_PIN,
    PIN,
    ClientFactory,
    FakeWorker,
    ServerFactory,
    assert_no_leak,
    assert_security_headers,
    environment_of,
    error_of,
    fake_worker,
    origin,
    refusing_port,
    workspace,
)

ENVIRONMENTS = "/api/v0/projects/{project}/provisioning/environments"
OBSERVATIONS = "/api/v0/projects/{project}/environments/{environment}/provisioning/observations"
DIFF = {
    "before": "1",
    "before_pin": PIN,
    "after": "2",
    "after_pin": OTHER_PIN,
    "offset": "0",
    "limit": "8",
}

# (name, route suffix, browser query, forwarded worker query besides project_id)
VIEWS: list[tuple[str, str, dict[str, str], dict[str, str]]] = [
    ("summary", "", {}, {}),
    ("detail", "/operations/prv-1", {"seq": "3"}, {"seq": "3"}),
    (
        "files",
        "/operations/prv-1/files",
        {"seq": "3", "offset": "8", "limit": "4"},
        {"seq": "3", "offset": "8", "limit": "4"},
    ),
    ("revisions", "/revisions", {}, {"after": "-1", "limit": "8"}),
    (
        "probes",
        "/revisions/2/probes",
        {"pin": PIN},
        {"pin": PIN, "offset": "0", "limit": "8"},
    ),
    (
        "packages",
        "/revisions/1/packages",
        {"pin": PIN, "limit": "2"},
        {"pin": PIN, "offset": "0", "limit": "2"},
    ),
    ("lockfile-diff", "/lockfile-diff", DIFF, DIFF),
]


def observations(environment: str, project: str = "p1") -> str:
    return OBSERVATIONS.format(project=project, environment=environment)


async def connected(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    *,
    login: bool = True,
) -> tuple[Any, FakeWorker, str]:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url, login=login)
    return client, worker, environment_of(state_dir, "p1", "a")


# The environment list


async def test_environment_list_names_the_projects_environments(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    # The list comes from the project record alone, so it needs no worker.
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(ENVIRONMENTS.format(project="p1"))
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content_type == "application/json"
    assert_security_headers(response.headers)
    body = await response.json()
    assert set(body) == {"project_id", "default_environment_id", "environment_ids", "observed_at"}
    record = ProjectStore(state_dir).get("p1")
    assert body["project_id"] == "p1"
    assert body["default_environment_id"] == record.default_environment_id
    assert body["environment_ids"] == sorted(
        {record.default_environment_id, record.sessions["c"].environment_id}
    )
    assert isinstance(body["observed_at"], str)


@pytest.mark.parametrize(
    ("project", "status", "kind"),
    [("missing", 404, "not_found"), ("-bad", 400, "invalid_request")],
)
async def test_environment_list_of_an_unknown_or_invalid_project(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    project: str,
    status: int,
    kind: str,
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(await client.get(ENVIRONMENTS.format(project=project)), status, kind)


async def test_environment_list_takes_no_query(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(ENVIRONMENTS.format(project="p1") + "?environment=x")
    await error_of(response, 400, "invalid_request")


# The worker-backed views


@pytest.mark.parametrize(("name", "suffix", "query", "forwarded"), VIEWS, ids=[v[0] for v in VIEWS])
async def test_view_forwards_the_exact_selection_and_answers_the_workers_record(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    name: str,
    suffix: str,
    query: dict[str, str],
    forwarded: dict[str, str],
) -> None:
    client, worker, environment = await connected(
        aiohttp_client, aiohttp_server, state_dir, static_dir
    )
    response = await client.get(observations(environment) + suffix, params=query)
    assert response.status == 200, await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content_type == "application/json"
    assert_security_headers(response.headers)
    body = await response.json()
    assert (body["project_id"], body["environment_id"]) == ("p1", environment)
    assert isinstance(body["observed_at"], str)
    # The worker is asked for this project and environment, with only the selection.
    assert worker.provisioning_calls() == [
        (
            f"/v1/environments/{environment}/provisioning/observations{suffix}",
            {"project_id": "p1", **forwarded},
        )
    ]
    if name == "summary":
        assert body["current_revision"] == 1
        assert body["queue_total"] == 1
        assert body["operations"][0]["provision_id"] == "prv-1"
        assert body["gate"]["mode"] == "hold"
    elif name == "detail":
        assert body["summary"]["provision_id"] == "prv-1"
        assert body["summary"]["seq"] == 3
        assert body["request"]["conda"] == ["networkx"]
        assert body["next_step"] == "Wait for operator"
    elif name == "files":
        assert (body["provision_id"], body["seq"], body["offset"], body["limit"]) == (
            "prv-1",
            3,
            8,
            4,
        )
        assert body["total"] == 9
        assert body["next_offset"] is None
        assert [row["name"] for row in body["files"]] == ["package-8"]
    elif name == "revisions":
        assert body["after"] == -1
        assert body["revisions"][0]["record_sha256"] == PIN
    elif name == "probes":
        assert (body["revision"], body["record_sha256"]) == (2, PIN)
        assert body["probes"][0]["status"] == "passed"
    elif name == "packages":
        assert body["selection"]["revision"] == 1
        assert body["selection"]["record_sha256"] == PIN
        assert body["limit"] == 2
        assert body["packages"][0]["license"] == {"declared": "BSD-3-Clause", "agpl": False}
    else:
        assert body["before"]["record_sha256"] == PIN
        assert body["after"]["record_sha256"] == OTHER_PIN
        assert (body["status"], body["total"], body["differences"]) == ("complete", 0, [])


async def test_closed_dedicated_environment_stays_readable(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    dedicated = environment_of(state_dir, "p1", "c")
    ProjectStore(state_dir).close_session("p1", "c")
    client, worker, _ = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    response = await client.get(observations(dedicated))
    assert response.status == 200
    assert (await response.json())["environment_id"] == dedicated
    assert len(worker.provisioning_calls()) == 1


@pytest.mark.parametrize(
    ("project", "environment", "status", "kind"),
    [
        ("p1", "unrecorded-env", 404, "not_found"),
        ("missing", None, 404, "not_found"),
        ("p2", None, 404, "not_found"),
        ("-bad", None, 400, "invalid_request"),
        ("p1", "-bad", 400, "invalid_request"),
    ],
    ids=[
        "environment-not-in-project",
        "unknown-project",
        "environment-of-another-project",
        "invalid-project-id",
        "invalid-environment-id",
    ],
)
async def test_unknown_or_invalid_scope_is_refused_without_asking_the_worker(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    project: str,
    environment: str | None,
    status: int,
    kind: str,
) -> None:
    client, worker, own = await connected(aiohttp_client, aiohttp_server, state_dir, static_dir)
    response = await client.get(observations(environment or own, project))
    await error_of(response, status, kind)
    assert worker.calls == []


@pytest.mark.parametrize(
    ("suffix", "query"),
    [
        ("", {"project_id": "p2"}),
        ("", {"wait": "8"}),
        ("/operations/-bad", {"seq": "3"}),
        ("/operations/prv-1/files", {}),
        ("/operations/prv-1/files", {"seq": "03"}),
        ("/operations/prv-1/files", {"seq": "3", "limit": "9"}),
        ("/operations/prv-1/files", {"seq": "3", "limit": "0"}),
        ("/revisions", {"after": "-2"}),
        ("/revisions/2/probes", {}),
        ("/revisions/2/probes", {"pin": PIN.upper()}),
        ("/revisions/x/probes", {"pin": PIN}),
        ("/revisions/1/packages", {"pin": "short"}),
        ("/revisions/1/packages", {"pin": PIN, "offset": "40001"}),
        ("/lockfile-diff", {key: value for key, value in DIFF.items() if key != "after_pin"}),
        ("/lockfile-diff", {**DIFF, "before": "true"}),
    ],
    ids=[
        "browser-project-context",
        "unknown-argument",
        "invalid-operation-id",
        "files-without-seq",
        "seq-leading-zero",
        "limit-over-8",
        "limit-0",
        "after-below-minus-1",
        "probes-without-pin",
        "pin-not-lowercase-hex",
        "revision-not-a-number",
        "short-pin",
        "packages-offset-over-bound",
        "diff-without-after-pin",
        "diff-revision-not-a-number",
    ],
)
async def test_invalid_selection_is_refused_without_asking_the_worker(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    suffix: str,
    query: dict[str, str],
) -> None:
    client, worker, environment = await connected(
        aiohttp_client, aiohttp_server, state_dir, static_dir
    )
    response = await client.get(observations(environment) + suffix, params=query)
    await error_of(response, 400, "invalid_request")
    assert worker.calls == []


async def test_a_repeated_argument_is_refused(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    client, worker, environment = await connected(
        aiohttp_client, aiohttp_server, state_dir, static_dir
    )
    path = observations(environment) + f"/revisions/2/probes?pin={PIN}&pin={OTHER_PIN}"
    await error_of(await client.get(path), 400, "invalid_request")
    assert worker.calls == []


@pytest.mark.parametrize(
    ("worker_status", "worker_kind", "status", "kind"),
    [
        (404, "not_found", 404, "not_found"),
        (409, "conflict", 409, "conflict"),
        (422, "unsupported", 422, "unsupported"),
        (500, "internal", 503, "unavailable"),
        (503, "unavailable", 503, "unavailable"),
    ],
)
@pytest.mark.parametrize(
    ("suffix", "query"),
    [("", {}), ("/revisions/1/packages", {"pin": PIN}), ("/lockfile-diff", DIFF)],
    ids=["summary", "packages", "lockfile-diff"],
)
async def test_worker_refusals_keep_their_meaning_but_not_their_text(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    worker_status: int,
    worker_kind: str,
    status: int,
    kind: str,
    suffix: str,
    query: dict[str, str],
) -> None:
    worker = FakeWorker()
    worker.refusal = (worker_status, worker_kind)
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    environment = environment_of(state_dir, "p1", "a")
    response = await client.get(observations(environment) + suffix, params=query)
    await error_of(response, status, kind)
    # Provisioning errors carry the workspace's own receipt time.
    assert isinstance((await response.json())["observed_at"], str)
    assert_no_leak(await response.text(), url.removeprefix("http://"))


async def test_without_a_worker_connection_views_are_unsupported(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    environment = environment_of(state_dir, "p1", "a")
    await error_of(await client.get(observations(environment)), 422, "unsupported")


@pytest.mark.parametrize(
    ("suffix", "query"),
    [("", {}), ("/revisions/1/packages", {"pin": PIN}), ("/lockfile-diff", DIFF)],
    ids=["summary", "packages", "lockfile-diff"],
)
async def test_unreachable_worker_is_unavailable_and_names_no_address(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    suffix: str,
    query: dict[str, str],
) -> None:
    with refusing_port() as url:
        client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
        environment = environment_of(state_dir, "p1", "a")
        response = await client.get(observations(environment) + suffix, params=query)
        await error_of(response, 503, "unavailable")
        assert_no_leak(await response.text(), url.removeprefix("http://"), "127.0.0.1")


@pytest.mark.parametrize("route", ["environments", "summary"])
async def test_damaged_project_record_is_unavailable_for_provisioning(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
) -> None:
    client, worker, environment = await connected(
        aiohttp_client, aiohttp_server, state_dir, static_dir
    )
    path = (
        ENVIRONMENTS.format(project="broken")
        if route == "environments"
        else observations(environment, "broken")
    )
    response = await client.get(path)
    await error_of(response, 503, "unavailable")
    assert str(state_dir) not in await response.text()
    assert worker.calls == []


# Guards


@pytest.mark.parametrize("route", ["environments", "summary", "lockfile-diff"])
async def test_provisioning_routes_need_a_login(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
) -> None:
    client, worker, environment = await connected(
        aiohttp_client, aiohttp_server, state_dir, static_dir, login=False
    )
    if route == "environments":
        response = await client.get(ENVIRONMENTS.format(project="p1"))
    elif route == "summary":
        response = await client.get(observations(environment))
    else:
        response = await client.get(observations(environment) + "/lockfile-diff", params=DIFF)
    await error_of(response, 401, "policy_denied")
    assert_security_headers(response.headers)
    assert worker.calls == []


@pytest.mark.parametrize("route", ["environments", "summary"])
async def test_provisioning_routes_refuse_a_foreign_host_name(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    route: str,
) -> None:
    client, worker, environment = await connected(
        aiohttp_client, aiohttp_server, state_dir, static_dir
    )
    path = (
        ENVIRONMENTS.format(project="p1") if route == "environments" else observations(environment)
    )
    response = await client.get(path, headers={"Host": "rebound.example"})
    assert response.status == 403
    # Provisioning responses carry no-store, the guard's refusals included.
    assert response.headers["Cache-Control"] == "no-store"
    assert_security_headers(response.headers)
    assert worker.calls == []


async def test_provisioning_views_are_read_only(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    client, worker, environment = await connected(
        aiohttp_client, aiohttp_server, state_dir, static_dir
    )
    assert (await client.post(observations(environment))).status == 403
    assert (await client.post(observations(environment), headers=origin(client))).status == 405
    assert worker.calls == []
