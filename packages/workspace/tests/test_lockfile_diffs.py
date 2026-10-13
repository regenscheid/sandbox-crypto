# SPDX-License-Identifier: MIT
"""Exact comparison proxy tests use a synthetic HTTP peer, never worker/blob execution."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiohttp import web

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.lockfile_diffs import LockfileDiffPage
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore
from sandbox_science.workspace import provisioning, provisioning_client

from .test_provisioning import ClientFactory, Peer, ServerFactory, create, url

BEFORE = "a" * 64
AFTER = "b" * 64


def query(**changes: object) -> dict[str, str]:
    value = {
        "before": "1",
        "before_pin": BEFORE,
        "after": "2",
        "after_pin": AFTER,
        "offset": "0",
        "limit": "8",
    }
    value.update({key: str(item) for key, item in changes.items()})
    return value


def selected(environment: str, *, held: bool = False) -> dict[str, object]:
    def ref(digest: str) -> dict[str, object]:
        return {"sha256": digest, "size": 40, "conda_packages": 0, "pip_distributions": 0}

    def header(digest: str) -> dict[str, object]:
        return {**ref(digest), "python": None, "platform": None, "skipped": 0}

    return {
        "project_id": "p1",
        "environment_id": environment,
        "observed_at": datetime.now(UTC).isoformat(),
        "before": {
            "revision": 1,
            "record_sha256": BEFORE,
            "lockfile": None if held else ref("c" * 64),
        },
        "after": {"revision": 2, "record_sha256": AFTER, "lockfile": ref("d" * 64)},
        "before_header": None if held else header("c" * 64),
        "after_header": header("d" * 64),
        "status": "held" if held else "complete",
        "holds": [{"side": "before", "reason": "lockfile_unrecorded"}] if held else [],
        "header_changes": [],
        "total": None if held else 0,
        "added": None if held else 0,
        "removed": None if held else 0,
        "changed": None if held else 0,
        "offset": 0,
        "limit": 8,
        "next_offset": None,
        "differences": [],
    }


class DiffPeer(Peer):
    """No provisioning manager, record parsing, file read or actual worker is called."""

    def __init__(self) -> None:
        super().__init__()
        self.payload: dict[str, object] | None = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if not request.path.endswith("/lockfile-diff"):
            return await super().handle(request)
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
        environment = request.match_info["tail"].split("/")[1]
        value = copy.deepcopy(self.payload) if self.payload is not None else selected(environment)
        if self.mode == "scope":
            value["project_id"] = "p2"
        elif self.mode == "environment":
            value["environment_id"] = "another"
        if self.mode == "duplicate":
            raw = json.dumps(value).replace(
                '"status": "complete"', '"status": "held", "status": "complete"'
            )
            return web.Response(text=raw, content_type="application/json")
        return web.json_response(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [False, True])
async def test_exact_empty_or_held_pair_is_forwarded_without_inventing_packages(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    held: bool,
) -> None:
    peer = DiffPeer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    peer.payload = selected(environment, held=held)
    original = (state_dir / "projects/p1.json").read_bytes()
    response = await client.get(url(environment) + "/lockfile-diff", params=query())
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert await response.json() == LockfileDiffPage.model_validate_json(
        json.dumps(peer.payload), strict=True
    ).model_dump(mode="json")
    assert peer.calls == [
        (
            f"/v1/environments/{environment}/provisioning/observations/lockfile-diff",
            {"project_id": "p1", **query()},
        )
    ]
    assert (state_dir / "projects/p1.json").read_bytes() == original
    if held:
        assert (await response.json())["total"] is None
    else:
        assert (await response.json())["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "before=1",
        "before=1&before_pin=bad&after=2&after_pin=" + AFTER,
        urlencode(query(before="01")),
        urlencode(query(after="-1")),
        urlencode(query(before="true")),
        urlencode(query(before=2**62 + 1)),
        urlencode(query(before_pin=BEFORE.upper())),
        urlencode(query(offset=80001)),
        urlencode(query(limit=9)),
        urlencode(query()) + "&after=2",
        urlencode(query()) + "&project_id=p2",
        urlencode(query()) + "&seq=3",
        urlencode(query()) + "&wait=8",
    ],
)
async def test_pair_query_is_explicit_canonical_and_bounded_before_any_peer_call(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    raw: str,
) -> None:
    peer = DiffPeer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    response = await client.get(url(environment) + "/lockfile-diff" + ("?" + raw if raw else ""))
    assert response.status == 400
    assert response.headers["Cache-Control"] == "no-store"
    assert not peer.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "before-revision",
        "before-pin",
        "after-revision",
        "after-pin",
        "offset",
        "limit",
        "header",
        "held-total",
        "bool",
        "float",
        "scope",
        "environment",
        "duplicate",
        "redirect",
        "large",
    ],
)
async def test_selected_response_coherence_and_raw_bounds_are_not_normalized(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    damage: str,
) -> None:
    peer = DiffPeer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    value = selected(environment, held=damage == "held-total")
    if damage.startswith(("before-", "after-")):
        side, field = damage.split("-")
        entry = value[side]
        assert isinstance(entry, dict)
        entry["revision" if field == "revision" else "record_sha256"] = (
            3 if field == "revision" else "f" * 64
        )
    elif damage == "offset":
        value["offset"] = 1
    elif damage == "limit":
        value["limit"] = 7
    elif damage == "header":
        header = value["before_header"]
        assert isinstance(header, dict)
        header["sha256"] = "f" * 64
    elif damage == "held-total":
        value["total"] = 0
    elif damage == "bool":
        value["added"] = False
    elif damage == "float":
        value["total"] = 0.0
    else:
        peer.mode = damage
    peer.payload = value
    response = await client.get(url(environment) + "/lockfile-diff", params=query())
    assert response.status == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert "differences" not in await response.json()
    assert len(peer.calls) == 1


@pytest.mark.asyncio
async def test_page_cursor_is_returned_actual_not_assumed_eight(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
) -> None:
    peer = DiffPeer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    value = selected(environment)
    # A known missing optional pip license stays unknown; no Conda metadata is borrowed.
    value.update(
        total=2,
        added=2,
        differences=[
            {
                "ecosystem": "pip",
                "name": "example",
                "location": "user",
                "change": "added",
                "before": None,
                "after": {"name": "Example", "version": "1", "location": "user"},
                "changed_fields": ["location", "name", "version"],
                "before_license": None,
                "after_license": {"declared": None, "agpl": None},
            }
        ],
        next_offset=1,
    )
    peer.payload = value
    response = await client.get(url(environment) + "/lockfile-diff", params=query())
    assert response.status == 200
    received = await response.json()
    assert received["next_offset"] == 1
    assert received["differences"][0]["after_license"] == {"declared": None, "agpl": None}
    value.update(offset=1, next_offset=None)
    response = await client.get(url(environment) + "/lockfile-diff", params=query(offset=1))
    assert response.status == 200
    assert peer.calls[-1][1]["offset"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 409, 422, 500])
async def test_selected_worker_error_has_no_pin_adoption_or_empty_fallback(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    status: int,
) -> None:
    peer = DiffPeer()
    peer.status = status
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    response = await client.get(url(environment) + "/lockfile-diff", params=query())
    assert response.status == (status if status != 500 else 503)
    assert len(peer.calls) == 1
    assert peer.calls[0][1] == {"project_id": "p1", **query()}
    assert "test-secret" not in await response.text()
    assert "differences" not in await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["logout", "root", "config"])
async def test_pair_postchecks_original_cookie_path_and_worker_binding(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    change: str,
) -> None:
    peer = DiffPeer()
    peer.blocked = True
    client, worker, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    call = asyncio.create_task(client.get(url(environment) + "/lockfile-diff", params=query()))
    await peer.entered.wait()
    if change == "logout":
        origin = str(client.make_url("/")).rstrip("/")
        assert (await client.post("/api/v0/logout", headers={"Origin": origin})).status == 204
    elif change == "root":
        root = state_dir / "projects"
        root.rename(state_dir / "previous-projects")
        root.mkdir()
        for path in (state_dir / "previous-projects").iterdir():
            if path.suffix == ".json":
                (root / path.name).write_bytes(path.read_bytes())
    else:
        worker.base_url = "http://127.0.0.1:1"
    peer.release.set()
    assert (await call).status == (401 if change == "logout" else 503)


@pytest.mark.asyncio
async def test_comparison_only_budget_preserves_ordinary_transport_timeout(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = DiffPeer()
    peer.blocked = True
    monkeypatch.setattr(provisioning_client, "CALL_TIMEOUT", 0.01)
    monkeypatch.setattr(provisioning_client, "DIFF_TIMEOUT", 1.0)
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    call = asyncio.create_task(client.get(url(environment) + "/lockfile-diff", params=query()))
    await peer.entered.wait()
    await asyncio.sleep(0.03)
    assert not call.done()
    peer.release.set()
    assert (await call).status == 200
    peer.release.clear()
    peer.entered.clear()
    assert (await client.get(url(environment))).status == 503
    peer.release.set()


@pytest.mark.asyncio
async def test_comparison_file_deadline_retains_actual_slot_and_descriptors(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr(provisioning, "DIFF_TIMEOUT", 0.02)
    proxy = provisioning.ProvisionProxy(ProjectStore(state_dir), None)
    environment = ProjectStore(state_dir).get("p1").default_environment_id
    wanted = provisioning.Selection(
        "lockfile-diff", before=1, before_pin=BEFORE, after=2, after_pin=AFTER
    )
    read = asyncio.create_task(proxy.read("p1", environment, wanted, "login", lambda: True))
    while not entered.is_set():
        await asyncio.sleep(0)
    try:
        with pytest.raises(ProjectError, match="timed out"):
            await read
        assert len(proxy._tasks) == 1
        assert proxy._scopes == {("p1", environment)}
        with pytest.raises(ProjectError, match="capacity"):
            await proxy.read("p1", environment, wanted, "other", lambda: True)
        close = asyncio.create_task(proxy.close())
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        assert not close.done()
        assert os.fstat(snapshots[0]._descriptor)
        release.set()
        await asyncio.gather(close, return_exceptions=True)
        assert proxy._closed
        assert not proxy._tasks
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(snapshots[0]._descriptor)
    finally:
        release.set()
        await proxy.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("before", True),
        ("before", 1.0),
        ("before", -1),
        ("before_pin", "A" * 64),
        ("after", False),
        ("after", 2.0),
        ("after_pin", None),
        ("offset", 80001),
        ("limit", True),
    ],
)
def test_constructed_pair_selection_is_rechecked_before_admission(
    field: str, value: object
) -> None:
    wanted = provisioning.Selection(
        "lockfile-diff", before=1, before_pin=BEFORE, after=2, after_pin=AFTER
    )
    object.__setattr__(wanted, field, value)
    with pytest.raises(ProjectError) as error:
        wanted.__post_init__()
    assert error.value.kind is ErrorKind.INVALID_REQUEST


@pytest.mark.asyncio
async def test_comparison_shares_ordinary_environment_lane_and_transport_timeout_drains(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = DiffPeer()
    peer.blocked = True
    monkeypatch.setattr(provisioning_client, "DIFF_TIMEOUT", 0.04)
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    call = asyncio.create_task(client.get(url(environment) + "/lockfile-diff", params=query()))
    await peer.entered.wait()
    assert (await client.get(url(environment))).status == 503
    assert len(peer.calls) == 1
    assert (await call).status == 503
    assert not client.app[provisioning.PROXY]._tasks
    peer.release.set()
    assert (await client.get(url(environment))).status == 200


@pytest.mark.asyncio
async def test_pair_auth_registry_scope_and_no_write_route(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
) -> None:
    peer = DiffPeer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer, login=False
    )
    assert (await client.get(url(environment) + "/lockfile-diff", params=query())).status == 401
    from .test_provisioning import TOKEN

    await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert (await client.get(url("foreign") + "/lockfile-diff", params=query())).status == 404
    assert (
        await client.get(
            url(environment).replace("/p1/", "/p2/") + "/lockfile-diff", params=query()
        )
    ).status == 404
    origin = str(client.make_url("/")).rstrip("/")
    assert (
        await client.post(
            url(environment) + "/lockfile-diff", params=query(), headers={"Origin": origin}
        )
    ).status == 405
    assert not peer.calls
    client.app[provisioning.PROXY].worker = None
    assert (await client.get(url(environment) + "/lockfile-diff", params=query())).status == 422
    assert not peer.calls
