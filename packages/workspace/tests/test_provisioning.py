# SPDX-License-Identifier: MIT
"""Provisioning views through the real worker client against a synthetic worker."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.provision_observations import ProvisionObservation
from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.testing import fake_worker_app
from sandbox_science.workspace import provisioning, provisioning_client
from sandbox_science.workspace.api import TIMEOUTS, Timeouts
from sandbox_science.workspace.app import create_app

TOKEN = "test-secret-provisioning-workspace-login-0123456789"
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
ServerFactory = Callable[..., Awaitable[TestServer]]
PIN = "a" * 64


def url(environment: str) -> str:
    return f"/api/v0/projects/p1/environments/{environment}/provisioning/observations"


def summary() -> dict[str, object]:
    return {
        "provision_id": "prv-1",
        "operation_id": "op-1",
        "session_id": "a",
        "seq": 3,
        "state": "awaiting_permission",
        "outcome": "awaiting_permission",
        "reason": None,
        "stage": None,
        "planned_revision": 1,
        "revision_before": 1,
        "revision_after": None,
        "permission_request": 7,
        "submitted_at": datetime.now(UTC).isoformat(),
        "started_at": None,
        "ended_at": None,
        "files_total": 9,
        "changes_total": 0,
        "probes_total": 0,
        "executions_total": 0,
        "notes_total": 0,
    }


class Peer:
    """Synthetic HTTP boundary; this cannot call a worker manager or guest."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.blocked = False
        self.mode = "valid"
        self.status = 200

    async def handle(self, request: web.Request) -> web.StreamResponse:
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-secret-provisioning-peer"
        self.calls.append((request.path, dict(request.query)))
        self.entered.set()
        if self.blocked:
            await self.release.wait()
        if self.status != 200:
            return web.json_response(
                {"error": {"message": "test-secret-never-forward"}}, status=self.status
            )
        if self.mode == "redirect":
            raise web.HTTPTemporaryRedirect("/stolen")
        if self.mode == "large":
            return web.Response(body=b" " * (64 * 1024 + 1), content_type="application/json")
        if self.mode == "chunks":
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(b" ")
            await asyncio.sleep(0.02)
            await response.write(b" ")
            await response.write_eof()
            return response
        if "/operations/" in request.path and request.query.get("seq", "3") != "3":
            return web.json_response({"error": {"kind": "conflict"}}, status=409)
        environment = request.match_info["tail"].split("/")[1]
        value: dict[str, object] = {
            "project_id": "p1",
            "environment_id": environment,
            "observed_at": datetime.now(UTC).isoformat(),
        }
        if request.path.endswith("/files"):
            offset, limit = int(request.query["offset"]), int(request.query["limit"])
            value.update(
                {
                    "provision_id": "prv-1",
                    "seq": 3,
                    "total": 9,
                    "offset": offset,
                    "limit": limit,
                    "next_offset": 8 if offset == 0 else None,
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
        elif "/operations/" in request.path:
            value.update(
                {
                    "summary": summary(),
                    "request": {
                        "operation_id": "op-1",
                        "context": {
                            "project_id": "p1",
                            "environment_id": environment,
                            "session_id": "a",
                        },
                        "purpose": "Synthetic <script> & no permission grant",
                        "conda": ["networkx"],
                    },
                    "policy": "sha256:" + "b" * 64,
                    "catalog": None,
                    "next_step": "Wait for operator",
                    "error_present": False,
                    "error_kind": None,
                }
            )
        elif request.path.endswith("/probes"):
            value.update(
                {
                    "revision": 2,
                    "record_sha256": PIN,
                    "total": 1,
                    "offset": 0,
                    "limit": int(request.query["limit"]),
                    "next_offset": None,
                    "probes": [
                        {
                            "probe": "synthetic",
                            "kind": "functional",
                            "status": "passed",
                            "revision": 2,
                            "recorded_at": datetime.now(UTC).isoformat(),
                            "output": "Synthetic evidence only",
                        }
                    ],
                }
            )
        elif request.path.endswith("/revisions"):
            value.update(
                {
                    "retained_total": 1,
                    "first_retained": 2,
                    "target": 2,
                    "after": int(request.query["after"]),
                    "limit": int(request.query["limit"]),
                    "next_after": None,
                    "revisions": [
                        {
                            "revision": 2,
                            "record_sha256": PIN,
                            "fingerprint": "c" * 32,
                            "managed": True,
                            "changed_by": "att-1",
                            "operation": "prv-1",
                            "recorded_at": datetime.now(UTC).isoformat(),
                            "lockfile": None,
                            "commands_total": 0,
                            "omitted_commands": 0,
                            "probes_total": 1,
                        }
                    ],
                }
            )
        else:
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
                    "operations": [summary()],
                }
            )
        if self.mode == "naive":
            value["observed_at"] = "2026-10-13T00:00:00"
        elif self.mode == "counter":
            value["current_revision"] = True
        elif self.mode == "offset":
            value["offset"] = 1
        elif self.mode == "limit":
            value["limit"] = 7
        if self.mode.startswith("duplicate"):
            raw = json.dumps(value, separators=(",", ":"))
            if self.mode == "duplicate-scope":
                raw = raw.replace('"project_id":"p1"', '"project_id":"p2","project_id":"p1"', 1)
            else:
                prior = '"action":"escalate"'
                assert prior in raw
                first = (
                    '"\\u0061ction":"deny"'
                    if self.mode == "duplicate-escaped"
                    else '"action":"deny"'
                )
                raw = raw.replace(prior, first + "," + prior, 1)
            return web.Response(text=raw, content_type="application/json")
        return web.json_response(value)


