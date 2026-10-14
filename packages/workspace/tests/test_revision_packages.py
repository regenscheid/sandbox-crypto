# SPDX-License-Identifier: MIT
"""Recorded-package proxy uses a synthetic peer, never a worker or lockfile store."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiohttp import web

from sandbox_science.contracts.revision_packages import RevisionPackagePage
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.api import Timeouts

from .test_provisioning import ClientFactory, Peer, ServerFactory, create, url

PIN = "a" * 64


def query(**changes: object) -> dict[str, str]:
    result = {"pin": PIN, "offset": "0", "limit": "8"}
    result.update({name: str(value) for name, value in changes.items()})
    return result


def payload(environment: str, mode: str = "populated") -> dict[str, object]:
    ref = {"sha256": "b" * 64, "size": 100, "conda_packages": 2, "pip_distributions": 1}
    return {
        "project_id": "p1",
        "environment_id": environment,
        "observed_at": datetime.now(UTC).isoformat(),
        "selection": {
            "revision": 1,
            "record_sha256": PIN,
            "lockfile": None
            if mode == "held"
            else {
                **ref,
                **({"conda_packages": 0, "pip_distributions": 0} if mode == "empty" else {}),
            },
        },
        "header": None
        if mode == "held"
        else {
            **ref,
            **({"conda_packages": 0, "pip_distributions": 0} if mode == "empty" else {}),
            "python": None,
            "platform": None,
            "skipped": 0,
        },
        "status": "held" if mode == "held" else "complete",
        "holds": ["lockfile_unrecorded"] if mode == "held" else [],
        "total": None if mode == "held" else 0 if mode == "empty" else 3,
        "offset": 0,
        "limit": 8,
        "next_offset": None,
        "packages": []
        if mode in {"held", "empty"}
        else [
            {
                "ecosystem": "conda",
                "name": "a-conda",
                "location": None,
                "package": {"name": "a-conda", "version": "1", "build": "0", "license": "AGPL-3.0"},
                "license": {"declared": "AGPL-3.0", "agpl": True},
            },
            {
                "ecosystem": "conda",
                "name": "z-conda",
                "location": None,
                "package": {"name": "z-conda", "version": "1", "build": "0", "license": None},
                "license": {"declared": None, "agpl": None},
            },
            {
                "ecosystem": "pip",
                "name": "some-pkg",
                "location": "prefix",
                "package": {"name": "Some_Pkg", "version": "1", "location": "prefix"},
                "license": {"declared": None, "agpl": None},
            },
        ],
    }


class PackagePeer(Peer):
    """An HTTP DTO fixture only; no provisioning manager, source scan or guest."""

    def __init__(self) -> None:
        super().__init__()
        self.payload: dict[str, object] | None = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if not request.path.endswith("/packages"):
            return await super().handle(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-secret-provisioning-peer"
        self.calls.append((request.path, dict(request.query)))
        self.entered.set()
        if self.blocked:
            await self.release.wait()
        if self.status != 200:
            return web.json_response(
                {"error": {"message": "test-secret-not-forwarded"}}, status=self.status
            )
        if self.mode == "redirect":
            raise web.HTTPTemporaryRedirect("/stolen")
        if self.mode == "large":
            return web.Response(body=b" " * (64 * 1024 + 1), content_type="application/json")
        environment = request.match_info["tail"].split("/")[1]
        value = copy.deepcopy(self.payload) if self.payload is not None else payload(environment)
        if self.mode == "scope":
            value["project_id"] = "p2"
        elif self.mode == "environment":
            value["environment_id"] = "foreign"
        raw = json.dumps(value)
        return web.Response(text=raw, content_type="application/json")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["populated", "empty", "held"])
async def test_exact_recorded_metadata_is_forwarded_without_inventory_or_license_inference(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    mode: str,
) -> None:
    peer = PackagePeer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    peer.payload = payload(environment, mode)
    before = ProjectStore(state_dir).get("p1")
    response = await client.get(url(environment) + "/revisions/1/packages", params=query())
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert await response.json() == RevisionPackagePage.model_validate_json(
        json.dumps(peer.payload), strict=True
    ).model_dump(mode="json")
    assert peer.calls == [
        (
            f"/v1/environments/{environment}/provisioning/observations/revisions/1/packages",
            {"project_id": "p1", **query()},
        )
    ]
    assert ProjectStore(state_dir).get("p1") == before
    if mode == "populated":
        rows = (await response.json())["packages"]
        assert rows[0]["license"] == {"declared": "AGPL-3.0", "agpl": True}
        assert rows[1]["license"] == rows[2]["license"] == {"declared": None, "agpl": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "offset=0",
        "pin=bad",
        urlencode(query(pin=PIN.upper())),
        urlencode(query(offset="00")),
        urlencode(query(offset=40_001)),
        urlencode(query(limit=0)),
        urlencode(query(limit=9)),
        urlencode(query(limit="true")),
        urlencode(query()) + "&pin=" + PIN,
        urlencode(query()) + "&project_id=p2",
        urlencode(query()) + "&seq=1",
        urlencode(query()) + "&wait=8",
    ],
)
async def test_query_requires_exact_pin_and_bounded_canonical_page_before_peer(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    raw: str,
) -> None:
    peer = PackagePeer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    response = await client.get(
        url(environment) + "/revisions/1/packages" + ("?" + raw if raw else "")
    )
    assert response.status == 400
    assert response.headers["Cache-Control"] == "no-store"
    assert not peer.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "header",
        "ref",
        "held-total",
        "bool",
        "float",
        "license",
        "pip-license",
        "large",
        "redirect",
    ],
)
async def test_strict_response_echo_header_ref_license_page_and_raw_bounds(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    damage: str,
) -> None:
    peer = PackagePeer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    value = payload(environment, "held" if damage == "held-total" else "populated")
    if damage == "ref":
        selected = value["selection"]
        assert isinstance(selected, dict)
        ref = selected["lockfile"]
        assert isinstance(ref, dict)
        ref["sha256"] = "f" * 64
    elif damage == "header":
        header = value["header"]
        assert isinstance(header, dict)
        header["sha256"] = "f" * 64
    elif damage in {"bool", "float", "held-total"}:
        value["total"] = True if damage == "bool" else 3.0 if damage == "float" else 0
    elif damage in {"license", "pip-license"}:
        rows = value["packages"]
        assert isinstance(rows, list)
        row = rows[0 if damage == "license" else 2]
        assert isinstance(row, dict)
        row["license"] = {"declared": "MIT", "agpl": False}
    else:
        peer.mode = damage
    peer.payload = value
    response = await client.get(url(environment) + "/revisions/1/packages", params=query())
    assert response.status == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert "packages" not in await response.json()
    assert len(peer.calls) == 1


@pytest.mark.asyncio
async def test_actual_page_continuation_is_preserved_not_assumed_limit(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
) -> None:
    peer = PackagePeer()
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    value = payload(environment)
    rows = value["packages"]
    assert isinstance(rows, list)
    value.update(packages=rows[:1], next_offset=1)
    peer.payload = value
    response = await client.get(url(environment) + "/revisions/1/packages", params=query())
    assert response.status == 200
    assert (await response.json())["next_offset"] == 1
    value.update(packages=rows[1:], offset=1, next_offset=None)
    response = await client.get(url(environment) + "/revisions/1/packages", params=query(offset=1))
    assert response.status == 200
    assert peer.calls[-1][1]["offset"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 409, 422, 500])
async def test_errors_preserve_stale_unsupported_unavailable_without_fallback(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    status: int,
) -> None:
    peer = PackagePeer()
    peer.status = status
    client, _, environment = await create(
        aiohttp_client, aiohttp_server, state_dir, static_dir, peer
    )
    response = await client.get(url(environment) + "/revisions/1/packages", params=query())
    assert response.status == (503 if status == 500 else status)
    assert response.headers["Cache-Control"] == "no-store"
    assert "packages" not in await response.json()
    assert "test-secret" not in await response.text()
    assert len(peer.calls) == 1


@pytest.mark.asyncio
async def test_shared_extended_budget_does_not_change_ordinary_read_timeout(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = PackagePeer()
    peer.blocked = True
    client, _, environment = await create(
        aiohttp_client,
        aiohttp_server,
        state_dir,
        static_dir,
        peer,
        timeouts=Timeouts(worker_call=0.01, worker_comparison=5.0),
    )
    call = asyncio.create_task(
        client.get(url(environment) + "/revisions/1/packages", params=query())
    )
    await peer.entered.wait()
    await asyncio.sleep(0.03)  # three ordinary deadlines pass; the package read keeps waiting
    assert not call.done()
    peer.release.set()
    assert (await call).status == 200
    peer.release.clear()
    peer.entered.clear()
    assert (await client.get(url(environment))).status == 503
    peer.release.set()
