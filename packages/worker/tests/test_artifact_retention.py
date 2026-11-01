# SPDX-License-Identifier: MIT
"""Durable ACK identity, conservative shared-hash pins and offline crash recovery."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from pydantic import ValidationError

from sandbox_science.contracts.artifact_inventory import ArtifactRevisionRef, ArtifactSnapshot
from sandbox_science.contracts.artifact_retention import (
    MAX_RETENTION_ACK_BYTES,
    ArtifactRetentionAcknowledgement,
)
from sandbox_science.worker import schema
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifact_retention import MAX_HASH_REFERENCES
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.cli import main
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.state_lock import StateLock
from sandbox_science.worker.store import Store

POOL = "test-secret-retention-pool-token-000000"
TOKENS = {name: f"test-secret-retention-{name}-000000" for name in ("control", "ordinary")}
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


def snapshot(
    name: str = "art-one", *, project: str = "p", content: bytes = b"hello"
) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        artifact_id=name,
        project_id=project,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        media_type="text/plain",
        source_path="result.txt",
        produced_by="attempt-one",
        created_at=datetime(2026, 10, 20, tzinfo=UTC),
    )


def acknowledgement(
    value: ArtifactSnapshot, name: str = "retain-one"
) -> ArtifactRetentionAcknowledgement:
    return ArtifactRetentionAcknowledgement(
        acknowledgement_id=name,
        snapshot=value,
        revision=ArtifactRevisionRef(
            artifact_id="control-one",
            project_id=value.project_id,
            revision=1,
            sha256=value.sha256,
            size=value.size,
            media_type=value.media_type,
        ),
    )


def install(blobs: Blobs, content: bytes = b"hello") -> Path:
    writer = blobs.writer()
    writer.write(content)
    digest = hashlib.sha256(content).hexdigest()
    assert writer.commit(digest)
    return blobs.path(digest)


@pytest.fixture
def state(tmp_path: Path) -> Any:
    store = Store(tmp_path / "worker.sqlite3")
    blobs = Blobs(tmp_path / "artifacts")
    try:
        yield store, blobs
    finally:
        store.close()


@pytest.mark.parametrize("field", ["project_id", "sha256", "size", "media_type"])
def test_acknowledgement_rejects_disagreeing_control_receipt(field: str) -> None:
    body = acknowledgement(snapshot()).model_dump(mode="json")
    body["revision"][field] = {
        "project_id": "other",
        "sha256": "ab" * 32,
        "size": 9,
        "media_type": "image/png",
    }[field]
    with pytest.raises(ValidationError, match="disagree"):
        ArtifactRetentionAcknowledgement.model_validate(body)


def test_acknowledgement_requires_every_original_field_and_immutable_id(state) -> None:
    store, _ = state
    original = snapshot()
    store.insert_artifact(original)
    retain = store.artifact_retention
    ack = acknowledgement(original)
    assert retain.acknowledge(ack) == retain.acknowledge(ack) == ack
    for field, replacement in (
        ("source_path", "other.txt"),
        ("produced_by", None),
        ("created_at", datetime(2025, 1, 1, tzinfo=UTC)),
    ):
        changed = acknowledgement(original.model_copy(update={field: replacement}), "new-id")
        with pytest.raises(WorkerError, match="differs from original"):
            retain.acknowledge(changed)
    changed = ack.model_copy(update={"revision": ack.revision.model_copy(update={"revision": 2})})
    with pytest.raises(WorkerError, match="identity already used"):
        retain.acknowledge(changed)
    rows = retain.db.execute("SELECT record FROM artifact_retention_acknowledgements").fetchall()
    assert rows == [(ack.model_dump_json(),)]


def test_dry_run_releases_only_referenced_blobs_and_keeps_readable_metadata(state) -> None:
    store, blobs = state
    value = snapshot()
    store.insert_artifact(value)
    path = install(blobs)
    orphan = install(blobs, b"orphan")
    temporary = blobs.root / "tmp" / "capture-helper"
    temporary.write_text("leave me")
    retain = store.artifact_retention
    assert retain.release_page(blobs).entries[0].status == "kept"
    retain.acknowledge(acknowledgement(value))
    assert retain.release_page(blobs).entries[0].status == "would_release"
    assert retain.db.execute("SELECT count(*) FROM artifact_release_intents").fetchone() == (0,)
    assert path.exists()
    assert retain.release_page(blobs, apply=True).entries[0].status == "released"
    metadata = store.artifact(value.artifact_id)
    assert metadata is not None
    assert metadata.model_dump() == value.model_dump()
    assert not path.exists()
    assert orphan.read_bytes() == b"orphan"
    assert temporary.read_text() == "leave me"
    assert retain.release_page(blobs, apply=True).entries[0].status == "missing"
    assert retain.db.execute("SELECT count(*) FROM artifact_release_intents").fetchone() == (1,)


def test_all_cross_project_references_need_exact_ack_and_new_capture_rechecks(state) -> None:
    store, blobs = state
    first, second = snapshot(), snapshot("art-two", project="other")
    for value in (first, second):
        store.insert_artifact(value)
    path = install(blobs)
    retain = store.artifact_retention
    retain.acknowledge(acknowledgement(first))
    assert retain.release_page(blobs, apply=True).entries[0].status == "kept"
    retain.acknowledge(acknowledgement(second, "retain-two"))
    assert retain.release_page(blobs, apply=True).entries[0].status == "released"
    assert not path.exists()
    third = snapshot("art-three")
    store.insert_artifact(third)
    install(blobs)
    assert retain.release_page(blobs, apply=True).entries[0].status == "kept"
    assert path.exists()
    retain.acknowledge(acknowledgement(third, "retain-three"))
    assert retain.release_page(blobs, apply=True).entries[0].status == "released"
    rows = retain.db.execute("SELECT settled_at FROM artifact_release_intents").fetchall()
    assert len(rows) == 2
    assert all(row[0] for row in rows)


@pytest.mark.parametrize("installer", ["id", "media"])
def test_installer_references_pin_shared_bytes_even_with_ack(state, installer: str) -> None:
    store, blobs = state
    first = snapshot()
    other = snapshot("art-inst-" + "a" * 32 if installer == "id" else "art-log")
    if installer == "media":
        other = other.model_copy(
            update={"media_type": "application/vnd.sandbox-science.installer-log+json"}
        )
    retain = store.artifact_retention
    for index, value in enumerate((first, other)):
        store.insert_artifact(value)
        retain.acknowledge(acknowledgement(value, f"retain-{index}"))
    path = install(blobs)
    result = retain.release_page(blobs, apply=True).entries[0]
    assert result.status == "kept"
    assert "installer" in result.reason
    assert path.exists()


@pytest.mark.parametrize(
    "corruption", ["json", "oversize", "reference", "identity", "ack", "hash_nul", "key"]
)
def test_corrupt_or_ambiguous_shared_references_pin_bytes(state, corruption: str) -> None:
    store, blobs = state
    value = snapshot()
    retain = store.artifact_retention
    store.insert_artifact(value)
    retain.acknowledge(acknowledgement(value))
    if corruption == "ack":
        retain.db.execute("UPDATE artifact_retention_acknowledgements SET record='{}'")
    else:
        record = {
            "json": "{broken",
            "oversize": " " * (MAX_RETENTION_ACK_BYTES + 1),
            "reference": snapshot("art-bad").model_dump_json().replace('"size":5', '"size":"bad"'),
            "identity": snapshot("art-wrong").model_dump_json(),
            "hash_nul": snapshot("art-bad")
            .model_dump_json()
            .replace(value.sha256, value.sha256 + "\\u0000suffix"),
            "key": snapshot("art-bad").model_dump_json(),
        }[corruption]
        key = "a" * 129 if corruption == "key" else "art-bad"
        retain.db.execute("INSERT INTO artifacts VALUES(?,?)", (key, record))
    path = install(blobs)
    assert retain.release_page(blobs, apply=True).entries[0].status == "kept"
    assert path.exists()


@pytest.mark.parametrize("replacement", ["symlink", "wrong_size"])
def test_nonregular_or_wrong_size_blob_preserves_bytes(
    state, tmp_path: Path, replacement: str
) -> None:
    store, blobs = state
    value = snapshot()
    store.insert_artifact(value)
    store.artifact_retention.acknowledge(acknowledgement(value))
    path = install(blobs)
    if replacement == "symlink":
        source = tmp_path / "mutable-workspace.txt"
        source.write_bytes(b"hello")
        path.unlink()
        path.symlink_to(source)
    else:
        path.write_bytes(b"larger blob")
    assert store.artifact_retention.release_page(blobs, apply=True).entries[0].status == "kept"
    assert path.exists()


@pytest.mark.parametrize("corruption", ["oversize", "id", "artifact", "digest", "record_id"])
def test_invalid_prior_ack_conflicts_on_retry_and_preserves_replica(state, corruption: str) -> None:
    store, blobs = state
    value = snapshot()
    retain = store.artifact_retention
    store.insert_artifact(value)
    ack = acknowledgement(value)
    retain.acknowledge(ack)
    table = "artifact_retention_acknowledgements"
    if corruption == "oversize":
        retain.db.execute("PRAGMA ignore_check_constraints=ON")
        try:
            retain.db.execute(
                f"UPDATE {table} SET record=?", (" " * (MAX_RETENTION_ACK_BYTES + 1),)
            )
        finally:
            retain.db.execute("PRAGMA ignore_check_constraints=OFF")
    elif corruption == "record_id":
        changed = ack.model_copy(update={"acknowledgement_id": "different"})
        retain.db.execute(f"UPDATE {table} SET record=?", (changed.model_dump_json(),))
    else:
        field, replacement = {
            "id": ("acknowledgement_id", "a" * 129),
            "artifact": ("artifact_id", "wrong-artifact"),
            "digest": ("snapshot_digest", "b" * 64),
        }[corruption]
        retain.db.execute(f"UPDATE {table} SET {field}=?", (replacement,))
    path = install(blobs)
    replay = ack.model_copy(update={"acknowledgement_id": "a" * 129}) if corruption == "id" else ack
    with pytest.raises(WorkerError, match="invalid recorded acknowledgement"):
        retain.acknowledge(replay)
    assert retain.release_page(blobs, apply=True).entries[0].status == "kept"
    assert path.exists()
    assert retain.db.execute("SELECT count(*) FROM artifact_release_intents").fetchone() == (0,)


@pytest.mark.parametrize("failure", ["unlink", "fsync", "settle"])
def test_release_intent_survives_each_crash_boundary_and_reconciles(
    state, monkeypatch, tmp_path: Path, failure: str
) -> None:
    store, blobs = state
    value = snapshot()
    store.insert_artifact(value)
    retain = store.artifact_retention
    retain.acknowledge(acknowledgement(value))
    path = install(blobs)
    with monkeypatch.context() as patch:
        if failure == "unlink":

            def broken_unlink(self: Path, *args: object, **kwargs: object) -> None:
                raise OSError("injected unlink failure")

            patch.setattr(Path, "unlink", broken_unlink)
        elif failure == "fsync":

            def broken_fsync(fd: int) -> None:
                raise OSError("injected fsync failure")

            patch.setattr(os, "fsync", broken_fsync)
        else:
            retain.db.execute(
                "CREATE TRIGGER fail_settlement BEFORE UPDATE ON artifact_release_intents"
                " BEGIN SELECT RAISE(ABORT,'injected settlement failure'); END"
            )
        with pytest.raises((OSError, sqlite3.IntegrityError), match="injected"):
            retain.release_page(blobs, apply=True)
    rows = retain.db.execute(
        "SELECT sha256,size,settled_at FROM artifact_release_intents"
    ).fetchall()
    assert rows == [(value.sha256, value.size, None)]
    assert path.exists() == (failure == "unlink")
    store.close()
    reopened = Store(tmp_path / "worker.sqlite3")
    try:
        retain = reopened.artifact_retention
        retain.db.execute("DROP TRIGGER IF EXISTS fail_settlement")
        result = retain.release_page(blobs, apply=True).entries[0]
        assert result.status == ("released" if failure == "unlink" else "reconciled")
        assert not path.exists()
        assert retain.db.execute("SELECT count(*) FROM artifact_release_intents").fetchone() == (1,)
        assert retain.original(value.artifact_id) == value
    finally:
        reopened.close()


def test_pending_release_rechecks_new_reference_after_failed_unlink(state, monkeypatch) -> None:
    store, blobs = state
    first = snapshot()
    store.insert_artifact(first)
    retain = store.artifact_retention
    retain.acknowledge(acknowledgement(first))
    path = install(blobs)
    with monkeypatch.context() as patch:

        def broken_unlink(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("injected")

        patch.setattr(Path, "unlink", broken_unlink)
        with pytest.raises(OSError, match="injected"):
            retain.release_page(blobs, apply=True)
    store.insert_artifact(snapshot("art-later"))
    assert retain.release_page(blobs, apply=True).entries[0].status == "kept"
    assert path.exists()


def test_page_cursors_and_reference_bounds_fail_closed(state) -> None:
    store, blobs = state
    retain = store.artifact_retention
    for index in range(3):
        value = snapshot(f"art-{index}", content=str(index).encode())
        store.insert_artifact(value)
    first = retain.release_page(blobs, limit=2)
    second = retain.release_page(blobs, limit=2, after=first.next_after)
    assert len(first.entries) == 2
    assert len(second.entries) == 1
    assert second.next_after is None
    assert first.entries[-1].sha256 < second.entries[0].sha256
    for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"after": "../outside"}):
        with pytest.raises(ValueError, match="invalid release"):
            retain.release_page(blobs, **kwargs)
    with retain.db:
        retain.db.execute("BEGIN")
        for index in range(MAX_HASH_REFERENCES + 1):
            store.insert_artifact(snapshot(f"art-duplicate-{index}"))
    path = install(blobs)
    page = retain.release_page(blobs, apply=True, limit=100)
    result = next(entry for entry in page.entries if entry.sha256 == snapshot().sha256)
    assert result.status == "kept"
    assert "count" in result.reason
    assert path.exists()


def test_cli_defaults_to_dry_run_and_obeys_state_lock(state, tmp_path: Path, capsys) -> None:
    store, blobs = state
    value = snapshot()
    store.insert_artifact(value)
    store.artifact_retention.acknowledge(acknowledgement(value))
    path = install(blobs)
    settings = tmp_path / "worker.toml"
    settings.write_text(f'token_file = "unused.token"\nstate_dir = "{tmp_path}"\n[local]\n')
    args = ["artifact-retention", "--settings", str(settings)]
    assert main(args) == 0
    assert path.exists()
    assert "would_release" in capsys.readouterr().out
    with StateLock(tmp_path):
        assert main([*args, "--apply"]) == 1
        assert path.exists()
    assert "another worker" in capsys.readouterr().err
    assert main([*args, "--apply"]) == 0
    assert not path.exists()


def test_upgrade_keeps_legacy_metadata_and_never_backfills_acknowledgements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "worker.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[: 6 - schema.BASELINE])
        old = Store(path)
        try:
            old.insert_artifact(snapshot())
        finally:
            old.close()
    upgraded = Store(path)
    try:
        assert upgraded.artifact_retention.original(snapshot().artifact_id) == snapshot()
        assert upgraded.artifact_retention.db.execute(
            "SELECT count(*) FROM artifact_retention_acknowledgements"
        ).fetchone() == (0,)
        blobs = Blobs(tmp_path / "artifacts")
        blob = install(blobs)
        assert (
            upgraded.artifact_retention.release_page(blobs, apply=True).entries[0].status == "kept"
        )
        assert blob.exists()
    finally:
        upgraded.close()


@pytest_asyncio.fixture
async def api(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> AsyncIterator[tuple[TestClient[Any, Any], Store]]:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(), tmp_path),
        Blobs(tmp_path / "artifacts"),
        pool_id="test",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    environments = cast(
        Environments,
        SimpleNamespace(
            deleted=[],
            required_deleted=[],
            required_deleting=[],
            confirmed_deleted=[],
            reusing=[],
        ),
    )
    clients = [
        ClientSettings(
            name=name,
            token_sha256=token_digest(token),
            projects=("p",),
            artifact_retention=name == "control",
        )
        for name, token in TOKENS.items()
    ]
    client = await aiohttp_client(create_app(manager, POOL, environments, clients))
    try:
        yield client, store
    finally:
        await client.close()
        await manager.close()
        store.close()


async def test_api_requires_permission_scope_exact_snapshot_and_echoes_retry(api) -> None:
    client, store = api
    value = snapshot()
    store.insert_artifact(value)
    body = acknowledgement(value).model_dump(mode="json")
    route = f"/v1/artifacts/{value.artifact_id}/retention?project_id=p"
    assert (await client.post(route, json=body)).status == 401
    for token in (TOKENS["ordinary"], TOKENS["control"], POOL):
        response = await client.post(route, json=body, headers={"Authorization": f"Bearer {token}"})
        assert response.status == (403 if token == TOKENS["ordinary"] else 200)
        if response.status == 200:
            assert await response.json() == body
    headers = {"Authorization": f"Bearer {TOKENS['control']}"}
    other = snapshot("art-other", project="other")
    store.insert_artifact(other)
    response = await client.post(
        f"/v1/artifacts/{other.artifact_id}/retention",
        json=acknowledgement(other).model_dump(mode="json"),
        headers=headers,
    )
    assert response.status == 404
    response = await client.post(
        route,
        json=acknowledgement(snapshot("art-different")).model_dump(mode="json"),
        headers=headers,
    )
    assert response.status == 409
    body["snapshot"]["source_path"] = "changed.txt"
    assert (await client.post(route, json=body, headers=headers)).status == 409
    assert (await client.get(f"/v1/artifacts/{value.artifact_id}", headers=headers)).status == 200
    assert (
        await client.get(f"/v1/artifacts/{value.artifact_id}/content", headers=headers)
    ).status == 404


async def test_api_bounds_chunked_body_before_decoding(api) -> None:
    client, store = api
    value = snapshot()
    store.insert_artifact(value)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(20):
            yield b" " * 1024

    response = await client.post(
        f"/v1/artifacts/{value.artifact_id}/retention",
        data=chunks(),
        headers={"Authorization": f"Bearer {TOKENS['control']}"},
    )
    assert response.status == 400
    assert store.artifact_retention.db.execute(
        "SELECT count(*) FROM artifact_retention_acknowledgements"
    ).fetchone() == (0,)


@pytest.mark.parametrize("record", [" " * (MAX_RETENTION_ACK_BYTES + 1), "{invalid", "{}"])
async def test_api_rejects_invalid_original_metadata_with_no_ack(api, record: str) -> None:
    client, store = api
    value = snapshot()
    store.artifact_retention.db.execute(
        "INSERT INTO artifacts VALUES(?,?)", (value.artifact_id, record)
    )
    response = await client.post(
        f"/v1/artifacts/{value.artifact_id}/retention",
        json=acknowledgement(value).model_dump(mode="json"),
        headers={"Authorization": f"Bearer {TOKENS['control']}"},
    )
    assert response.status == 409
    assert store.artifact_retention.db.execute(
        "SELECT count(*) FROM artifact_retention_acknowledgements"
    ).fetchone() == (0,)


@pytest.mark.parametrize("value", ["true", 1, None])
def test_client_retention_permission_is_strict_and_defaults_false(value: object) -> None:
    fields = {"name": "control", "token_sha256": "a" * 64, "projects": ["p"]}
    assert not ClientSettings.model_validate(fields).artifact_retention
    with pytest.raises(ValidationError):
        ClientSettings.model_validate({**fields, "artifact_retention": value})
