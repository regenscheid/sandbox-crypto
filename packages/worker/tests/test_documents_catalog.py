# SPDX-License-Identifier: MIT
"""Durable catalog admission, complete-target validation and atomic replacement."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.documents import (
    MAX_CONTENT_CHUNK,
    CatalogChange,
    CatalogContentChunk,
    CatalogDelta,
    CatalogSnapshot,
    CatalogSnapshotPage,
    PublicationEntry,
    generation_id,
    manifest_sha256,
)
from sandbox_science.worker.documents_catalog import CatalogLimits, DocumentsCatalog
from sandbox_science.worker.errors import WorkerError


def entry(
    raw: bytes = b"proof",
    *,
    document: str = "a",
    revision: int = 1,
    name: str | None = None,
    session: str | None = None,
) -> PublicationEntry:
    return PublicationEntry(
        document_id=document,
        revision=revision,
        name=name or document,
        scope="session" if session else "project",
        session_id=session,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        content_bytes=len(raw),
    )


def target(
    entries: list[PublicationEntry], *, seq: int = 1, project: str = "p1"
) -> CatalogSnapshot:
    digest = manifest_sha256(project, seq, entries)
    return CatalogSnapshot(
        project_id=project,
        generation_id=generation_id(seq, digest),
        catalog_seq=seq,
        entry_count=len(entries),
        total_content_bytes=sum(item.content_bytes for item in entries),
        manifest_sha256=digest,
    )


def page(
    snapshot: CatalogSnapshot, entries: list[PublicationEntry], start: int = 0
) -> CatalogSnapshotPage:
    return CatalogSnapshotPage(
        project_id=snapshot.project_id,
        generation_id=snapshot.generation_id,
        start_index=start,
        entries=tuple(entries),
    )


def chunks(snapshot: CatalogSnapshot, raw: bytes) -> Iterator[CatalogContentChunk]:
    for offset in range(0, len(raw), MAX_CONTENT_CHUNK):
        yield CatalogContentChunk(
            project_id=snapshot.project_id,
            generation_id=snapshot.generation_id,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            offset=offset,
            content_b64=base64.b64encode(raw[offset : offset + MAX_CONTENT_CHUNK]).decode(),
        )


def stage(
    catalog: DocumentsCatalog,
    snapshot: CatalogSnapshot,
    entries: list[PublicationEntry],
    contents: list[bytes],
) -> None:
    catalog.begin(snapshot)
    for start in range(0, len(entries), 64):
        catalog.page(page(snapshot, entries[start : start + 64], start))
    for raw in contents:
        for chunk in chunks(snapshot, raw):
            catalog.content(chunk)


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[DocumentsCatalog]:
    store = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    yield store
    store.close()


def test_only_a_complete_target_changes_membership(catalog: DocumentsCatalog) -> None:
    first = target([entry()])
    stage(catalog, first, [entry()], [b"proof"])
    assert catalog.status("p1").catalog_seq is None
    catalog.commit(first)
    following = target([entry(b"new", document="b")], seq=2)
    catalog.begin(following)
    assert catalog.status("p1").catalog_seq == 1
    with pytest.raises(WorkerError, match="all metadata"):
        catalog.commit(following)
    catalog.page(page(following, [entry(b"new", document="b")]))
    with pytest.raises(WorkerError, match="incomplete"):
        catalog.commit(following)
    assert catalog.status("p1").catalog_seq == 1
    catalog.content(next(chunks(following, b"new")))
    assert catalog.commit(following).catalog_seq == 2
    assert catalog._db.execute("SELECT document FROM catalog_entries").fetchall() == [("b",)]
    assert catalog._db.execute("SELECT count(*) FROM catalog_blobs").fetchone()[0] == 1
    assert catalog._db.execute("SELECT count(*) FROM document_written_revisions").fetchone()[0] == 0
    empty = target([], seq=3)
    catalog.begin(empty)
    assert catalog.status("p1").entry_count == 1
    assert catalog.commit(empty).entry_count == 0
    assert catalog._db.execute("SELECT count(*) FROM catalog_blobs").fetchone()[0] == 0


def test_partial_metadata_and_chunks_survive_restart_with_exact_replays(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    raw = b"x" * (MAX_CONTENT_CHUNK + 1)
    document = entry(raw)
    snapshot = target([document])
    first = DocumentsCatalog(path)
    first.begin(snapshot)
    first.page(page(snapshot, [document]))
    pieces = list(chunks(snapshot, raw))
    first.content(pieces[0])
    first.close()
    second = DocumentsCatalog(path)
    try:
        status = second.status("p1")
        assert status.catalog_seq is None
        assert status.staging is not None
        assert status.staging.received_content_bytes == MAX_CONTENT_CHUNK
        assert second.begin(snapshot).replayed
        assert second.page(page(snapshot, [document])).replayed
        assert second.content(pieces[0]).replayed
        second.content(pieces[1])
        assert second.commit(snapshot).catalog_seq == 1
        assert second.commit(snapshot).replayed
        assert second.begin(snapshot).replayed
        assert second._db.execute("SELECT content FROM catalog_blobs").fetchone()[0] == raw
    finally:
        second.close()


def test_holes_changed_replays_and_cross_target_content_are_refused(
    catalog: DocumentsCatalog,
) -> None:
    documents = [entry(document="a"), entry(document="b")]
    snapshot = target(documents)
    catalog.begin(snapshot)
    with pytest.raises(WorkerError, match="out of order"):
        catalog.page(page(snapshot, [documents[1]], 1))
    catalog.page(page(snapshot, [documents[0]]))
    with pytest.raises(WorkerError, match="different content"):
        catalog.page(page(snapshot, [entry(b"other")]))
    with pytest.raises(WorkerError, match="strictly ordered"):
        catalog.page(page(snapshot, [documents[0]], 1))
    with pytest.raises(WorkerError, match="duplicate"):
        catalog.page(page(snapshot, [entry(document="b", name="a")], 1))
    status = catalog.status("p1")
    assert status.staging is not None
    assert status.staging.received_entries == 1
    chunk = next(chunks(snapshot, b"proof"))
    for changed in [
        chunk.model_copy(update={"project_id": "p2"}),
        chunk.model_copy(update={"generation_id": "g"}),
    ]:
        with pytest.raises(WorkerError, match="no pending"):
            catalog.content(changed)
    with pytest.raises(WorkerError, match="absent"):
        catalog.content(next(chunks(snapshot, b"unknown")))
    catalog.content(chunk)
    with pytest.raises(WorkerError, match="different bytes"):
        catalog.content(
            chunk.model_copy(update={"content_b64": base64.b64encode(b"xxxxx").decode()})
        )


def test_content_is_fixed_size_contiguous_and_digest_checked(catalog: DocumentsCatalog) -> None:
    raw = b"x" * (2 * MAX_CONTENT_CHUNK + 1)
    document = entry(raw)
    snapshot = target([document])
    catalog.begin(snapshot)
    catalog.page(page(snapshot, [document]))
    pieces = list(chunks(snapshot, raw))
    with pytest.raises(WorkerError, match="contiguous"):
        catalog.content(pieces[1])
    with pytest.raises(WorkerError, match="aligned"):
        catalog.content(pieces[0].model_copy(update={"content_b64": "eA=="}))
    with pytest.raises(WorkerError, match="aligned"):
        catalog.content(pieces[0].model_copy(update={"offset": 1}))
    catalog.content(pieces[0])
    catalog.content(pieces[1])
    catalog.content(pieces[2].model_copy(update={"content_b64": "eQ=="}))
    with pytest.raises(WorkerError, match="digest"):
        catalog.commit(snapshot)
    assert catalog.status("p1").catalog_seq is None


@pytest.mark.parametrize("raw", [b"\xff", b"a\x00b"])
def test_non_text_documents_are_retained_staged_but_never_committed(
    catalog: DocumentsCatalog, raw: bytes
) -> None:
    snapshot = target([entry(raw)])
    stage(catalog, snapshot, [entry(raw)], [raw])
    with pytest.raises(WorkerError, match=r"UTF-8|NUL"):
        catalog.commit(snapshot)
    assert catalog.status("p1").catalog_seq is None
    assert catalog.status("p1").staging is not None


def test_empty_and_maximum_multibyte_text(catalog: DocumentsCatalog) -> None:
    raw = "é".encode() * (1024 * 512)
    documents = [entry(b"", document="a"), entry(raw, document="b")]
    snapshot = target(documents)
    stage(catalog, snapshot, documents, [raw])
    status = catalog.commit(snapshot)
    assert status.total_content_bytes == 1024 * 1024
    assert status.publication == "disabled"


def test_manifest_digest_and_declared_counts_must_be_exact(catalog: DocumentsCatalog) -> None:
    wrong = target([entry()]).model_copy(
        update={"manifest_sha256": "0" * 64, "generation_id": generation_id(1, "0" * 64)}
    )
    stage(catalog, wrong, [entry()], [b"proof"])
    with pytest.raises(WorkerError, match="manifest"):
        catalog.commit(wrong)
    with pytest.raises(WorkerError, match="payload is already staged"):
        catalog.begin(wrong.model_copy(update={"total_content_bytes": 6}))
    assert catalog.status("p1").catalog_seq is None


def test_delta_complete_target_tombstones_promotion_and_immutable_revisions(
    catalog: DocumentsCatalog,
) -> None:
    old = entry(session="s1")
    first = target([old])
    stage(catalog, first, [old], [b"proof"])
    catalog.commit(first)
    promoted = entry()
    second = target([promoted], seq=2)
    catalog.delta(
        CatalogDelta(
            target=second, after_seq=1, changes=(CatalogChange(catalog_seq=2, entry=promoted),)
        )
    )
    assert catalog.status("p1").catalog_seq == 1
    catalog.commit(second)
    assert catalog._db.execute("SELECT path FROM catalog_entries").fetchone()[0] == "documents/a.md"
    tombstone = PublicationEntry(
        document_id="a", revision=2, scope="project", name="a", deleted=True
    )
    third = target([], seq=3)
    catalog.delta(
        CatalogDelta(
            target=third, after_seq=2, changes=(CatalogChange(catalog_seq=3, entry=tombstone),)
        )
    )
    assert catalog.commit(third).entry_count == 0


@pytest.mark.parametrize("revision", [1, 0])
def test_snapshot_cannot_rewrite_an_immutable_revision(
    catalog: DocumentsCatalog, revision: int
) -> None:
    first = target([entry(revision=2)])
    stage(catalog, first, [entry(revision=2)], [b"proof"])
    catalog.commit(first)
    changed = entry(b"different", revision=revision + 1)
    second = target([changed], seq=2)
    stage(catalog, second, [changed], [b"different"])
    with pytest.raises(WorkerError, match="revision"):
        catalog.commit(second)
    assert catalog.status("p1").catalog_seq == 1


def test_failed_delta_does_not_reserve_or_change_current(catalog: DocumentsCatalog) -> None:
    first = target([entry()])
    stage(catalog, first, [entry()], [b"proof"])
    catalog.commit(first)
    changed = entry(b"different")
    second = target([changed], seq=2)
    with pytest.raises(WorkerError, match="immutable"):
        catalog.delta(
            CatalogDelta(
                target=second, after_seq=1, changes=(CatalogChange(catalog_seq=2, entry=changed),)
            )
        )
    assert catalog.status("p1").staging is None
    second = target([], seq=2)
    absent = PublicationEntry(
        document_id="absent", revision=2, scope="project", name="absent", deleted=True
    )
    with pytest.raises(WorkerError, match="no current"):
        catalog.delta(
            CatalogDelta(
                target=second, after_seq=1, changes=(CatalogChange(catalog_seq=2, entry=absent),)
            )
        )
    with pytest.raises(WorkerError, match="complete target"):
        catalog.delta(
            CatalogDelta(
                target=second,
                after_seq=1,
                changes=(CatalogChange(catalog_seq=2, entry=entry(revision=2)),),
            )
        )
    assert catalog.status("p1").staging is None


def test_global_reservations_are_transactional_and_expire_without_deleting_current(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    now = [100.0]
    limits = CatalogLimits(max_staged_bytes=5, max_staged_entries=1, staging_seconds=10)
    stores = [DocumentsCatalog(path, limits, clock=lambda: now[0]) for _ in range(2)]
    snapshots = [target([entry()], project=f"p{i}") for i in range(2)]

    def reserve(index: int) -> ErrorKind | None:
        try:
            stores[index].begin(snapshots[index])
            return None
        except WorkerError as exc:
            return exc.kind

    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(reserve, range(2)))
        assert results.count(None) == 1
        assert results.count(ErrorKind.UNAVAILABLE) == 1
        selected = results.index(None)
        stage(stores[selected], snapshots[selected], [entry()], [b"proof"])
        stores[selected].commit(snapshots[selected])
        other = 1 - selected
        stores[other].begin(snapshots[other])
        now[0] = 110
        assert stores[other].status(snapshots[other].project_id).staging is None
        assert stores[selected].status(snapshots[selected].project_id).catalog_seq == 1
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize(
    "limits",
    [
        CatalogLimits(max_entries=1),
        CatalogLimits(max_project_bytes=9),
        CatalogLimits(max_staged_entries=1),
        CatalogLimits(max_staged_bytes=9),
    ],
)
def test_project_and_staging_quotas_are_reserved_before_pages(
    tmp_path: Path, limits: CatalogLimits
) -> None:
    store = DocumentsCatalog(tmp_path / "catalog.sqlite3", limits)
    try:
        with pytest.raises(WorkerError) as error:
            store.begin(target([entry(document="a"), entry(document="b")]))
        assert error.value.kind == ErrorKind.UNAVAILABLE
        assert store.status("p1").staging is None
    finally:
        store.close()


def test_global_current_quota_and_project_count_include_empty_targets(tmp_path: Path) -> None:
    store = DocumentsCatalog(
        tmp_path / "catalog.sqlite3",
        CatalogLimits(max_total_bytes=5, max_total_entries=1, max_projects=2),
    )
    try:
        first = target([entry()])
        stage(store, first, [entry()], [b"proof"])
        store.commit(first)
        second = target([entry()], project="p2")
        stage(store, second, [entry()], [b"proof"])
        with pytest.raises(WorkerError, match="current catalog quota"):
            store.commit(second)
        with pytest.raises(WorkerError, match="project catalog quota"):
            store.begin(target([], project="p3"))
        assert store.status("p1").catalog_seq == 1
        assert store.status("p2").catalog_seq is None
    finally:
        store.close()


def test_lowered_project_limits_apply_to_already_staged_targets(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    store = DocumentsCatalog(path)
    snapshot = target([entry()])
    stage(store, snapshot, [entry()], [b"proof"])
    store.close()
    store = DocumentsCatalog(path, CatalogLimits(max_project_bytes=0))
    try:
        with pytest.raises(WorkerError, match="configured quota"):
            store.commit(snapshot)
    finally:
        store.close()


def test_receipt_eviction_never_reapplies_an_old_generation(tmp_path: Path) -> None:
    store = DocumentsCatalog(tmp_path / "catalog.sqlite3", CatalogLimits(receipts_per_project=2))
    try:
        snapshots = [target([], seq=seq) for seq in range(1, 4)]
        for snapshot in snapshots:
            store.begin(snapshot)
            store.commit(snapshot)
        replay = store.commit(snapshots[1])
        assert replay.catalog_seq == 3
        assert replay.acknowledged == snapshots[1]
        assert store.status("p1").catalog_seq == 3
        with pytest.raises(WorkerError, match="stale"):
            store.begin(snapshots[0])
        with pytest.raises(WorkerError, match="no pending"):
            store.commit(snapshots[0])
        assert store._db.execute("SELECT count(*) FROM catalog_receipts").fetchone()[0] == 2
    finally:
        store.close()


def test_commit_failure_rolls_back_membership_content_and_receipt(
    catalog: DocumentsCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = target([entry()])
    stage(catalog, first, [entry()], [b"proof"])
    catalog.commit(first)
    second = target([entry(b"next", revision=2)], seq=2)
    stage(catalog, second, [entry(b"next", revision=2)], [b"next"])
    publish = catalog._publish

    def fail(snapshot: CatalogSnapshot) -> None:
        publish(snapshot)
        raise sqlite3.OperationalError("injected failure after mutation")

    monkeypatch.setattr(catalog, "_publish", fail)
    with pytest.raises(sqlite3.OperationalError):
        catalog.commit(second)
    assert catalog.status("p1").catalog_seq == 1
    assert catalog.status("p1").staging is not None
    assert catalog._db.execute("SELECT content FROM catalog_blobs").fetchall() == [(b"proof",)]
    monkeypatch.setattr(catalog, "_publish", publish)
    assert catalog.commit(second).catalog_seq == 2


@pytest.mark.parametrize(("revision", "raw"), [(1, b"other"), (5, b"other"), (5, b"proof")])
def test_removed_identity_and_revision_high_water_survive_restart(
    tmp_path: Path, revision: int, raw: bytes
) -> None:
    path = tmp_path / "catalog.sqlite3"
    store = DocumentsCatalog(path)
    first = target([entry(revision=5)])
    stage(store, first, [entry(revision=5)], [b"proof"])
    store.commit(first)
    empty = target([], seq=2)
    store.begin(empty)
    store.commit(empty)
    store.close()
    store = DocumentsCatalog(path)
    try:
        reappeared = entry(raw, revision=revision)
        snapshot = target([reappeared], seq=3)
        stage(store, snapshot, [reappeared], [raw])
        with pytest.raises(WorkerError, match="revision"):
            store.commit(snapshot)
        assert store.status("p1").catalog_seq == 2
        assert (
            store._db.execute("SELECT present FROM catalog_document_revisions").fetchone()[0] == 0
        )
    finally:
        store.close()


def test_identity_ledger_quota_never_silently_forgets_deleted_documents(tmp_path: Path) -> None:
    store = DocumentsCatalog(tmp_path / "catalog.sqlite3", CatalogLimits(max_retained_identities=1))
    try:
        first = target([entry()])
        stage(store, first, [entry()], [b"proof"])
        store.commit(first)
        empty = target([], seq=2)
        store.begin(empty)
        store.commit(empty)
        new = entry(document="b")
        third = target([new], seq=3)
        stage(store, third, [new], [b"proof"])
        with pytest.raises(WorkerError, match="retained document identity quota"):
            store.commit(third)
        assert store.status("p1").entry_count == 0
        assert store._db.execute("SELECT document FROM catalog_document_revisions").fetchall() == [
            ("a",)
        ]
    finally:
        store.close()


def test_tombstone_revision_survives_later_snapshot_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    store = DocumentsCatalog(path)
    first = target([entry()])
    stage(store, first, [entry()], [b"proof"])
    store.commit(first)
    tombstone = PublicationEntry(
        document_id="a", revision=10, scope="project", name="a", deleted=True
    )
    second = target([], seq=2)
    store.delta(
        CatalogDelta(
            target=second, after_seq=1, changes=(CatalogChange(catalog_seq=2, entry=tombstone),)
        )
    )
    store.commit(second)
    third = target([], seq=3)
    store.begin(third)
    store.commit(third)
    store.close()
    store = DocumentsCatalog(path)
    try:
        old = entry(revision=9)
        fourth = target([old], seq=4)
        with pytest.raises(WorkerError, match="backwards"):
            store.delta(
                CatalogDelta(
                    target=fourth, after_seq=3, changes=(CatalogChange(catalog_seq=4, entry=old),)
                )
            )
        assert store.status("p1").staging is None
        newer = entry(revision=11)
        fourth = target([newer], seq=4)
        store.delta(
            CatalogDelta(
                target=fourth, after_seq=3, changes=(CatalogChange(catalog_seq=4, entry=newer),)
            )
        )
        for chunk in chunks(fourth, b"proof"):
            store.content(chunk)
        assert store.commit(fourth).entry_count == 1
    finally:
        store.close()


def test_expiry_cleanup_survives_a_rejected_transaction(tmp_path: Path) -> None:
    now = [0.0]
    store = DocumentsCatalog(
        tmp_path / "catalog.sqlite3", CatalogLimits(staging_seconds=1), clock=lambda: now[0]
    )
    try:
        snapshot = target([entry()])
        store.begin(snapshot)
        now[0] = 2.0
        with pytest.raises(WorkerError, match="no pending"):
            store.page(page(snapshot, [entry()]))
        assert store._db.execute("SELECT count(*) FROM catalog_stages").fetchone()[0] == 0
    finally:
        store.close()


def test_lowered_global_staging_quota_blocks_continuation(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    store = DocumentsCatalog(path)
    snapshot = target([entry()])
    stage(store, snapshot, [entry()], [b"proof"])
    store.close()
    store = DocumentsCatalog(path, CatalogLimits(max_staged_bytes=0))
    try:
        for operation in [
            lambda: store.page(page(snapshot, [entry()])),
            lambda: store.content(next(chunks(snapshot, b"proof"))),
            lambda: store.commit(snapshot),
        ]:
            with pytest.raises(WorkerError, match="existing staging exceeds"):
                operation()
        assert store.status("p1").catalog_seq is None
    finally:
        store.close()


def test_concurrent_identical_commit_has_one_durable_receipt(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    stores = [DocumentsCatalog(path) for _ in range(2)]
    snapshot = target([entry()])
    try:
        stage(stores[0], snapshot, [entry()], [b"proof"])
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda store: store.commit(snapshot), stores))
        assert sorted(result.replayed for result in results) == [False, True]
        assert all(result.acknowledged == snapshot for result in results)
        assert stores[0]._db.execute("SELECT count(*) FROM catalog_receipts").fetchone()[0] == 1
        assert stores[0]._db.execute("SELECT count(*) FROM catalog_stages").fetchone()[0] == 0
    finally:
        for store in stores:
            store.close()


def test_delta_tombstone_then_reappearance_cannot_skip_revision_high_water(
    catalog: DocumentsCatalog,
) -> None:
    first = target([entry()])
    stage(catalog, first, [entry()], [b"proof"])
    catalog.commit(first)
    tombstone = PublicationEntry(
        document_id="a", revision=3, scope="project", name="a", deleted=True
    )
    reappeared = entry(revision=2)
    following = target([reappeared], seq=3)
    changes = (
        CatalogChange(catalog_seq=2, entry=tombstone),
        CatalogChange(catalog_seq=3, entry=reappeared),
    )
    with pytest.raises(WorkerError, match="backwards"):
        catalog.delta(CatalogDelta(target=following, after_seq=1, changes=changes))
    assert catalog.status("p1").staging is None
    reappeared = entry(revision=4)
    following = target([reappeared], seq=3)
    changes = (
        CatalogChange(catalog_seq=2, entry=tombstone),
        CatalogChange(catalog_seq=3, entry=reappeared),
    )
    catalog.delta(CatalogDelta(target=following, after_seq=1, changes=changes))
    assert catalog.commit(following).catalog_seq == 3


def test_unreadable_bounded_staging_metadata_cannot_replace_current(
    catalog: DocumentsCatalog,
) -> None:
    first = target([entry()])
    stage(catalog, first, [entry()], [b"proof"])
    catalog.commit(first)
    following = target([entry(revision=2)], seq=2)
    stage(catalog, following, [entry(revision=2)], [])
    catalog._db.execute("UPDATE catalog_stage_entries SET record=?", ("x" * (2 * 1024 * 1024),))
    with pytest.raises(WorkerError, match="unreadable") as error:
        catalog.commit(following)
    assert error.value.kind == ErrorKind.INTERNAL
    assert catalog.status("p1").catalog_seq == 1


def test_unreadable_target_cannot_disclose_another_projects_status(
    catalog: DocumentsCatalog,
) -> None:
    first = target([])
    catalog.begin(first)
    catalog.commit(first)
    catalog._db.execute(
        "UPDATE catalog_projects SET target=?", (target([], project="p2").model_dump_json(),)
    )
    with pytest.raises(WorkerError, match="another project") as error:
        catalog.status("p1")
    assert error.value.kind == ErrorKind.INTERNAL


def test_delta_path_collision_is_a_conflict_and_keeps_current(catalog: DocumentsCatalog) -> None:
    documents = [entry(document="a"), entry(document="b")]
    first = target(documents)
    stage(catalog, first, documents, [b"proof"])
    catalog.commit(first)
    following = target(documents, seq=2)
    collision = entry(document="b", name="a")
    with pytest.raises(WorkerError, match="invalid complete delta manifest") as error:
        catalog.delta(
            CatalogDelta(
                target=following,
                after_seq=1,
                changes=(CatalogChange(catalog_seq=2, entry=collision),),
            )
        )
    assert error.value.kind == ErrorKind.CONFLICT
    assert catalog.status("p1").catalog_seq == 1
    assert catalog.status("p1").staging is None
