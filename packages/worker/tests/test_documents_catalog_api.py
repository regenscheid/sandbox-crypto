# SPDX-License-Identifier: MIT
"""Catalog ingress never borrows an ordinary execution token's authority."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.documents import MAX_CATALOG_PAGE_BYTES, MAX_CONTENT_BODY
from sandbox_science.worker.app import CATALOG_WORK, _catalog_call, _drain_catalog, create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.documents_catalog import DocumentsCatalog
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

from .test_documents_catalog import chunks, entry, page, target

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
POOL = "test-pool-catalog-token-" + "p" * 32
CONTROL = "test-control-catalog-token-" + "c" * 32
ORDINARY = "test-ordinary-catalog-token-" + "o" * 32
ROOT = "/v1/document-catalog/p1"
pytestmark = pytest.mark.asyncio


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def api(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> AsyncIterator[tuple[TestClient[Any, Any], DocumentsCatalog, ExecutionManager]]:
    records = Store(tmp_path / "worker.sqlite3")
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    manager = ExecutionManager(
        records,
        LocalBackend(LocalBackendSettings(), tmp_path),
        Blobs(tmp_path / "artifacts"),
        pool_id="test",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    environments = cast(Environments, SimpleNamespace(deleted=[]))
    settings = [
        ClientSettings(name="ordinary", token_sha256=token_digest(ORDINARY), projects=("p1",)),
        ClientSettings(
            name="control",
            token_sha256=token_digest(CONTROL),
            projects=("p1",),
            document_catalog=True,
        ),
    ]
    client = await aiohttp_client(
        create_app(manager, POOL, environments, settings, document_catalog=catalog)
    )
    yield client, catalog, manager
    await client.close()
    await manager.close()
    catalog.close()
    records.close()


async def test_ingress_default_deny_and_project_hiding(
    api: tuple[TestClient[Any, Any], DocumentsCatalog, ExecutionManager],
) -> None:
    client, catalog, _ = api
    assert (await client.get(ROOT)).status == 401
    assert (await client.get(ROOT, headers=auth(POOL))).status == 200
    assert (await client.get(ROOT, headers=auth(ORDINARY))).status == 403
    assert (await client.get(ROOT, headers=auth(CONTROL))).status == 200
    assert (await client.get("/v1/document-catalog/p2", headers=auth(CONTROL))).status == 404
    snapshot = target([])
    for suffix in ["snapshots", "deltas", "pages", "content", "commit"]:
        response = await client.post(f"{ROOT}/{suffix}", data=b"invalid", headers=auth(ORDINARY))
        assert response.status == 403
        response = await client.post(
            f"/v1/document-catalog/p2/{suffix}", data=b"invalid", headers=auth(CONTROL)
        )
        assert response.status == 404
    response = await client.post(
        f"{ROOT}/snapshots", json=snapshot.model_dump(mode="json"), headers=auth(CONTROL)
    )
    assert response.status == 200
    assert catalog.status("p1").staging is not None
    other = target([], project="p2")
    response = await client.post(
        f"{ROOT}/commit", json=other.model_dump(mode="json"), headers=auth(CONTROL)
    )
    assert response.status == 400
    assert catalog.status("p2").catalog_seq is None


async def test_http_completion_replay_has_no_executor_or_publication_side_effects(
    api: tuple[TestClient[Any, Any], DocumentsCatalog, ExecutionManager],
) -> None:
    client, catalog, manager = api
    document = entry()
    snapshot = target([document])
    payloads = [
        ("snapshots", snapshot),
        ("pages", page(snapshot, [document])),
        ("content", next(chunks(snapshot, b"proof"))),
        ("commit", snapshot),
    ]
    for suffix, payload in payloads:
        response = await client.post(
            f"{ROOT}/{suffix}", json=payload.model_dump(mode="json"), headers=auth(CONTROL)
        )
        assert response.status == 200, await response.text()
        assert (await response.json())["publication"] == "disabled"
    response = await client.post(
        f"{ROOT}/commit", json=snapshot.model_dump(mode="json"), headers=auth(CONTROL)
    )
    assert response.status == 200
    assert (await response.json())["replayed"]
    assert not manager._environments
    assert not manager._live
    assert catalog._db.execute("SELECT count(*) FROM document_written_revisions").fetchone()[0] == 0
    assert not list(manager.state_dir.rglob("documents"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("catalog_seq", True),
        ("entry_count", 0.0),
        ("protocol", 1.0),
        ("manifest_sha256", "A" * 64),
        ("extra", "ignored?"),
    ],
)
async def test_http_strict_schema_validation(
    api: tuple[TestClient[Any, Any], DocumentsCatalog, ExecutionManager], field: str, value: object
) -> None:
    client, catalog, _ = api
    body = {**target([]).model_dump(mode="json"), field: value}
    response = await client.post(f"{ROOT}/snapshots", json=body, headers=auth(CONTROL))
    assert response.status == 400
    assert catalog.status("p1").staging is None


@pytest.mark.parametrize(
    ("suffix", "limit"),
    [
        ("snapshots", MAX_CATALOG_PAGE_BYTES),
        ("pages", MAX_CATALOG_PAGE_BYTES),
        ("deltas", MAX_CATALOG_PAGE_BYTES),
        ("commit", MAX_CATALOG_PAGE_BYTES),
        ("content", MAX_CONTENT_BODY),
    ],
)
async def test_chunked_body_is_bounded_before_parsing(
    api: tuple[TestClient[Any, Any], DocumentsCatalog, ExecutionManager], suffix: str, limit: int
) -> None:
    client, catalog, _ = api

    async def body() -> AsyncIterator[bytes]:
        for _ in range(limit // 4096 + 1):
            yield b" " * 4096

    response = await client.post(f"{ROOT}/{suffix}", data=body(), headers=auth(CONTROL))
    assert response.status == 400
    assert "byte limit" in (await response.json())["error"]["message"]
    assert catalog.status("p1").staging is None


async def test_catalog_permission_is_an_explicit_boolean() -> None:
    settings = {"name": "ordinary", "token_sha256": token_digest(ORDINARY), "projects": ["p1"]}
    assert not ClientSettings.model_validate(settings).document_catalog
    for value in [1, "true", None]:
        with pytest.raises(ValidationError):
            ClientSettings.model_validate({**settings, "document_catalog": value})


async def test_cancelled_sql_keeps_admission_until_done_and_cleanup_drains(
    api: tuple[TestClient[Any, Any], DocumentsCatalog, ExecutionManager],
) -> None:
    client, catalog, _ = api
    catalog.limits = catalog.limits.model_copy(update={"max_pending_requests": 1})
    started = threading.Event()
    release = threading.Event()
    app = cast(TestServer, client.server).app
    request = cast(web.Request, SimpleNamespace(app=app))

    def blocked(project: str):
        started.set()
        assert release.wait(5)
        return catalog.status(project)

    caller = asyncio.create_task(_catalog_call(request, blocked, "p1"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert len(app[CATALOG_WORK]) == 1
        with pytest.raises(WorkerError) as error:
            await _catalog_call(request, catalog.status, "p1")
        assert error.value.kind == ErrorKind.UNAVAILABLE
        # Catalog SQL owns one private thread; it cannot fill the default pool
        # with lock waiters and starve unrelated worker I/O.
        assert (
            await asyncio.wait_for(asyncio.to_thread(lambda: "unrelated I/O"), 1) == "unrelated I/O"
        )
        cleanup = asyncio.create_task(_drain_catalog(app))
        await asyncio.sleep(0)
        assert not cleanup.done()
        release.set()
        await asyncio.wait_for(cleanup, 2)
        assert not app[CATALOG_WORK]
    finally:
        release.set()
