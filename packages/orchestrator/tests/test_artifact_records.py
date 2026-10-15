# SPDX-License-Identifier: MIT
"""Revisions without their bytes, tombstones, recorded-file attribution and the retention
figures (research artifacts, RA-31 to RA-34)."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox_science.contracts.artifact_inventory import (
    ArtifactLink,
    ArtifactRevision,
)
from sandbox_science.orchestrator.artifacts import ArtifactInventory, ArtifactLimits
from sandbox_science.orchestrator.consistency.artifacts import project_artifact
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore

from .control_versions import older_store
from .test_artifact_inventory import CONTEXT, SOURCE, record

RECORDED = SOURCE.model_copy(
    update={
        "kind": "execution_record",
        "role": "delegated",
        "parent_run": "20261014T120000000000Z-root",
        "delegation": "dlg-1",
        "execution_id": "exe-1",
        "attempt_state": "failed",
    }
)
BIG = hashlib.sha256(b"big").hexdigest()


def inventory_at(tmp_path: Path, **limits: int) -> ArtifactInventory:
    return ArtifactInventory(
        DocumentStore(tmp_path / "control.sqlite3"), limits=ArtifactLimits(**limits)
    )


def latest(inventory: ArtifactInventory, artifact: str) -> ArtifactRevision:
    link = ArtifactLink(project_id="p1", artifact_id=artifact, mode="latest")
    return inventory.resolve("p1", link)


def test_a_file_too_large_keeps_its_hash_and_size(tmp_path: Path) -> None:
    inventory = inventory_at(tmp_path)
    reference, reproduced = inventory.record_metadata(
        RECORDED, "data/raw.h5", sha256=BIG, size=2 * 1024**3,
        media_type="application/x-hdf5", note="too_large",
    )  # fmt: skip
    assert not reproduced
    revision = latest(inventory, reference.artifact_id)
    assert (revision.path, revision.reference.size, revision.reference.sha256) == (
        "sessions/s1/data/raw.h5", 2 * 1024**3, BIG,
    )  # fmt: skip
    assert (revision.content_retained, revision.content_note, revision.worker_snapshot) == (
        False, "too_large", None,
    )  # fmt: skip
    assert revision.publication == RECORDED
    with pytest.raises(DocumentError, match="not kept"):
        inventory.content("p1", reference)
    again, reproduced = inventory.record_metadata(
        RECORDED, "data/raw.h5", sha256=BIG, size=2 * 1024**3,
        media_type="application/x-hdf5", note="too_large",
    )  # fmt: skip
    assert (again, reproduced) == (reference, True)
    kinds = [event.kind for event in inventory.events("p1").entries]
    assert kinds == ["artifact.revision_created", "artifact.reproduced"]


def test_a_deleted_file_gets_a_tombstone_once(tmp_path: Path) -> None:
    inventory = inventory_at(tmp_path)
    assert inventory.record_deletion(RECORDED, "notes/draft.md") is None  # never recorded
    first = record(inventory, "op1", b"draft", path="notes/draft.md")
    assert first.revision is not None
    tombstone = inventory.record_deletion(RECORDED, "notes/draft.md")
    assert tombstone is not None
    assert (tombstone.artifact_id, tombstone.revision) == (first.revision.artifact_id, 2)
    assert inventory.record_deletion(RECORDED, "notes/draft.md") is None  # already gone
    revision = latest(inventory, first.revision.artifact_id)
    assert (revision.deleted, revision.content_retained, revision.parent) == (True, False, 1)
    with pytest.raises(DocumentError, match="deleted"):
        inventory.content("p1", tombstone)
    assert inventory.content("p1", first.revision) == b"draft"  # history stays
    # The same content written again is a new revision, not a reproduction of a deleted one.
    back = record(inventory, "op2", b"draft", path="notes/draft.md")
    assert back.revision is not None
    assert (back.revision.revision, back.reproduced) == (3, False)
    kinds = [event.kind for event in inventory.events("p1").entries]
    assert kinds == ["artifact.revision_created", "artifact.deleted", "artifact.revision_created"]


def test_shared_files_are_shared_artifacts(tmp_path: Path) -> None:
    inventory = inventory_at(tmp_path)
    reference, _ = inventory.record_metadata(
        RECORDED, "shared/bibliography.bib", sha256=BIG, size=9, media_type="text/plain",
        note="quota",
    )  # fmt: skip
    assert latest(inventory, reference.artifact_id).path == "shared/bibliography.bib"


def test_no_record_is_attributed_to_the_user(tmp_path: Path) -> None:
    inventory = inventory_at(tmp_path)
    user = RECORDED.model_copy(update={"context": CONTEXT.model_copy(update={"actor": "USER"})})
    with pytest.raises(DocumentError, match="USER"):
        inventory.record_deletion(user, "a.txt")
    with pytest.raises(DocumentError, match="USER"):
        inventory.record_metadata(
            user, "a.txt", sha256=BIG, size=1, media_type="text/plain", note="quota"
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"deleted": True, "content_retained": True},
        {"content_retained": False},  # without saying why
        {"content_note": "quota"},  # bytes kept, and a reason they were not
    ],
)
def test_a_revision_says_consistently_what_it_kept(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    inventory = inventory_at(tmp_path)
    kept = record(inventory, "op1")
    assert kept.revision is not None
    revision = latest(inventory, kept.revision.artifact_id)
    with pytest.raises(ValidationError):
        ArtifactRevision.model_validate({**revision.model_dump(), **fields})


def test_room_follows_the_budget(tmp_path: Path) -> None:
    inventory = inventory_at(tmp_path, project_bytes=10, project_revisions=2)
    assert inventory.room("p1", 5) == "content"
    assert inventory.room("p1", 9 * 1024 * 1024) == "metadata"  # over 8 MiB a revision
    record(inventory, "op1", b"12345678")  # 8 of 10 bytes
    assert inventory.room("p1", 5) == "metadata"
    assert inventory.room("p1", 2) == "content"
    inventory.record_metadata(
        RECORDED, "b.bin", sha256=BIG, size=5, media_type="text/plain", note="quota"
    )
    assert inventory.room("p1", 1) == "full"  # two revisions of two
    assert inventory.room("p2", 1) == "content"  # another project's budget


def usage_rows(path: Path) -> dict[str, tuple[int, int, int, int]]:
    with sqlite3.connect(path) as db:
        return {row[0]: tuple(row[1:]) for row in db.execute("SELECT * FROM artifact_usage")}


def recomputed(path: Path) -> dict[str, tuple[int, int, int, int]]:
    """The figures as the budget once summed them, from every row."""
    reserved = 8 * 1024 + 2 * 16 * 1024
    stored = (
        "length(CAST(request AS BLOB))+coalesce(length(CAST(worker AS BLOB)),0)"
        "+coalesce(length(CAST(receipt AS BLOB)),0)"
    )
    with sqlite3.connect(path) as db:
        found: dict[str, tuple[int, int, int, int]] = {}
        for scope in (
            "",
            *(
                p
                for (p,) in db.execute(
                    "SELECT DISTINCT project FROM artifact_usage WHERE project!=''"
                )
            ),
        ):
            where = "WHERE ?='' OR project=?"
            revisions, data = db.execute(
                f"SELECT count(*),coalesce(sum(length(data)),0) FROM artifact_revisions {where}",
                (scope, scope),
            ).fetchone()
            (operations,) = db.execute(
                f"SELECT count(*) FROM artifact_operations {where}", (scope, scope)
            ).fetchone()
            metadata = sum(
                db.execute(sql + f" {where}", (scope, scope)).fetchone()[0]
                for sql in (
                    "SELECT coalesce(sum(length(CAST(record AS BLOB))),0) FROM artifact_revisions",
                    "SELECT coalesce(sum(length(CAST(record AS BLOB))),0) FROM artifact_events",
                    "SELECT coalesce(sum(length(CAST(path AS BLOB))+length(id)+length(project)),0)"
                    " FROM artifact_objects",
                    "SELECT coalesce(sum(CASE WHEN state='done' OR (state='intent' AND receipt "
                    f"IS NOT NULL) THEN {stored} WHEN state='transfer' THEN {stored}+16384 "
                    f"ELSE {reserved} END),0) FROM artifact_operations",
                )
            )
            found[scope] = (revisions, data, operations, metadata)
        return found


def busy_inventory(tmp_path: Path) -> ArtifactInventory:
    inventory = inventory_at(tmp_path)
    record(inventory, "op1", b"one")
    record(inventory, "op2", b"one")  # a reproduction
    inventory.begin(SOURCE, "op3", "held.bin", None)
    inventory.hold(SOURCE, "op3", "held.bin", None, "transfer failed")
    other = SOURCE.model_copy(update={"context": CONTEXT.model_copy(update={"project_id": "p2"})})
    record(inventory, "op4", b"two", source=other)
    inventory.record_metadata(
        RECORDED, "big.bin", sha256=BIG, size=99, media_type="text/plain", note="too_large"
    )
    inventory.record_deletion(RECORDED, "out.bin")
    return inventory


def test_the_retention_figures_follow_every_change(tmp_path: Path) -> None:
    busy_inventory(tmp_path)
    figures = usage_rows(tmp_path / "control.sqlite3")
    assert figures == recomputed(tmp_path / "control.sqlite3")
    assert figures["p1"][0] == 3  # out.bin twice (a revision and its tombstone), big.bin
    assert figures[""][0] == 4


def test_a_version_19_store_gains_its_retention_figures(tmp_path: Path) -> None:
    (tmp_path / "now").mkdir()
    busy_inventory(tmp_path / "now").documents.close()
    older_store(tmp_path / "now" / "control.sqlite3", tmp_path / "control.sqlite3", 19)
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (19,)
    writer = DocumentStore(tmp_path / "control.sqlite3")
    writer.initialize()  # the writer migrates it
    writer.close()
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
    assert usage_rows(tmp_path / "control.sqlite3") == recomputed(tmp_path / "control.sqlite3")


def test_the_review_sees_a_record_without_its_bytes_as_unavailable(tmp_path: Path) -> None:
    inventory = busy_inventory(tmp_path)
    reviews = ReviewStore(inventory.documents, artifacts=inventory)
    events = [e for e in inventory.events("p1").entries if e.worker_snapshot is None]
    assert [e.kind for e in events] == ["artifact.revision_created", "artifact.deleted"]
    reasons = []
    for event in events:
        _, payload, complete = project_artifact(reviews, "p1", event.revision, event.source)
        assert not complete
        reasons.append((payload["availability"], payload["reason"]))
    assert reasons == [
        ("unavailable", "the bytes were not kept (too_large)"),
        ("unavailable", "the file was deleted"),
    ]


def test_the_usage_triggers_exist_at_the_current_version(tmp_path: Path) -> None:
    """A later rebuild of an artifact table must keep them, or the figures go stale."""
    inventory_at(tmp_path)
    inventory_at(tmp_path).documents.initialize()
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        triggers = dict(
            db.execute(
                "SELECT name,tbl_name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'artifact_usage_%'"
            )
        )
    assert triggers == {
        "artifact_usage_revision": "artifact_revisions",
        "artifact_usage_event": "artifact_events",
        "artifact_usage_object": "artifact_objects",
        "artifact_usage_operation": "artifact_operations",
        "artifact_usage_operation_change": "artifact_operations",
    }


def test_a_held_operation_gives_back_its_reservation(tmp_path: Path) -> None:
    inventory = inventory_at(tmp_path)
    inventory.begin(SOURCE, "op1", "a.bin", None)
    reserved = usage_rows(tmp_path / "control.sqlite3")["p1"][3]
    assert reserved >= 40 * 1024  # in flight: the worst case is held back
    inventory.hold(SOURCE, "op1", "a.bin", None, "worker unreachable")
    held = usage_rows(tmp_path / "control.sqlite3")["p1"][3]
    assert held < 4 * 1024  # never pinned, it never finishes: only what it stored counts
    assert usage_rows(tmp_path / "control.sqlite3") == recomputed(tmp_path / "control.sqlite3")


def test_one_project_cannot_fill_the_metadata_of_all(tmp_path: Path) -> None:
    inventory = inventory_at(tmp_path, project_metadata_bytes=100_000)

    def fill() -> int:
        for index in range(1000):
            try:
                inventory.record_metadata(
                    RECORDED, f"f{index}.bin", sha256=BIG, size=9, media_type="text/plain",
                    note="quota",
                )  # fmt: skip
            except DocumentError as refused:
                if "metadata" not in str(refused):
                    raise
                return index
        raise AssertionError("the project's metadata never filled")

    assert 0 < fill() < 1000
    assert inventory.room("p1", 1) == "full"
    assert inventory.room("p2", 1) == "content"  # another project still records


def test_bytes_kept_after_a_hash_only_revision_are_a_new_revision(tmp_path: Path) -> None:
    inventory = inventory_at(tmp_path)
    data = b"first"
    hashed, _ = inventory.record_metadata(
        RECORDED, "out.bin", sha256=hashlib.sha256(data).hexdigest(), size=len(data),
        media_type="application/octet-stream", note="quota",
    )  # fmt: skip
    assert latest(inventory, hashed.artifact_id).hash_source == "sandbox"
    kept = record(inventory, "op1", data)
    assert kept.revision is not None
    assert (kept.reproduced, kept.revision.revision) == (False, 2)
    revision = latest(inventory, hashed.artifact_id)
    assert (revision.content_retained, revision.hash_source) == (True, "capture")
    assert inventory.content("p1", kept.revision) == data
    again, reproduced = inventory.record_metadata(
        RECORDED, "out.bin", sha256=hashlib.sha256(data).hexdigest(), size=len(data),
        media_type="application/octet-stream", note="quota",
    )  # fmt: skip
    assert (again, reproduced) == (kept.revision, True)  # a hash of bytes already kept


def test_a_tombstone_is_checked_against_the_budget_too(tmp_path: Path) -> None:
    inventory = inventory_at(tmp_path, project_revisions=1)
    record(inventory, "op1", b"x", path="a.txt")
    with pytest.raises(DocumentError, match="quota"):
        inventory.record_deletion(RECORDED, "a.txt")
    assert usage_rows(tmp_path / "control.sqlite3")["p1"][0] == 1  # nothing was added
