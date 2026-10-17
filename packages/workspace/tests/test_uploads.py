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
