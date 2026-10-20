# SPDX-License-Identifier: MIT
"""Uploads through the authenticated HTTP boundary and a protocol-speaking worker."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.uploads import UserUpload
from sandbox_science.testing import fake_worker_app
from sandbox_science.workspace.uploads import message_text

from .route_support import ClientFactory, ServerFactory, origin, workspace

SESSION = "/api/v0/projects/p1/sessions/a"
UPLOADS = SESSION + "/uploads"


def body(
    data: bytes = b"# Notes\n\n$E = mc^2$", name: str = "notes.md", identity: str = "upload-test"
) -> dict[str, str]:
    return {
        "upload_id": identity,
        "filename": name,
        "media_type": "text/markdown",
        "content_b64": base64.b64encode(data).decode(),
    }


async def unmanaged(request: web.Request) -> web.Response:
    return web.json_response(
        {"error": {"kind": "unsupported", "message": "unmanaged worker"}}, status=422
    )


@pytest.mark.parametrize("mode", ["create", "refused", "other-project", "other-profile"])
async def test_first_attachment_prepares_only_the_configured_environment(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    mode: str,
) -> None:
    store = ProjectStore(state_dir)
    try:
        project = store.get("p1")
    finally:
        store.close()
    events: list[str] = []
    info = {
        "environment_id": project.sessions["a"].environment_id,
        "project_id": project.project_id,
        "profile": project.profile,
        "policy": project.policy,
        "image": "test:synthetic",
        "sandbox": "test-sandbox",
        "state": "ready",
        "created_at": "2026-10-17T12:00:00Z",
        "updated_at": "2026-10-17T12:00:00Z",
    }

    async def observe(request: web.Request) -> web.Response:
        events.append("observe")
        assert request.match_info["environment"] == info["environment_id"]
        if mode.startswith("other-"):
            changed = "project_id" if mode == "other-project" else "profile"
            return web.json_response(info | {changed: "other"})
        return web.json_response({"error": {"kind": "not_found", "message": "absent"}}, status=404)

    async def create(request: web.Request) -> web.Response:
        events.append("create")
        assert await request.json() == {
            key: info[key] for key in ("environment_id", "project_id", "profile", "policy")
        }
        if mode == "refused":
            return web.json_response(
                {"error": {"kind": "unavailable", "message": "cannot prepare"}}, status=503
            )
        return web.json_response(info)

    async def write(request: web.Request) -> web.Response:
        assert events == ["observe", "create"]
        events.append("write")
        sent = await request.json()
        assert sent["context"]["environment_id"] == info["environment_id"]
        assert sent["create_only"]
        data = base64.b64decode(sent["content_b64"])
        return web.json_response({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    app = fake_worker_app()
    app.router.add_get("/v1/environments/{environment}", observe)
    app.router.add_post("/v1/environments", create)
    app.router.add_post("/v1/files/write", write)
    server = await aiohttp_server(app)
    client = await workspace(
        aiohttp_client, state_dir, static_dir, worker_url=str(server.make_url("/"))
    )
    assert (await client.post(UPLOADS, json=body(), headers=origin(client))).status == 200
    assert events == []
    response = await client.post(
        SESSION + "/messages",
        json={"message_id": "fresh", "text": "Read this", "attachments": ["upload-test"]},
        headers=origin(client),
    )
    assert response.status == (200 if mode == "create" else 503), await response.text()
    assert events == (
        ["observe", "create", "write"]
        if mode == "create"
        else ["observe", "create"]
        if mode == "refused"
        else ["observe"]
    )
    messages = (await (await client.get(SESSION + "/messages")).json())["messages"]
    assert len(messages) == (1 if mode == "create" else 0)
    assert (await client.get(UPLOADS + "/upload-test/content")).status == 200


async def test_original_roundtrip_replay_scope_and_text_derivative(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    raw = b"\xef\xbb\xbf# Notes\r\n\r\n$E=mc^2$\r\n"
    first = await client.post(UPLOADS, json=body(raw), headers=origin(client))
    assert first.status == 200, await first.text()
    found = await first.json()
    assert found["author"] == "user"
    assert found["sha256"] == hashlib.sha256(raw).hexdigest()
    assert found["path"] == "shared/uploads/a/upload-test/source.md"
    repeat = await client.post(UPLOADS, json=body(raw), headers=origin(client))
    assert await repeat.json() == found
    assert len((await (await client.get(UPLOADS)).json())["uploads"]) == 1
    conflict = await client.post(UPLOADS, json=body(b"other"), headers=origin(client))
    assert conflict.status == 409
    content = await client.get(UPLOADS + "/upload-test/content")
    assert await content.read() == raw
    assert content.content_type == "application/octet-stream"
    assert content.headers["Content-Disposition"].startswith("attachment;")
    assert content.headers["X-Content-Type-Options"] == "nosniff"
    assert (await client.get(UPLOADS.replace("/a/", "/c/") + "/upload-test/content")).status == 404
    imported = await client.post(UPLOADS + "/upload-test/document", json={}, headers=origin(client))
    assert imported.status == 200, await imported.text()
    derivative = await imported.json()
    again = await client.post(UPLOADS + "/upload-test/document", json={}, headers=origin(client))
    assert await again.json() == derivative
    document = await (
        await client.get("/api/v0/projects/p1/documents/" + derivative["document_id"])
    ).json()
    assert document["revision"]["author"]["kind"] == "user"
    assert document["text"] == "# Notes\n\n$E=mc^2$\n"
    assert await (await client.get(UPLOADS + "/upload-test/content")).read() == raw


@pytest.mark.parametrize("name", ["../escape", "a/b", "a\\b", "a\n.md", "..", "\ud800.md"])
async def test_upload_names_are_not_paths(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, name: str
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.post(UPLOADS, json=body(name=name), headers=origin(client))
    assert response.status == 400
    assert (await (await client.get(UPLOADS)).json())["uploads"] == []


async def test_auth_limits_closed_session_and_binary_import(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    assert (await anonymous.post(UPLOADS, json=body(), headers=origin(anonymous))).status == 401
    client = await workspace(aiohttp_client, state_dir, static_dir)
    assert (await client.post(UPLOADS, json=body())).status == 403
    assert (
        await client.post(UPLOADS.replace("/a/", "/b/"), json=body(), headers=origin(client))
    ).status == 409
    for value in (
        body(b"x" * (8 * 1024 * 1024 + 1)),
        body() | {"content_b64": "!!!"},
        body() | {"author": "agent"},
    ):
        assert (await client.post(UPLOADS, json=value, headers=origin(client))).status == 400
    raw = b"\x00\xff%PDF"
    assert (
        await client.post(UPLOADS, json=body(raw, "paper.pdf"), headers=origin(client))
    ).status == 200
    assert (
        await client.post(UPLOADS + "/upload-test/document", json={}, headers=origin(client))
    ).status == 422
    assert await (await client.get(UPLOADS + "/upload-test/content")).read() == raw
    message = {"message_id": "m", "text": "Read this", "attachments": ["upload-test"]}
    assert (
        await client.post(SESSION + "/messages", json=message, headers=origin(client))
    ).status == 503
    assert (await (await client.get(SESSION + "/messages")).json())["messages"] == []


async def test_delivery_reconciles_identical_copy_and_replay_never_redelivers(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    files: dict[str, bytes] = {}
    calls: list[dict[str, Any]] = []

    async def write(request: web.Request) -> web.Response:
        sent = await request.json()
        calls.append(sent)
        assert sent["context"]["actor"] == "user"
        assert sent["context"]["project_id"] == "p1"
        assert sent["context"]["session_id"] == "a"
        assert sent["create_only"] is True
        path = sent["path"]
        if path in files:
            return web.json_response(
                {"error": {"kind": "conflict", "message": "exists"}}, status=409
            )
        files[path] = base64.b64decode(sent["content_b64"])
        # Simulate a write that succeeded but whose acknowledgement was lost.
        return web.json_response(
            {"error": {"kind": "unavailable", "message": "lost reply"}}, status=503
        )

    async def read(request: web.Request) -> web.Response:
        sent = await request.json()
        data = files[sent["path"]]
        return web.json_response(
            {
                "path": sent["path"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "offset": 0,
                "content_b64": "",
                "eof": False,
            }
        )

    app = fake_worker_app()
    app.router.add_get("/v1/environments/{environment}", unmanaged)
    app.router.add_post("/v1/files/write", write)
    app.router.add_post("/v1/files/read", read)
    server = await aiohttp_server(app)
    client = await workspace(
        aiohttp_client, state_dir, static_dir, worker_url=str(server.make_url("/"))
    )
    uploaded = await client.post(UPLOADS, json=body(), headers=origin(client))
    found = await uploaded.json()
    message = {
        "message_id": "message-upload",
        "text": "Check the equation",
        "attachments": ["upload-test"],
    }
    response = await client.post(SESSION + "/messages", json=message, headers=origin(client))
    assert response.status == 200, await response.text()
    accepted = await response.json()
    assert found["sha256"] in accepted["message"]["text"]
    assert found["path"] in accepted["message"]["text"]
    files[found["path"]] = b"agent edit"
    replay = await client.post(SESSION + "/messages", json=message, headers=origin(client))
    assert await replay.json() == accepted
    assert len(calls) == 1
    retry = await client.post(
        SESSION + "/messages", json=message | {"message_id": "new"}, headers=origin(client)
    )
    assert retry.status == 409
    assert files[found["path"]] == b"agent edit"
    cross = await client.post(
        SESSION.replace("sessions/a", "sessions/c") + "/messages",
        json=message | {"message_id": "other"},
        headers=origin(client),
    )
    assert cross.status == 404


async def test_attachment_budget_is_checked_before_worker_delivery(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    from .test_routes_chat import ask, runs

    runs(state_dir).acquire("p1", "a", "run-x", "holder")
    question = ask(state_dir)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    assert (
        await client.post(UPLOADS, json=body(name="équation-\U0001d6fc.md"), headers=origin(client))
    ).status == 200
    value = {
        "message_id": "budget",
        "text": "x" * 16000,
        "question_id": question,
        "attachments": ["upload-test"],
    }
    response = await client.post(SESSION + "/messages", json=value, headers=origin(client))
    # No worker is configured: a delivery attempt would be 503 instead.
    assert response.status == 400, await response.text()
    assert "16000" in await response.text()
    response = await client.post(
        SESSION + "/messages",
        data=json.dumps(
            value | {"text": "é" * 16000 + "\n", "attachments": []}, ensure_ascii=False
        ),
        headers=origin(client) | {"Content-Type": "application/json"},
    )
    assert response.status == 200, await response.text()
    assert len((await response.json())["question"]["answer"]["text"]) == 16000


def test_manifest_unicode_encoding_matches_browser_budget() -> None:
    file = UserUpload(
        upload_id="upload-test",
        project="p1",
        session="s1",
        filename="équation-\U0001d6fc.md",
        media_type="text/markdown",
        sha256="a" * 64,
        size=3,
        created_at="2026-10-17T12:00:00Z",
        author="user",
        path="shared/uploads/s1/upload-test/source.md",
        number=1,
    )
    text = message_text("Check $x$  \n", [file])
    assert '"filename": "équation-\U0001d6fc.md"' in text
    assert len(text.encode()) == 312


def binary_headers(
    client: Any, identity: str = "binary", name: str = "équation-\U0001d6fc.pdf"
) -> dict[str, str]:
    from urllib.parse import quote

    return origin(client) | {
        "Content-Type": "application/octet-stream",
        "X-Upload-Metadata": quote(
            json.dumps(
                {"upload_id": identity, "filename": name, "media_type": "application/pdf"},
                ensure_ascii=False,
            )
        ),
    }


async def test_binary_upload_accepts_exactly_100_mib_and_retains_immutable_original(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    import tracemalloc
    from collections.abc import AsyncIterator

    from sandbox_science.orchestrator.uploads import MAX_UPLOAD_BYTES

    client = await workspace(aiohttp_client, state_dir, static_dir)
    block = bytes(range(256)) * 2048
    expected = hashlib.sha256()
    for _ in range(MAX_UPLOAD_BYTES // len(block)):
        expected.update(block)

    async def chunks(*, extra: bool = False) -> AsyncIterator[bytes]:
        for _ in range(MAX_UPLOAD_BYTES // len(block)):
            yield block
        if extra:
            yield b"!"

    tracemalloc.start()
    try:
        response = await client.post(UPLOADS, data=chunks(), headers=binary_headers(client))
        assert response.status == 200, await response.text()
        found = await response.json()
        content = await client.get(UPLOADS + "/binary/content")
        digest, received = hashlib.sha256(), 0
        async for part in content.content.iter_chunked(192 * 1024):
            digest.update(part)
            received += len(part)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert found["size"] == 100 * 1024 * 1024
    assert found["filename"] == "équation-\U0001d6fc.pdf"
    assert found["sha256"] == digest.hexdigest() == expected.hexdigest()
    assert received == MAX_UPLOAD_BYTES
    # Both ends of a real HTTP transfer, scratch and hashing share this process.
    assert peak < 32 * 1024 * 1024
    replay = await client.post(UPLOADS, data=chunks(), headers=binary_headers(client))
    assert await replay.json() == found
    conflict = await client.post(UPLOADS, data=b"changed", headers=binary_headers(client))
    assert conflict.status == 409
    too_big = await client.post(
        UPLOADS, data=chunks(extra=True), headers=binary_headers(client, "too-big")
    )
    assert too_big.status == 413
    assert len((await (await client.get(UPLOADS)).json())["uploads"]) == 1


async def test_binary_upload_keeps_auth_scope_and_metadata_guards(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    headers = binary_headers(client)
    assert (
        await client.post(
            UPLOADS, data=b"x", headers={k: v for k, v in headers.items() if k != "Origin"}
        )
    ).status == 403
    for changed in (
        {"X-Upload-Metadata": "{}"},
        {"X-Upload-Metadata": "%ff"},
        {"X-Upload-Metadata": "a" * 2049},
        {
            "X-Upload-Metadata": json.dumps(
                {"upload_id": "binary", "filename": "../escape", "author": "agent"}
            )
        },
    ):
        assert (await client.post(UPLOADS, data=b"x", headers=headers | changed)).status == 400
    assert (await client.post(UPLOADS + "?project=p2", data=b"x", headers=headers)).status == 400
    assert (
        await client.post(UPLOADS.replace("/a/", "/b/"), data=b"x", headers=headers)
    ).status == 409
    assert (await (await client.get(UPLOADS)).json())["uploads"] == []


async def test_stalled_binary_upload_has_its_own_bounded_deadline(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    from sandbox_science.workspace.api import Timeouts

    client = await workspace(aiohttp_client, state_dir, static_dir, timeouts=Timeouts(upload=0.05))

    async def stalled() -> AsyncIterator[bytes]:
        yield b"initial bytes"
        await asyncio.Event().wait()

    response = await client.post(UPLOADS, data=stalled(), headers=binary_headers(client))
    assert response.status == 503
    assert "upload timed out" in await response.text()
    assert (await (await client.get(UPLOADS)).json())["uploads"] == []


async def test_incompatible_worker_retains_upload_with_actionable_refusal(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    async def refuse(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response(
            {"error": {"kind": "unsupported", "message": "old executor"}}, status=422
        )

    app = fake_worker_app()
    app.router.add_get("/v1/environments/{environment}", unmanaged)
    app.router.add_post("/v1/files/write", refuse)
    server = await aiohttp_server(app)
    client = await workspace(
        aiohttp_client, state_dir, static_dir, worker_url=str(server.make_url("/"))
    )
    assert (await client.post(UPLOADS, json=body(), headers=origin(client))).status == 200
    response = await client.post(
        SESSION + "/messages",
        json={"message_id": "unsupported", "text": "Read this", "attachments": ["upload-test"]},
        headers=origin(client),
    )
    assert response.status == 422
    assert "recreate the environment" in await response.text()
    assert len((await (await client.get(UPLOADS)).json())["uploads"]) == 1
    assert (await (await client.get(SESSION + "/messages")).json())["messages"] == []


async def test_retained_uploads_are_serialized_across_tabs(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    from sandbox_science.orchestrator.uploads import UserUploads

    client = await workspace(aiohttp_client, state_dir, static_dir)
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    begin = UserUploads.begin
    calls = 0

    def held(self: UserUploads, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10)
        return begin(self, *args, **kwargs)

    monkeypatch.setattr(UserUploads, "begin", held)
    first = asyncio.create_task(
        client.post(UPLOADS, data=b"first", headers=binary_headers(client, "first"))
    )
    second = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(
            client.post(UPLOADS, data=b"second", headers=binary_headers(client, "second"))
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.02)
        assert calls == 1
        release.set()
        assert (await first).status == 200
        assert (await second).status == 200
    finally:
        release.set()
        await first
        if second is not None:
            await second


async def test_stalled_original_download_releases_transfer_capacity_without_appending_json(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from aiohttp import ClientPayloadError

    from sandbox_science.workspace.api import Timeouts

    client = await workspace(aiohttp_client, state_dir, static_dir, timeouts=Timeouts(upload=0.1))
    assert (await client.post(UPLOADS, json=body(), headers=origin(client))).status == 200

    async def stalled(response: web.StreamResponse, data: bytes) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(web.StreamResponse, "write", stalled)
    response = await client.get(UPLOADS + "/upload-test/content")
    assert response.status == 200
    with pytest.raises(ClientPayloadError):
        await response.read()
    # The download's failure releases admission; retained originals remain untouched.
    uploaded = await client.post(UPLOADS, json=body(identity="next"), headers=origin(client))
    assert uploaded.status == 200
    assert len((await (await client.get(UPLOADS)).json())["uploads"]) == 2


async def test_timed_out_store_write_keeps_admission_until_its_thread_finishes(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    from sandbox_science.orchestrator.uploads import UserUploads
    from sandbox_science.workspace.api import Timeouts

    client = await workspace(aiohttp_client, state_dir, static_dir, timeouts=Timeouts(upload=0.05))
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    create = UserUploads.create
    calls = 0

    def held(self: UserUploads, *args: Any) -> UserUpload:
        nonlocal calls
        calls += 1
        if calls == 1:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2)
        return create(self, *args)

    monkeypatch.setattr(UserUploads, "create", held)
    first = asyncio.create_task(
        client.post(UPLOADS, json=body(b"first", identity="first"), headers=origin(client))
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        # The request deadline expires while the actual disk writer still owns memory.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(first), 0.15)
        second = await client.post(
            UPLOADS, json=body(b"second", identity="second"), headers=origin(client)
        )
        assert second.status == 503
        assert calls == 1
        release.set()
        assert (await first).status == 503
        # The committed original can be recovered by the same retry identity.
        replay = await client.post(
            UPLOADS, json=body(b"first", identity="first"), headers=origin(client)
        )
        assert replay.status == 200
        assert len((await (await client.get(UPLOADS)).json())["uploads"]) == 1
    finally:
        release.set()
        await first


async def test_cancelled_binary_admission_disposes_returned_writer(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    from sandbox_science.orchestrator.uploads import UserUploads
    from sandbox_science.workspace import uploads
    from sandbox_science.workspace.api import Timeouts

    route = uploads.uploaded
    first_request = True

    async def timed_admission(request: web.Request) -> web.Response:
        nonlocal first_request
        if first_request:
            first_request = False
            async with asyncio.timeout(0.05):
                return await route(request)
        return await route(request)

    monkeypatch.setattr(uploads, "uploaded", timed_admission)
    client = await workspace(aiohttp_client, state_dir, static_dir, timeouts=Timeouts(upload=5))
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    begin = UserUploads.begin

    def delayed(self: UserUploads, *args: Any, **kwargs: Any) -> Any:
        writer = begin(self, *args, **kwargs)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return writer

    monkeypatch.setattr(UserUploads, "begin", delayed)
    first = asyncio.create_task(
        client.post(UPLOADS, data=b"first", headers=binary_headers(client, "cancelled"))
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        # The deadline cannot detach the acquired scratch/lock/admission owner.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(first), 0.15)
        release.set()
        assert (await first).status == 503
    finally:
        release.set()
        await first
    monkeypatch.setattr(UserUploads, "begin", begin)
    # The exact same ID and bytes can be retried; no leaked live transfer owns it.
    retried = await client.post(UPLOADS, data=b"first", headers=binary_headers(client, "cancelled"))
    assert retried.status == 200, await retried.text()


async def test_original_download_streams_without_convenience_body_read(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandbox_science.orchestrator.uploads import UserUploads

    client = await workspace(aiohttp_client, state_dir, static_dir)
    raw = b"original" * (256 * 1024)
    uploaded = await client.post(UPLOADS, data=raw, headers=binary_headers(client))
    assert uploaded.status == 200

    def materialize(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("HTTP download must not materialize a full upload")

    monkeypatch.setattr(UserUploads, "content", materialize)
    response = await client.get(UPLOADS + "/binary/content")
    assert response.status == 200
    digest = hashlib.sha256()
    async for chunk in response.content.iter_chunked(192 * 1024):
        digest.update(chunk)
    assert digest.hexdigest() == hashlib.sha256(raw).hexdigest()
    assert response.headers["X-Content-SHA256"] == digest.hexdigest()
    head = await client.head(UPLOADS + "/binary/content")
    assert head.status == 200
    assert head.content_length == len(raw)
    assert await head.read() == b""


async def test_cancelled_download_acquisition_closes_returned_spool(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import tempfile
    import threading
    from collections.abc import Generator
    from contextlib import contextmanager
    from typing import BinaryIO

    from aiohttp import ClientConnectionError

    from sandbox_science.orchestrator.uploads import UserUploads
    from sandbox_science.workspace import uploads
    from sandbox_science.workspace.api import Timeouts

    task: asyncio.Task[Any] | None = None
    route = uploads.downloaded

    async def tracked(request: web.Request) -> web.StreamResponse:
        nonlocal task
        if task is None:
            task = asyncio.current_task()
            async with asyncio.timeout(0.05):
                return await route(request)
        return await route(request)

    monkeypatch.setattr(uploads, "downloaded", tracked)
    client = await workspace(aiohttp_client, state_dir, static_dir, timeouts=Timeouts(upload=5))
    assert (await client.post(UPLOADS, json=body(), headers=origin(client))).status == 200
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = UserUploads.open_content
    opened: list[BinaryIO] = []

    @contextmanager
    def delayed(self: UserUploads, *args: Any) -> Generator[BinaryIO]:
        with original(self, *args) as source, tempfile.TemporaryFile(mode="w+b") as stream:
            stream.write(source.read())
            stream.seek(0)
            opened.append(stream)
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            yield stream

    monkeypatch.setattr(UserUploads, "open_content", delayed)
    first = asyncio.create_task(client.get(UPLOADS + "/upload-test/content"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        # Deadline cancellation must wait for the actual acquired resource.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(first), 0.15)
        assert not opened[0].closed
        assert task is not None
        task.cancel()  # repeated cancellation still cannot detach acquisition
        release.set()
        try:
            response = await first
        except ClientConnectionError:
            pass
        else:
            # aiohttp may retry the disconnected idempotent GET once.
            assert response.status == 200
            assert await response.read() == b"# Notes\n\n$E = mc^2$"
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    assert opened[0].closed
    monkeypatch.setattr(UserUploads, "open_content", original)
    second = await client.get(UPLOADS + "/upload-test/content")
    assert second.status == 200
    assert await second.read() == b"# Notes\n\n$E = mc^2$"


async def test_chunked_upload_enforces_limit_without_content_length(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import AsyncIterator

    from sandbox_science.workspace import uploads

    client = await workspace(aiohttp_client, state_dir, static_dir)
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 4096)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"x" * 1024

    response = await client.post(UPLOADS, data=chunks(), headers=binary_headers(client))
    assert response.status == 413
    assert (await (await client.get(UPLOADS)).json())["uploads"] == []
    retry = await client.post(UPLOADS, data=b"complete", headers=binary_headers(client))
    assert retry.status == 200, await retry.text()
