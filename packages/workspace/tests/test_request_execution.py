# SPDX-License-Identifier: MIT
"""Public route contracts shared by the workspace's request execution paths."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, DocumentError, DocumentStore, store_path
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.api import Timeouts

from .route_support import ClientFactory, origin, workspace


@pytest.mark.parametrize(
    ("route", "owner", "method"),
    [
        ("documents", DocumentStore, "documents"),
        ("ledger", Ledger, "entries"),
        ("sessions/a/reviews", ReviewStore, "listing"),
    ],
)
@pytest.mark.parametrize(
    ("kind", "status", "response_kind", "message"),
    [
        ("invalid_request", 400, "invalid_request", "synthetic refusal"),
        ("policy_denied", 403, "policy_denied", "synthetic refusal"),
        ("not_found", 404, "not_found", "synthetic refusal"),
        ("conflict", 409, "conflict", "synthetic refusal"),
        ("busy", 503, "unavailable", "synthetic refusal"),
        ("internal", 500, "internal", "internal workspace error"),
        ("unrecognized", 500, "internal", "internal workspace error"),
    ],
)
async def test_store_errors_keep_their_status_body_and_details(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    owner: type,
    method: str,
    kind: str,
    status: int,
    response_kind: str,
    message: str,
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)

    def fail(*args: object, **kwargs: object) -> None:
        raise DocumentError(kind, "synthetic refusal", details={"current": 7})

    monkeypatch.setattr(owner, method, fail)
    response = await client.get(f"/api/v0/projects/p1/{route}")
    assert response.status == status
    assert response.headers["Cache-Control"] == "no-store"
    assert await response.json() == {
        "error": {"kind": response_kind, "message": message, "details": {"current": 7}}
    }


async def test_scope_checks_precede_store_work_but_document_names_are_checked_first(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    calls: list[str] = []

    def listing(*args: object, **kwargs: object) -> None:
        calls.append("listing")
        raise AssertionError("an unknown project's store view must not run")

    monkeypatch.setattr(DocumentStore, "documents", listing)
    response = await client.get("/api/v0/projects/missing/documents")
    assert response.status == 404
    assert (await response.json())["error"]["kind"] == "not_found"
    assert calls == []
    response = await client.get("/api/v0/projects/missing/documents/a%20b")
    assert response.status == 400
    assert await response.json() == {
        "error": {
            "kind": "invalid_request",
            "message": "a document is named by its ID or its name",
            "details": {},
        }
    }


async def test_store_reads_use_threads_and_readonly_connections(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    main_thread = threading.get_ident()
    projects, documents = ProjectStore.get, DocumentStore.documents
    calls: list[str] = []

    def project_read(store: ProjectStore, *args: Any, **kwargs: Any) -> Any:
        assert threading.get_ident() != main_thread
        assert store.readonly
        calls.append("project")
        return projects(store, *args, **kwargs)

    def document_read(store: DocumentStore, *args: Any, **kwargs: Any) -> Any:
        assert threading.get_ident() != main_thread
        assert store.readonly
        calls.append("documents")
        return documents(store, *args, **kwargs)

    monkeypatch.setattr(ProjectStore, "get", project_read)
    monkeypatch.setattr(DocumentStore, "documents", document_read)
    for path in (
        "/api/v0/projects/p1",
        "/api/v0/projects/p1/documents",
        "/api/v0/projects/p1/ledger",
    ):
        response = await client.get(path)
        assert response.status == 200, await response.text()
        assert response.headers["Cache-Control"] == "no-store"
    assert "project" in calls
    assert "documents" in calls


async def test_ledger_deadline_does_not_apply_to_document_writes(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DocumentStore(store_path(state_dir))
    document = store.create("p1", name="note", title="Note", text="Before.", author=USER)
    store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir, timeouts=Timeouts(call=0))
    entered, release = threading.Event(), threading.Event()
    edit = DocumentStore.edit_source
    main_thread = threading.get_ident()

    def blocked_write(store: DocumentStore, *args: Any, **kwargs: Any) -> Any:
        assert threading.get_ident() != main_thread
        assert not store.readonly
        assert kwargs["author"] == USER
        entered.set()
        assert release.wait(5), "test must release its writer"
        return edit(store, *args, **kwargs)

    monkeypatch.setattr(DocumentStore, "edit_source", blocked_write)
    pending = asyncio.ensure_future(
        client.post(
            f"/api/v0/projects/p1/documents/{document.document_id}/changes",
            headers=origin(client),
            json={
                "action": "source",
                "base": 1,
                "text": "After.",
                "operation_id": "write-without-deadline",
                "reason": "User edit",
            },
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        response = await client.get("/api/v0/projects/p1/ledger")
        assert response.status == 503
        assert await response.json() == {
            "error": {"kind": "unavailable", "message": "the request timed out"}
        }
        assert not pending.done()
    finally:
        release.set()
        result = await pending
    assert result.status == 200, await result.text()
    response = await client.get(f"/api/v0/projects/p1/documents/{document.document_id}")
    assert response.status == 200
    value = await response.json()
    assert value["text"] == "After."
    assert value["revision"]["author"] == USER.model_dump(mode="json")