async def create(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    peer: Peer,
    *,
    login: bool = True,
    timeouts: Timeouts = TIMEOUTS,
) -> tuple[TestClient[Any, Any], WorkerClient, str]:
    application = fake_worker_app()
    application.router.add_get("/v1/{tail:.*}", peer.handle)
    server = await aiohttp_server(application)
    worker = WorkerClient(str(server.make_url("/")).rstrip("/"), "test-secret-provisioning-peer")
    client = await aiohttp_client(
        create_app(
            ProjectStore(state_dir),
            static_dir,
            login_token=TOKEN,
            job_worker=worker,
            timeouts=timeouts,
        )
    )
    if login:
        assert (
            await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
        ).status == 303
    return client, worker, ProjectStore(state_dir).get("p1").default_environment_id


@pytest.mark.asyncio
async def test_all_read_routes_forward_exact_scoped_pinned_pages(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    peer = Peer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    original = (state_dir / "projects" / "p1.json").read_bytes()
    listing = await client.get(provisioning.ENVIRONMENTS_PATH.replace("{project}", "p1"))
    assert listing.status == 200
    assert environment in (await listing.json())["environment_ids"]
    assert not peer.calls
    base = url(environment)
    paths = (
        base,
        base + "/operations/prv-1?seq=3",
        base + "/operations/prv-1/files?seq=3&offset=0&limit=8",
        base + "/operations/prv-1/files?seq=3&offset=8&limit=8",
        base + "/revisions?after=-1&limit=8&target=2",
        base + f"/revisions/2/probes?pin={PIN}&offset=0&limit=8",
        base + "/operations/prv-1",
    )
    answers: list[dict[str, Any]] = []
    for path in paths:
        response = await client.get(path)
        assert response.status == 200, await response.text()
        assert response.headers["Cache-Control"] == "no-store"
        answer = await response.json()
        assert answer["project_id"] == "p1"
        assert answer["environment_id"] == environment
        assert datetime.fromisoformat(answer["observed_at"]).utcoffset() is not None
        assert "test-secret" not in await response.text()
        answers.append(answer)
    assert len(peer.calls) == 7
    assert "seq" not in peer.calls[-1][1]
    assert answers[-1]["summary"]["seq"] == 3
    assert all(params["project_id"] == "p1" for _, params in peer.calls)
    assert [entry["name"] for page in answers[2:4] for entry in page["files"]] == [
        f"package-{n}" for n in range(9)
    ]
    assert answers[2]["next_offset"] == 8
    assert answers[3]["next_offset"] is None
    assert all(entry["license"] is None for entry in answers[2]["files"])
    assert answers[4]["revisions"][0]["lockfile"] is None
    assert answers[5]["probes"][0]["kind"] == "functional"
    assert (state_dir / "projects" / "p1.json").read_bytes() == original


@pytest.mark.asyncio
async def test_current_detail_is_explicit_and_stale_selection_has_no_fallback(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    peer = Peer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    selected = url(environment) + "/operations/prv-1"
    response = await client.get(selected + "?seq=2")
    assert response.status == 409
    assert len(peer.calls) == 1
    assert peer.calls[-1][1]["seq"] == "2"
    response = await client.get(selected)
    assert response.status == 200
    assert (await response.json())["summary"]["seq"] == 3
    assert len(peer.calls) == 2
    assert "seq" not in peer.calls[-1][1]
    response = await client.get(selected + "/files")
    assert response.status == 400
    assert len(peer.calls) == 2


@pytest.mark.asyncio
async def test_closed_recorded_dedicated_environment_is_history_only(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    peer = Peer()
    client, _, _ = await create(aiohttp_client, aiohttp_server, state_dir, static_dir, peer)
    store = ProjectStore(state_dir)
    store.close_session("p1", "c")
    original = (store.root / "p1.json").read_bytes()
    response = await client.get(url(store.get("p1").sessions["c"].environment_id))
    assert response.status == 200
    assert len(peer.calls) == 1
    assert (store.root / "p1.json").read_bytes() == original
    assert store.get("p1").sessions["c"].closed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "status"),
    [
        ("?environment_id=forged", 400),
        ("?wait=30", 400),
        ("/operations/prv-1?seq=true", 400),
        ("/operations/prv-1?seq=3&seq=3", 400),
        ("/operations/prv-1/files?seq=3&limit=9", 400),
        ("/operations/prv-1/files?seq=3&offset=-1", 400),
        ("/revisions?after=01", 400),
        ("/revisions?target=99999999999999999999", 400),
        ("/revisions/2/probes", 400),
        ("/revisions/2/probes?pin=bad", 400),
        ("/cancel", 404),
    ],
)
async def test_browser_cannot_supply_context_or_unpinned_selection(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    suffix: str,
    status: int,
) -> None:
    peer = Peer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    response = await client.get(url(environment) + suffix)
    assert response.status == status
    assert not peer.calls
    if status == 400:
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_status", "status"), [(404, 404), (409, 409), (422, 422), (401, 503), (500, 503)]
)
async def test_worker_error_kinds_are_dated_safe_and_never_empty(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    worker_status: int,
    status: int,
) -> None:
    peer = Peer()
    peer.status = worker_status
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    response = await client.get(url(environment))
    assert response.status == status
    value = await response.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert datetime.fromisoformat(value["observed_at"]).utcoffset() is not None
    assert "operations" not in value
    assert "test-secret" not in await response.text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "suffix"),
    [
        ("naive", ""),
        ("counter", ""),
        ("large", ""),
        ("redirect", ""),
        ("offset", "/operations/prv-1/files?seq=3"),
        ("limit", "/operations/prv-1/files?seq=3"),
    ],
)
async def test_actual_http_refuses_invalid_or_unbounded_records(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    mode: str,
    suffix: str,
) -> None:
    peer = Peer()
    peer.mode = mode
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    response = await client.get(url(environment) + suffix)
    assert response.status == 503
    assert "operations" not in await response.json()
    assert len(peer.calls) == 1  # no redirect or active-status fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deadline", "chunks"])
