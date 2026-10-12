# SPDX-License-Identifier: MIT
"""Typed, scoped and bounded catalog transport; this grants no worker authority."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts.documents import (
    CatalogChange,
    CatalogContentChunk,
    CatalogDelta,
    CatalogSnapshot,
    CatalogSnapshotPage,
    CatalogStatus,
    PublicationEntry,
    generation_id,
    manifest_sha256,
)
from sandbox_science.orchestrator import worker_client
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

TOKEN = "test-secret-catalog-client"  # noqa: S105 - visibly synthetic credential
ENTRY = PublicationEntry(
    document_id="doc-one",
    revision=1,
    scope="project",
    name="one",
    content_sha256=hashlib.sha256(b"x").hexdigest(),
    content_bytes=1,
)
MANIFEST = manifest_sha256("p", 1, [ENTRY])
TARGET = CatalogSnapshot(
    project_id="p",
    generation_id=generation_id(1, MANIFEST),
    catalog_seq=1,
    entry_count=1,
    total_content_bytes=1,
    manifest_sha256=MANIFEST,
)
PAGE = CatalogSnapshotPage(
    project_id="p", generation_id=TARGET.generation_id, start_index=0, entries=(ENTRY,)
)
DELTA = CatalogDelta(
    target=TARGET, after_seq=0, changes=(CatalogChange(catalog_seq=1, entry=ENTRY),)
)
CHUNK = CatalogContentChunk(
    project_id="p",
    generation_id=TARGET.generation_id,
    content_sha256=ENTRY.content_sha256,
    offset=0,
    content_b64=base64.b64encode(b"x").decode(),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("catalog_begin", "snapshots", TARGET),
        ("catalog_page", "pages", PAGE),
        ("catalog_delta", "deltas", DELTA),
        ("catalog_content", "content", CHUNK),
        ("catalog_commit", "commit", TARGET),
    ],
)
async def test_catalog_post_routes_pin_typed_body_and_project(method, suffix, body):
    calls = []

    async def receive(request: web.Request) -> web.Response:
        assert request.method == "POST"
        assert request.path == f"/v1/document-catalog/p/{suffix}"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        calls.append(await request.json())
        return web.json_response(CatalogStatus(project_id="p").model_dump(mode="json"))

    app = web.Application()
    app.router.add_post("/v1/document-catalog/p/" + suffix, receive)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        status = await getattr(client, method)(body)
    assert status.project_id == "p"
    assert status.publication == "disabled"
    assert calls == [body.model_dump(mode="json")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        CatalogStatus(project_id="other").model_dump(mode="json"),
        CatalogStatus(
            project_id="p", acknowledged=TARGET.model_copy(update={"project_id": "other"})
        ).model_dump(mode="json"),
        {"project_id": "p", "catalog_seq": True},
        {"project_id": "p", "unexpected": "field"},
    ],
)
async def test_catalog_mismatched_or_invalid_status_refuses(payload):
    async def receive(request: web.Request) -> web.Response:
        return web.json_response(payload)

    app = web.Application()
    app.router.add_get("/v1/document-catalog/p", receive)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        with pytest.raises(WorkerAPIError):
            await client.catalog_status("p")


@pytest.mark.asyncio
async def test_catalog_invalid_project_never_requests_or_expands_path():
    async with WorkerClient("http://127.0.0.1:1", TOKEN) as client:
        for project in ("../other", "p?token=x", "p/other", "p\n", ""):
            with pytest.raises(WorkerAPIError, match="invalid catalog project"):
                await client.catalog_status(project)
        assert client._session is None


@pytest.mark.asyncio
async def test_catalog_chunked_status_is_bounded_before_decode():
    async def receive(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(json.dumps({"project_id": "p"}).encode() + b" " * 20_000)
        return response

    app = web.Application()
    app.router.add_get("/v1/document-catalog/p", receive)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        with pytest.raises(WorkerAPIError, match="over 8192 bytes"):
            await client.catalog_status("p")


@pytest.mark.asyncio
async def test_catalog_redirect_never_reaches_other_endpoint():
    followed = []

    async def redirect(request: web.Request) -> web.Response:
        raise web.HTTPFound("/sentinel")

    async def sentinel(request: web.Request) -> web.Response:
        followed.append(True)
        return web.json_response(CatalogStatus(project_id="p").model_dump(mode="json"))

    app = web.Application()
    app.router.add_get("/v1/document-catalog/p", redirect)
    app.router.add_get("/sentinel", sentinel)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        with pytest.raises(WorkerAPIError):
            await client.catalog_status("p")
    assert followed == []


@pytest.mark.asyncio
async def test_catalog_total_deadline_includes_trickled_response(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(worker_client, "CATALOG_TIMEOUT", 0.05)

    async def trickle(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        try:
            for _ in range(20):
                await response.write(b" ")
                await asyncio.sleep(0.02)
        except ConnectionResetError:
            pass
        return response

    app = web.Application()
    app.router.add_get("/v1/document-catalog/p", trickle)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        with pytest.raises(WorkerAPIError, match="cannot reach the worker"):
            await client.catalog_status("p")
