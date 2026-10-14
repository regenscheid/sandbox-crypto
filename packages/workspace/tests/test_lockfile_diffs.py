# SPDX-License-Identifier: MIT
"""Exact comparison proxy tests use a synthetic HTTP peer, never worker/blob execution."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiohttp import web

from sandbox_science.contracts.lockfile_diffs import LockfileDiffPage
from sandbox_science.workspace.api import Timeouts

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
        "offset",
        "header",
        "held-total",
        "bool",
        "float",
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
    if damage == "offset":
        value["offset"] = 1
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
async def test_comparison_only_budget_preserves_ordinary_transport_timeout(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = DiffPeer()
    peer.blocked = True
    client, _, environment = await create(
        aiohttp_client,
        aiohttp_server,
        state_dir,
        static_dir,
        peer,
        timeouts=Timeouts(worker_call=0.01, worker_comparison=5.0),
    )
    call = asyncio.create_task(client.get(url(environment) + "/lockfile-diff", params=query()))
    await peer.entered.wait()
    await asyncio.sleep(0.03)  # three ordinary deadlines pass; the comparison keeps waiting
    assert not call.done()
    peer.release.set()
    assert (await call).status == 200
    peer.release.clear()
    peer.entered.clear()
    assert (await client.get(url(environment))).status == 503
    peer.release.set()


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