async def test_actual_transport_whole_deadline_and_chunk_limit(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    peer = Peer()
    peer.blocked = mode == "deadline"
    peer.mode = mode
    if mode == "chunks":
        monkeypatch.setattr(provisioning_client, "MAX_CHUNKS", 1)
    # A blocked peer never answers, so the worker call's deadline is the only way out.
    client, _, environment = await create(
        aiohttp_client,
        aiohttp_server,
        state_dir,
        static_dir,
        peer,
        timeouts=Timeouts(worker_call=0.02) if mode == "deadline" else TIMEOUTS,
    )
    try:
        assert (await client.get(url(environment))).status == 503
    finally:
        peer.release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["url", "authorization", "headers"])
async def test_observer_config_preflight_refuses_before_creating_a_session(damage: str) -> None:
    worker = WorkerClient("http://127.0.0.1:1", "test-secret-bounded-observer")
    if damage == "url":
        worker.base_url = "x" * 4097
    elif damage == "authorization":
        worker._headers["Authorization"] = "x" * 8193
    else:
        worker._headers["Other"] = "unexpected"
    with pytest.raises(WorkerAPIError, match="configuration is unavailable"):
        worker.observation_transport()
    assert worker._session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["duplicate-scope", "duplicate-action", "duplicate-escaped"])
async def test_raw_duplicate_scope_and_policy_action_refuse_before_normalization(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    mode: str,
) -> None:
    peer = Peer()
    peer.mode = mode
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    response = await client.get(url(environment) + "/operations/prv-1/files?seq=3")
    assert response.status == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert "files" not in await response.json()
    assert len(peer.calls) == 1


@pytest.mark.asyncio
async def test_observer_refuses_a_worker_of_another_protocol_version(
    aiohttp_server: ServerFactory,
) -> None:
    seen: list[str | None] = []

    async def answer(request: web.Request) -> web.Response:
        seen.append(request.headers.get(PROTOCOL_HEADER))
        return web.json_response({}, headers={PROTOCOL_HEADER: str(PROTOCOL_VERSION + 1)})

    application = web.Application()
    application.router.add_get("/v1/{tail:.*}", answer)
    server = await aiohttp_server(application)
    worker = WorkerClient(str(server.make_url("/")).rstrip("/"), "test-secret-protocol-peer")
    try:
        observer = provisioning_client.ProvisionObserver(worker)
        with pytest.raises(WorkerAPIError) as error:
            await observer.get(
                "/v1/environments/env1/provisioning/observations",
                {"project_id": "p1"},
                ProvisionObservation,
            )
        assert error.value.kind is ErrorKind.UNSUPPORTED
        assert "version mismatch" in error.value.message
        assert seen == [str(PROTOCOL_VERSION)]
    finally:
        await worker.close()
