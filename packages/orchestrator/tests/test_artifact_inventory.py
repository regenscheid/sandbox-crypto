# SPDX-License-Identifier: MIT
"""Immutable artifact identity, retention, provenance and atomic replay boundaries."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import ArtifactRef, ExecutionContext
from sandbox_science.contracts.artifact_inventory import (
    ArtifactLink,
    ArtifactPublicationSource,
    ArtifactSnapshot,
)
from sandbox_science.contracts.model import MAX_COUNTER
from sandbox_science.orchestrator.artifacts import ArtifactInventory, ArtifactLimits
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
SOURCE = ArtifactPublicationSource(
    context=CONTEXT,
    run_id="run1",
    tool_call="call1",
    alias="main",
    model="actual-model",
    worker_endpoint_sha256="a" * 64,
)


def snapshot(
    data: bytes, *, path: str = "out.bin", project: str = "p1", id: str = "art-worker"
) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=id,
        project_id=project,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        media_type="application/octet-stream",
        source_path=path,
        produced_by="attempt-unproven",
        created_at=datetime.now(UTC),
    )


def record(
    inventory: ArtifactInventory,
    operation: str,
    data: bytes = b"first",
    *,
    source: ArtifactPublicationSource = SOURCE,
    path: str = "out.bin",
):
    worker = snapshot(data, path=path, project=source.context.project_id, id=f"worker-{operation}")
    inventory.begin(source, operation, path, None)
    inventory.pin(source, operation, path, None, worker)
    return inventory.finish(source, operation, path, None, worker, data)


def test_stable_path_revision_reproduction_and_pinned_latest(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    first = record(inventory, "op1")
    same = record(inventory, "op2")
    changed = record(inventory, "op3", b"changed")
    assert first.revision
    assert changed.revision
    assert same.reproduced
    assert same.revision == first.revision
    assert changed.revision.artifact_id == first.revision.artifact_id
    assert changed.revision.revision == 2
    pinned = ArtifactLink(
        artifact_id=first.revision.artifact_id,
        project_id="p1",
        mode="pinned",
        revision=1,
        sha256=first.revision.sha256,
    )
    latest = pinned.model_copy(update={"mode": "latest", "revision": None, "sha256": None})
    assert inventory.resolve("p1", pinned).reference == first.revision
    revision = inventory.resolve("p1", latest)
    assert revision.reference == changed.revision
    assert revision.parent == 1
    assert revision.path == "sessions/s1/out.bin"
    assert revision.publication.kind == "agent_publication"
    assert revision.publication.model == "actual-model"
    assert revision.publication.producer == "unknown"
    assert revision.publication.observed_inputs == "unknown"
    assert revision.worker_last_writer_hint == "attempt-unproven"
    assert inventory.content("p1", first.revision) == b"first"
    assert inventory.content("p1", changed.revision) == b"changed"
    events = inventory.events("p1")
    assert [e.kind for e in events.entries] == [
        "artifact.revision_created",
        "artifact.reproduced",
        "artifact.revision_created",
    ]
    documents.close()
    reopened = ArtifactInventory(DocumentStore(documents.path))
    assert reopened.resolve("p1", pinned).reference == first.revision
    replay = reopened.begin(SOURCE, "op3", "out.bin", None)
    assert replay.receipt == changed
    assert len(reopened.events("p1").entries) == 3
    reopened.documents.close()


def test_project_and_session_namespaces_and_shared_identity(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    other_session = SOURCE.model_copy(
        update={"context": CONTEXT.model_copy(update={"session_id": "s2"})}
    )
    other_project = SOURCE.model_copy(
        update={"context": CONTEXT.model_copy(update={"project_id": "p2"})}
    )
    one = record(inventory, "op1")
    two = record(inventory, "op1", source=other_session)
    three = record(inventory, "op1", source=other_project)
    assert one.revision
    assert two.revision
    assert three.revision
    assert (
        len({one.revision.artifact_id, two.revision.artifact_id, three.revision.artifact_id}) == 3
    )
    shared1 = record(inventory, "op2", path="shared/result")
    shared2 = record(inventory, "op2", source=other_session, path="shared/result")
    assert shared1.revision == shared2.revision
    assert shared2.reproduced
    assert one.revision
    with pytest.raises(DocumentError, match="project"):
        inventory.content("p2", one.revision)
    link = ArtifactLink(artifact_id=one.revision.artifact_id, project_id="p1", mode="latest")
    with pytest.raises(DocumentError, match="project"):
        inventory.resolve("p2", link)
    assert len(inventory.list("p1").entries) == 3
    documents.close()


def test_unknown_intent_and_pinned_transfer_survive_restart(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    assert inventory.begin(SOURCE, "unknown", "out.bin", None).fresh
    inventory.begin(SOURCE, "known", "out.bin", None)
    worker = snapshot(b"old bytes")
    inventory.pin(SOURCE, "known", "out.bin", None, worker)
    documents.close()
    documents = DocumentStore(documents.path)
    inventory = ArtifactInventory(documents)
    assert not inventory.begin(SOURCE, "unknown", "out.bin", None).fresh
    assert inventory.begin(SOURCE, "known", "out.bin", None).worker == worker
    for changed in (
        SOURCE.model_copy(update={"worker_endpoint_sha256": "b" * 64}),
        SOURCE.model_copy(update={"model": "another-model"}),
    ):
        with pytest.raises(DocumentError, match="another request"):
            inventory.begin(changed, "known", "out.bin", None)
    with pytest.raises(DocumentError, match="another request"):
        inventory.begin(SOURCE, "known", "another.bin", None)
    documents.close()


def test_atomic_failure_retains_pinned_worker_and_no_partial_revision(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    worker = snapshot(b"value")
    inventory.begin(SOURCE, "op1", "out.bin", None)
    inventory.pin(SOURCE, "op1", "out.bin", None, worker)
    with sqlite3.connect(documents.path) as db:
        db.execute(
            "CREATE TRIGGER fail_ack BEFORE UPDATE ON artifact_operations "
            "WHEN NEW.state='done' BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        inventory.finish(SOURCE, "op1", "out.bin", None, worker, b"value")
    assert inventory.list("p1").entries == ()
    assert inventory.events("p1").entries == ()
    assert inventory.begin(SOURCE, "op1", "out.bin", None).worker == worker
    with sqlite3.connect(documents.path) as db:
        db.execute("DROP TRIGGER fail_ack")
    complete = inventory.finish(SOURCE, "op1", "out.bin", None, worker, b"value")
    assert complete.revision
    assert inventory.content("p1", complete.revision) == b"value"
    documents.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_bytes", 1),
        ("project_bytes", 1),
        ("total_bytes", 1),
        ("project_revisions", 1),
        ("total_revisions", 1),
    ],
)
def test_quota_refusal_retains_worker_and_previous_revision(
    tmp_path: Path, field: str, value: int
) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    first = record(inventory, "op1", b"a")
    worker = snapshot(b"bb")
    inventory.begin(SOURCE, "op2", "out.bin", None)
    inventory.pin(SOURCE, "op2", "out.bin", None, worker)
    inventory.limits = ArtifactLimits.model_validate({field: value})
    with pytest.raises(DocumentError, match="quota"):
        inventory.finish(SOURCE, "op2", "out.bin", None, worker, b"bb")
    assert inventory.begin(SOURCE, "op2", "out.bin", None).worker == worker
    assert inventory.list("p1").entries[0].reference == first.revision
    assert len(inventory.events("p1").entries) == 1
    documents.close()


@pytest.mark.parametrize("field", ["project_operations", "total_operations"])
def test_unknown_zero_byte_intents_count_towards_retention(tmp_path: Path, field: str) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents, limits=ArtifactLimits.model_validate({field: 1}))
    inventory.begin(SOURCE, "unknown", "empty", None)
    with pytest.raises(DocumentError, match="quota"):
        inventory.begin(SOURCE, "second", "empty", None)
    assert not inventory.begin(SOURCE, "unknown", "empty", None).fresh
    documents.close()


def test_metadata_reservation_and_no_unresolved_eviction(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents, limits=ArtifactLimits(metadata_bytes=45 * 1024))
    inventory.begin(SOURCE, "unknown", "empty", None)
    with pytest.raises(DocumentError, match="metadata"):
        inventory.begin(SOURCE, "second", "empty", None)
    assert inventory.begin(SOURCE, "unknown", "empty", None).state == "intent"
    documents.close()


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "p2"},
        {"source_path": "other"},
        {"size": True},
        {"size": 1.0},
        {"sha256": "../bad"},
    ],
)
def test_forged_or_weak_legacy_metadata_never_creates_revision(
    tmp_path: Path, change: dict[str, object]
) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    worker = snapshot(b"a").model_copy(update=change)
    inventory.begin(SOURCE, "op", "out.bin", None)
    inventory.pin(SOURCE, "op", "out.bin", None, worker)
    with pytest.raises((DocumentError, ValidationError)):
        inventory.finish(SOURCE, "op", "out.bin", None, worker, b"a")
    assert not inventory.list("p1").entries
    documents.close()


@pytest.mark.parametrize(
    "path", ["../x", "/etc/passwd", "a//b", "a/./b", "a/../b", "a\x00b", "a\nb"]
)
def test_path_ambiguity_rejected_before_intent(tmp_path: Path, path: str) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    with pytest.raises(ValueError, match="artifact path"):
        inventory.begin(SOURCE, "op", path, None)
    documents.close()


def test_exact_retained_bytes_and_immutable_rows(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    receipt = record(inventory, "op")
    assert receipt.revision
    with sqlite3.connect(documents.path) as db:
        for statement in ("DELETE FROM artifact_revisions", "DELETE FROM artifact_events"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)
        db.execute("DROP TRIGGER artifact_revisions_no_update")
        db.execute("UPDATE artifact_revisions SET data=?", (b"altered",))
    with pytest.raises(DocumentError, match="integrity"):
        inventory.content("p1", receipt.revision)
    documents.close()


def test_bounded_pages_and_zero_byte_revision_count(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    for index in range(13):
        record(inventory, f"op{index}", b"", path=f"file{index}")
    page = inventory.list("p1")
    assert len(page.entries) == 10
    assert page.next_after
    rest = inventory.list("p1", after=page.next_after)
    assert len(rest.entries) == 3
    assert rest.next_after is None
    first_events = inventory.events("p1")
    assert len(first_events.entries) == 10
    assert len(inventory.events("p1", after=first_events.next_after).entries) == 3
    documents.close()


def test_user_impersonation_and_weak_counters_refused(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    user = SOURCE.model_copy(update={"context": CONTEXT.model_copy(update={"actor": "USER"})})
    with pytest.raises(DocumentError, match="impersonate"):
        inventory.begin(user, "op", "file", None)
    for value in (True, 1.0, -1):
        with pytest.raises(ValidationError):
            ArtifactSnapshot.model_validate({**snapshot(b"x").model_dump(), "size": value})
    documents.close()


def test_replay_receipt_cannot_substitute_another_valid_revision(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    first = record(inventory, "op1", b"first")
    second = record(inventory, "op2", b"changed")
    assert second.revision
    corrupted = first.model_copy(update={"revision": second.revision})
    with sqlite3.connect(documents.path) as db:
        db.execute(
            "UPDATE artifact_operations SET receipt=? WHERE operation='op1'",
            (corrupted.model_dump_json(),),
        )
    with pytest.raises(DocumentError, match="acknowledgement identity"):
        inventory.begin(SOURCE, "op1", "out.bin", None)
    documents.close()


@pytest.mark.parametrize("column", ["request", "worker", "receipt"])
@pytest.mark.parametrize("value", ["x" * (20 * 1024 * 1024), "not json"])
def test_oversized_or_malformed_operation_metadata_is_refused_when_written(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    record(inventory, "op1")
    refused = pytest.raises(sqlite3.IntegrityError, match="CHECK")
    with refused, documents.control.write() as tx:
        tx.sql.execute(f"UPDATE artifact_operations SET {column}=?", (value,))
    with pytest.raises(DocumentError, match="already used"):
        inventory.begin(SOURCE, "op1", "other.bin", None)
    documents.close()


def test_latest_hold_observation_survives_restart_and_preserves_transfer(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    worker = snapshot(b"retained")
    inventory.begin(SOURCE, "op1", "out.bin", None)
    inventory.pin(SOURCE, "op1", "out.bin", None, worker)
    held = inventory.hold(SOURCE, "op1", "out.bin", None, "hash mismatch")
    assert held.state == "held"
    documents.close()
    documents = DocumentStore(documents.path)
    inventory = ArtifactInventory(documents)
    operation = inventory.begin(SOURCE, "op1", "out.bin", None)
    assert operation.state == "transfer"
    assert operation.worker == worker
    assert operation.receipt == held
    complete = inventory.finish(SOURCE, "op1", "out.bin", None, worker, b"retained")
    assert complete.state == "recorded"
    documents.close()


@pytest.mark.parametrize("after", [-1, MAX_COUNTER + 1, 2**63 - 1])
def test_an_event_cursor_past_the_counter_bound_is_an_invalid_request(
    tmp_path: Path, after: int
) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    with pytest.raises(DocumentError, match="invalid artifact event cursor"):
        inventory.events("p1", after=after)
    assert inventory.events("p1", after=MAX_COUNTER).entries == ()
    documents.close()
