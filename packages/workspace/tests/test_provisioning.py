# SPDX-License-Identifier: MIT
"""Bounded authenticated provisioning views and actual owned cleanup."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.workspace import provisioning, provisioning_client
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
        if self.mode == "scope":
            value["project_id"] = "p2"
        elif self.mode == "environment":
            value["environment_id"] = "another"
        elif self.mode == "naive":
            value["observed_at"] = "2026-10-13T00:00:00"
        elif self.mode == "counter":
            value["current_revision"] = True
        elif self.mode == "seq":
            value["seq"] = 4
        elif self.mode == "offset":
            value["offset"] = 1
        elif self.mode == "limit":
            value["limit"] = 7
        elif self.mode == "after":
            value["after"] = 0
        elif self.mode == "target":
            value["target"] = 3
        elif self.mode == "pin":
            value["record_sha256"] = "d" * 64
        elif self.mode == "operation":
            value["provision_id"] = "prv-another"
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
) -> tuple[TestClient[Any, Any], WorkerClient, str]:
    application = web.Application()
    application.router.add_get("/v1/{tail:.*}", peer.handle)
    server = await aiohttp_server(application)
    worker = WorkerClient(str(server.make_url("/")).rstrip("/"), "test-secret-provisioning-peer")
    client = await aiohttp_client(
        create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN, job_worker=worker)
    )
    if login:
        assert (
            await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
        ).status == 303
    return client, worker, ProjectStore(state_dir).get("p1").default_environment_id


def test_registry_includes_default_and_retained_dedicated_environments(state_dir: Path) -> None:
    store = ProjectStore(state_dir)
    record = store.get("p1")
    store.close_session("p1", "c")
    snapshot = provisioning.project_snapshot(store, "p1")
    try:
        assert set(snapshot.environments) == {
            record.default_environment_id,
            record.sessions["c"].environment_id,
        }
        assert snapshot.record.sessions["c"].closed_at is not None
        fresh = provisioning.project_snapshot(store, "p1")
        try:
            assert snapshot.matches(fresh)
        finally:
            fresh.close()
    finally:
        snapshot.close()


@pytest.mark.parametrize("mode", ["replace", "identical-aba", "in-place", "root"])
def test_retained_descriptor_rejects_registry_aba(state_dir: Path, mode: str) -> None:
    store = ProjectStore(state_dir)
    snapshot = provisioning.project_snapshot(store, "p1")
    path = store.root / "p1.json"
    original = path.read_bytes()
    try:
        if mode == "replace":
            store.set_policy("p1", "sha256:" + "b" * 64)
        elif mode == "identical-aba":
            store.close_session("p1", "a")
            replacement = store.root / "replacement"
            replacement.write_bytes(original)
            os.replace(replacement, path)
        elif mode == "root":
            moved = store.root.with_name("previous-projects")
            store.root.rename(moved)
            store.root.mkdir()
            path.write_bytes(original)
        else:
            value = json.loads(original)
            value["profile"] = "minimal"
            path.write_text(json.dumps(value))
        fresh = provisioning.project_snapshot(store, "p1")
        try:
            assert not snapshot.matches(fresh)
        finally:
            fresh.close()
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    "damage", ["project", "session", "shared", "time", "oversize", "symlink", "fifo", "broken"]
)
def test_registry_refuses_corrupt_oversized_and_special_files(state_dir: Path, damage: str) -> None:
    store = ProjectStore(state_dir)
    path = store.root / "p1.json"
    value = json.loads(path.read_text())
    if damage == "project":
        value["project_id"] = "p2"
    elif damage == "session":
        value["sessions"]["a"]["session_id"] = "c"
    elif damage == "shared":
        value["sessions"]["a"]["environment_id"] = "foreign"
    elif damage == "time":
        value["sessions"]["a"]["opened_at"] = "2026-10-10T00:00:00"
    if damage in {"symlink", "fifo"}:
        path.unlink()
        if damage == "symlink":
            path.symlink_to(store.root / "p2.json")
        else:
            os.mkfifo(path)
    else:
        path.write_text(
            "x" * (1024 * 1024 + 1)
            if damage == "oversize"
            else "{"
            if damage == "broken"
            else json.dumps(value)
        )
    with pytest.raises(ProjectError) as error:
        provisioning.project_snapshot(store, "p1")
    assert error.value.kind is ErrorKind.UNAVAILABLE


@pytest.mark.parametrize("project", ["missing", "../p1", "p1/other", "", "x" * 129])
def test_registry_invalid_and_absent_scope(state_dir: Path, project: str) -> None:
    with pytest.raises(ProjectError) as error:
        provisioning.project_snapshot(ProjectStore(state_dir), project)
    assert error.value.kind is (
        ErrorKind.NOT_FOUND if project == "missing" else ErrorKind.INVALID_REQUEST
    )


def test_registry_environment_limit_refuses_instead_of_clipping(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provisioning, "MAX_ENVIRONMENTS", 1)
    with pytest.raises(ProjectError, match="environment list exceeds"):
        provisioning.project_snapshot(ProjectStore(state_dir), "p1")


def test_registry_growth_is_bounded_before_parsing(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.read
    calls = 0

    def grown(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            with (state_dir / "projects" / "p1.json").open("ab") as output:
                output.write(b" " * (1024 * 1024))
        return original(descriptor, size)

    monkeypatch.setattr(os, "read", grown)
    with pytest.raises(ProjectError, match="changed during read"):
        provisioning.project_snapshot(ProjectStore(state_dir), "p1")
    assert calls <= 17


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
async def test_auth_scope_and_unconfigured_no_fallback(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    peer = Peer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer, login=False
    )
    assert (await client.get(url(environment))).status == 401
    await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert (await client.get(url("foreign"))).status == 404
    assert (await client.get(url(environment).replace("/p1/", "/p2/"))).status == 404
    assert not peer.calls
    client.app[provisioning.PROXY].worker = None
    response = await client.get(url(environment))
    assert response.status == 422
    assert (await response.json())["error"]["kind"] == "unsupported"
    assert not peer.calls


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
        ("scope", ""),
        ("environment", ""),
        ("naive", ""),
        ("counter", ""),
        ("large", ""),
        ("redirect", ""),
        ("seq", "/operations/prv-1/files?seq=3"),
        ("operation", "/operations/prv-1/files?seq=3"),
        ("offset", "/operations/prv-1/files?seq=3"),
        ("limit", "/operations/prv-1/files?seq=3"),
        ("after", "/revisions?after=-1&target=2"),
        ("target", "/revisions?after=-1&target=2"),
        ("pin", f"/revisions/2/probes?pin={PIN}"),
    ],
)
async def test_actual_http_refuses_wrong_scope_selection_or_bounds(
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
    if mode == "deadline":
        monkeypatch.setattr(provisioning_client, "CALL_TIMEOUT", 0.02)
    else:
        monkeypatch.setattr(provisioning_client, "MAX_CHUNKS", 1)
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    try:
        assert (await client.get(url(environment))).status == 503
        assert not client.app[provisioning.PROXY]._tasks
    finally:
        peer.release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["logout", "close-reopen", "same-byte-aba", "policy", "config"])
async def test_delayed_reply_rechecks_original_login_registry_and_worker_binding(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    change: str,
) -> None:
    peer = Peer()
    peer.blocked = True
    client, worker, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    call = asyncio.create_task(client.get(url(environment)))
    await peer.entered.wait()
    store = ProjectStore(state_dir)
    if change == "logout":
        origin = str(client.make_url("/")).rstrip("/")
        assert (await client.post("/api/v0/logout", headers={"Origin": origin})).status == 204
        await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    elif change == "config":
        worker._headers = {"Authorization": "Bearer test-secret-replaced"}
    elif change == "policy":
        store.set_policy("p1", "sha256:" + "c" * 64)
    else:
        path = store.root / "p1.json"
        original = path.read_bytes()
        store.close_session("p1", "a")
        store.open_session("p1", "a", dedicated=False)
        if change == "same-byte-aba":
            replacement = store.root / "replacement"
            replacement.write_bytes(original)
            os.replace(replacement, path)
    peer.release.set()
    assert (await call).status == (401 if change == "logout" else 503)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_actual_file_owner_retains_capacity_and_close_through_repeated_cancel(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = provisioning.project_snapshot
    snapshots: list[provisioning.ProjectSnapshot] = []

    def blocked(store: ProjectStore, project: str) -> provisioning.ProjectSnapshot:
        result = original(store, project)
        snapshots.append(result)
        entered.set()
        release.wait()
        return result

    monkeypatch.setattr(provisioning, "project_snapshot", blocked)
    monkeypatch.setattr(provisioning, "CALL_TIMEOUT", 0.02)
    proxy = provisioning.ProvisionProxy(ProjectStore(state_dir), None)
    read = asyncio.create_task(
        proxy.read("p1", None, provisioning.Selection("environments"), "login", lambda: True)
    )
    while not entered.is_set():
        await asyncio.sleep(0)
    try:
        if cancel:
            read.cancel()
        result = await asyncio.gather(read, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError if cancel else ProjectError)
        assert len(proxy._tasks) == 1
        assert proxy._scopes == {("p1", None)}
        with pytest.raises(ProjectError, match="capacity"):
            await proxy.read(
                "p1", None, provisioning.Selection("environments"), "other", lambda: True
            )
        close = asyncio.create_task(proxy.close())
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        assert not close.done()
        assert not proxy._closed
        assert os.fstat(snapshots[0]._descriptor)
        assert (
            await asyncio.to_thread(lambda: "default executor remains free")
            == "default executor remains free"
        )
        release.set()
        await asyncio.gather(close, return_exceptions=True)
        assert proxy._closed
        assert not proxy._tasks
        assert not proxy._logins
        assert not proxy._scopes
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(snapshots[0]._descriptor)
    finally:
        release.set()
        await proxy.close()


@pytest.mark.asyncio
async def test_browser_disconnect_does_not_release_ongoing_server_request(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    peer = Peer()
    peer.blocked = True
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    call = asyncio.create_task(client.get(url(environment)))
    await peer.entered.wait()
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)
    proxy = client.app[provisioning.PROXY]
    assert len(proxy._tasks) == 1
    peer.release.set()
    for _ in range(100):
        if not proxy._tasks:
            break
        await asyncio.sleep(0.001)
    assert not proxy._tasks
    assert (await client.get(url(environment))).status == 200


@pytest.mark.asyncio
async def test_observer_snapshot_copies_config_and_only_borrows_the_shared_session() -> None:
    async with ClientSession() as session:
        worker = WorkerClient("http://127.0.0.1:1", "test-secret-private-observer", session=session)
        snapshot = worker.observation_transport()
        assert snapshot.session is session
        assert repr(snapshot) == "WorkerObservationTransport()"
        copied = dict(snapshot.headers)
        copied["Authorization"] = "Bearer test-secret-different"
        assert worker.matches_observation_transport(snapshot)
        worker._headers["Authorization"] = "Bearer test-secret-replaced"
        assert snapshot.headers == (("Authorization", "Bearer test-secret-private-observer"),)
        assert not worker.matches_observation_transport(snapshot)
        worker._headers["Authorization"] = "Bearer test-secret-private-observer"
        worker.base_url = "http://127.0.0.1:2"
        assert not worker.matches_observation_transport(snapshot)
        worker.base_url = snapshot.base_url
        await worker.close()
        assert not session.closed  # externally supplied sessions keep their original owner
        assert worker.matches_observation_transport(snapshot)
    assert not worker.matches_observation_transport(snapshot)
    assert worker._session is session  # recheck never creates a replacement transport


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
async def test_admission_has_no_queue_and_owns_all_counts_through_rpc_drain(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = Peer()
    peer.blocked = True
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    store = ProjectStore(state_dir)
    store.open_session("p1", "d", dedicated=True)
    record = store.get("p1")
    other = record.sessions["c"].environment_id
    third = record.sessions["d"].environment_id
    proxy = client.app[provisioning.PROXY]
    wanted = provisioning.Selection("summary")
    calls = [
        asyncio.create_task(proxy.read("p1", env, wanted, "login", lambda: True))
        for env in (environment, other)
    ]
    await peer.entered.wait()
    try:
        with pytest.raises(ProjectError, match="capacity"):
            await proxy.read("p1", third, wanted, "login", lambda: True)
        with pytest.raises(ProjectError, match="capacity"):
            await proxy.read("p1", environment, wanted, "other", lambda: True)
        monkeypatch.setattr(provisioning, "MAX_CALLS", 2)
        with pytest.raises(ProjectError, match="capacity"):
            await proxy.read("p1", third, wanted, "other", lambda: True)
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        await proxy.close()
        assert not proxy._tasks
        assert not proxy._scopes
        assert not proxy._logins
    finally:
        peer.release.set()
        await proxy.close()
        await asyncio.gather(*calls, return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_cancellation_after_proxy_drain_still_closes_later_shared_owners(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = Peer()
    client, worker, _ = await create(aiohttp_client, aiohttp_server, state_dir, static_dir, peer)
    proxy = client.app[provisioning.PROXY]
    order: list[str] = []
    original = proxy.close
    close_worker = worker.close

    async def cancelled_after_drain() -> None:
        await original()
        order.append("proxy-drained")
        raise asyncio.CancelledError

    async def shared_close() -> None:
        assert proxy._closed
        order.append("worker-closed")
        await close_worker()

    monkeypatch.setattr(proxy, "close", cancelled_after_drain)
    monkeypatch.setattr(worker, "close", shared_close)
    with pytest.raises(asyncio.CancelledError):
        await client.app.cleanup()
    assert order == ["proxy-drained", "worker-closed"]
    monkeypatch.setattr(proxy, "close", original)


@pytest.mark.asyncio
async def test_thread_origin_cancellation_finishes_owner_instead_of_spinning(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cancelled(store: ProjectStore, project: str) -> provisioning.ProjectSnapshot:
        raise asyncio.CancelledError

    monkeypatch.setattr(provisioning, "project_snapshot", cancelled)
    proxy = provisioning.ProvisionProxy(ProjectStore(state_dir), None)
    try:
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await proxy.read(
                    "p1", None, provisioning.Selection("environments"), "login", lambda: True
                )
            await proxy.close()
        assert not proxy._tasks
        assert proxy._closed
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_postawait_file_proof_is_owned_through_deadline_cancel_and_close(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = provisioning.ProjectSnapshot.matches
    retained: list[provisioning.ProjectSnapshot] = []

    def blocked(self: provisioning.ProjectSnapshot, other: provisioning.ProjectSnapshot) -> bool:
        retained.extend((self, other))
        entered.set()
        release.wait()
        return original(self, other)

    monkeypatch.setattr(provisioning.ProjectSnapshot, "matches", blocked)
    monkeypatch.setattr(provisioning, "CALL_TIMEOUT", 0.02)
    proxy = provisioning.ProvisionProxy(ProjectStore(state_dir), None)
    call = asyncio.create_task(
        proxy.read("p1", None, provisioning.Selection("environments"), "login", lambda: True)
    )
    while not entered.is_set():
        await asyncio.sleep(0)
    try:
        with pytest.raises(ProjectError, match="timed out"):
            await call
        assert len(proxy._tasks) == 1
        close = asyncio.create_task(proxy.close())
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        assert not close.done()
        assert await asyncio.to_thread(lambda: True)
        assert all(os.fstat(value._descriptor) for value in retained)
        release.set()
        await asyncio.gather(close, return_exceptions=True)
        assert proxy._closed
        for value in retained:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(value._descriptor)
    finally:
        release.set()
        await proxy.close()


@pytest.mark.asyncio
async def test_postawait_proof_io_error_is_unavailable_and_not_a_partial_success(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(self: provisioning.ProjectSnapshot, other: provisioning.ProjectSnapshot) -> bool:
        raise OSError("synthetic private proof failure")

    monkeypatch.setattr(provisioning.ProjectSnapshot, "matches", failed)
    proxy = provisioning.ProvisionProxy(ProjectStore(state_dir), None)
    try:
        with pytest.raises(ProjectError, match="registry proof is unavailable") as error:
            await proxy.read(
                "p1", None, provisioning.Selection("environments"), "login", lambda: True
            )
        assert error.value.kind is ErrorKind.UNAVAILABLE
        assert not proxy._tasks
    finally:
        await proxy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["root", "file", "root-symlink", "file-symlink"])
async def test_canonical_path_replacement_after_fresh_read_invalidates_held_proof(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    store = ProjectStore(state_dir)
    original = provisioning.ProjectSnapshot.matches
    observed: list[bool] = []

    def replaced(self: provisioning.ProjectSnapshot, other: provisioning.ProjectSnapshot) -> bool:
        path = store.root / "p1.json"
        data = path.read_bytes()
        if replacement in {"root", "root-symlink"}:
            previous = store.root.with_name("previous-projects")
            store.root.rename(previous)
            if replacement == "root":
                store.root.mkdir()
                path.write_bytes(data)
            else:
                store.root.symlink_to(previous, target_is_directory=True)
        elif replacement == "file":
            next_file = store.root / "replacement.json"
            next_file.write_bytes(data)
            os.replace(next_file, path)
        else:
            path.unlink()
            path.symlink_to(store.root / "p2.json")
        result = original(self, other)
        observed.append(result)
        return result

    monkeypatch.setattr(provisioning.ProjectSnapshot, "matches", replaced)
    proxy = provisioning.ProvisionProxy(store, None)
    try:
        with pytest.raises(ProjectError) as error:
            await proxy.read(
                "p1", None, provisioning.Selection("environments"), "login", lambda: True
            )
        assert error.value.kind is ErrorKind.UNAVAILABLE
        assert observed in ([], [False])  # nofollow may refuse before returning a boolean
    finally:
        await proxy.close()


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
