# SPDX-License-Identifier: MIT
"""Pinned artifact downloads need no running worker or preview service."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import quote

import pytest

from sandbox_science.contracts import ArtifactRef
from sandbox_science.orchestrator.artifact_blobs import Blob
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app

from .route_support import TOKEN, log_in
from .test_pdf_viewer import SOURCE, publish


@pytest.fixture
def artifact(state_dir):
    documents = DocumentStore(store_path(state_dir))
    inventory = ArtifactInventory(documents)
    reference = publish(inventory, "download-1", "data/results.bin", b"0123456789", "text/html")
    inventory.record_metadata(
        SOURCE,
        "unretained.bin",
        sha256="a" * 64,
        size=2**31,
        media_type="application/octet-stream",
        note="too_large",
    )
    documents.close()
    return reference


def route(reference, *, project="p1", digest=None):
    return (
        f"/api/v0/projects/{project}/artifacts/{reference.artifact_id}"
        f"/revisions/{reference.revision}/content?sha256={digest or reference.sha256}"
    )


async def client(aiohttp_client, state_dir, static_dir):
    workspace = await aiohttp_client(
        create_app(ProjectStore(state_dir, readonly=True), static_dir, login_token=TOKEN)
    )
    await log_in(workspace)
    return workspace


async def test_download_survives_store_reopen_without_worker_or_preview(
    aiohttp_client, state_dir, static_dir, artifact
):
    workspace = await client(aiohttp_client, state_dir, static_dir)
    response = await workspace.get(route(artifact))
    assert response.status == 200
    assert await response.read() == b"0123456789"
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert response.headers["Content-Disposition"] == "attachment; filename*=UTF-8''results.bin"
    assert response.headers["ETag"] == f'"{artifact.sha256}"'
    assert response.headers["X-Content-SHA256"] == artifact.sha256
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"
    head = await workspace.head(route(artifact))
    assert head.status == 200
    assert head.content_length == 10
    assert await head.read() == b""


@pytest.mark.parametrize(
    ("selected", "expected", "content_range"),
    [
        ("bytes=2-5", b"2345", "bytes 2-5/10"),
        ("bytes=-3", b"789", "bytes 7-9/10"),
        ("bytes=8-999", b"89", "bytes 8-9/10"),
    ],
)
async def test_download_ranges(
    aiohttp_client, state_dir, static_dir, artifact, selected, expected, content_range
):
    workspace = await client(aiohttp_client, state_dir, static_dir)
    response = await workspace.get(route(artifact), headers={"Range": selected})
    assert response.status == 206
    assert response.headers["Content-Range"] == content_range
    assert await response.read() == expected
    response = await workspace.get(
        route(artifact), headers={"Range": selected, "If-Range": '"different-revision"'}
    )
    assert response.status == 200
    assert await response.read() == b"0123456789"


@pytest.mark.parametrize("selected", ["bytes=10-", "bytes=-0", "bytes=0-1,3-4", "items=1-2"])
async def test_invalid_ranges_are_refused(
    aiohttp_client, state_dir, static_dir, artifact, selected
):
    workspace = await client(aiohttp_client, state_dir, static_dir)
    response = await workspace.get(route(artifact), headers={"Range": selected})
    assert response.status == 416
    assert response.headers["Content-Range"] == "bytes */10"


async def test_pins_scope_and_login_are_required(aiohttp_client, state_dir, static_dir, artifact):
    workspace = await aiohttp_client(
        create_app(ProjectStore(state_dir, readonly=True), static_dir, login_token=TOKEN)
    )
    assert (await workspace.get(route(artifact))).status == 401
    await log_in(workspace)
    assert (await workspace.get(route(artifact, project="p2"))).status == 404
    assert (await workspace.get(route(artifact, digest="a" * 64))).status == 409
    assert (await workspace.get(route(artifact, digest="bad"))).status == 400
    assert (await workspace.get(route(artifact) + "&path=" + quote("../../outside"))).status == 400


@pytest.fixture
def external_artifact(state_dir):
    documents = DocumentStore(store_path(state_dir))
    inventory = ArtifactInventory(documents)
    chunk = bytes(range(256)) * 4096
    tail = b"retained external artifact tail"
    digest = hashlib.sha256()
    for _ in range(9):
        digest.update(chunk)
    digest.update(tail)
    size = 9 * len(chunk) + len(tail)
    worker = ArtifactRef(
        artifact_id="art-external-download",
        project_id="p1",
        sha256=digest.hexdigest(),
        size=size,
        media_type="application/octet-stream",
        source_path="data/large-results.bin",
        produced_by="attempt-1",
        created_at=datetime.now(UTC),
    )
    operation = "external-download"
    inventory.begin(SOURCE, operation, worker.source_path, None)
    inventory.pin(SOURCE, operation, worker.source_path, None, worker)
    transfer = inventory.reserve_transfer("p1", size, worker.sha256)
    writer = inventory.blobs.writer(Blob(worker.sha256, size))
    try:
        for _ in range(9):
            writer.write(chunk)
        writer.write(tail)
        blob = writer.commit()
        publication = inventory.finish(
            SOURCE, operation, worker.source_path, None, worker, b"", blob=blob, transfer=transfer
        )
        assert publication.revision is not None
        return publication.revision, chunk, tail
    finally:
        writer.discard()
        documents.close()


async def test_external_blob_over_inline_bound_streams_full_and_pinned_range_without_worker(
    aiohttp_client, state_dir, static_dir, external_artifact
):
    reference, pattern, tail = external_artifact
    assert reference.size > 8 * 1024 * 1024
    # The app opens the durable store anew, with neither worker nor preview client.
    workspace = await client(aiohttp_client, state_dir, static_dir)
    response = await workspace.get(route(reference))
    assert response.status == 200
    assert response.content_length == reference.size
    assert response.headers["X-Content-SHA256"] == reference.sha256
    assert response.headers["ETag"] == f'"{reference.sha256}"'
    received, digest = 0, hashlib.sha256()
    async for chunk in response.content.iter_chunked(192 * 1024):
        received += len(chunk)
        digest.update(chunk)
    assert received == reference.size
    assert digest.hexdigest() == reference.sha256

    start, end = 8 * 1024 * 1024 - 17, 8 * 1024 * 1024 + 73
    ranged = await workspace.get(
        route(reference),
        headers={"Range": f"bytes={start}-{end}", "If-Range": f'"{reference.sha256}"'},
    )
    assert ranged.status == 206
    assert ranged.headers["Content-Range"] == f"bytes {start}-{end}/{reference.size}"
    assert ranged.content_length == end - start + 1
    assert await ranged.read() == bytes(
        pattern[offset % len(pattern)] for offset in range(start, end + 1)
    )
    suffix = await workspace.get(route(reference), headers={"Range": f"bytes=-{len(tail)}"})
    assert suffix.status == 206
    assert await suffix.read() == tail
