# SPDX-License-Identifier: MIT
"""The content-addressed store and its SQLite index."""

import hashlib
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox_science.package_cache.adapter import Kind
from sandbox_science.package_cache.errors import StoreError
from sandbox_science.package_cache.store import (
    ACCESS_INTERVAL,
    ENTRY_OVERHEAD,
    Entry,
    Removal,
    Store,
    StoreStats,
    TempBlob,
)


class Clock:
    """A settable stand-in for ``time.time``."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def put(store: Store, key: str, body: bytes, **options: object) -> Entry:
    fields: dict[str, object] = {"kind": Kind.ARTIFACT, "content_type": "application/x-test"}
    fields.update(options)
    return store.put(key, store.write_temp(body), **fields)  # pyright: ignore[reportArgumentType]


def lookup(store: Store, key: str) -> Entry:
    found = store.lookup(key)
    assert found is not None
    entry, file = found
    file.close()
    return entry


def blob_names(root: Path) -> set[str]:
    return {path.name for path in (root / "blobs").rglob("*") if path.is_file()}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def store(root: Path, clock: Clock) -> Iterator[Store]:
    """A store whose cap counts blob bytes only, to keep the arithmetic of these tests plain."""
    with Store(root, max_size=1000, clock=clock, entry_overhead=0) as store:
        yield store


def test_bodies_are_stored_under_their_sha256(store: Store, root: Path, clock: Clock) -> None:
    body = b"package bytes"
    entry = put(
        store,
        "pypi:files/x.whl",
        body,
        etag='"v1"',
        last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
        digest=f"sha256:{sha(body)}",
        immutable=True,
    )
    assert entry == Entry(
        key="pypi:files/x.whl",
        sha256=sha(body),
        size=len(body),
        kind=Kind.ARTIFACT,
        content_type="application/x-test",
        fetched=clock.now,
        accessed=clock.now,
        etag='"v1"',
        last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
        status=200,
        immutable=True,
        digest=f"sha256:{sha(body)}",
    )
    assert (root / "blobs" / sha(body)[:2] / sha(body)).read_bytes() == body
    assert store.get("pypi:files/x.whl") == entry
    found = store.lookup("pypi:files/x.whl")
    assert found is not None
    with found[1] as file:
        assert file.read() == body
    assert found[0] == entry
    assert list((root / "tmp").iterdir()) == []


def test_identical_bodies_share_one_blob(store: Store, root: Path) -> None:
    put(store, "a", b"same")
    put(store, "b", b"same")
    assert blob_names(root) == {sha(b"same")}
    assert store.stats() == StoreStats(entries=2, bytes=4)
    assert store.total_size == 4


def test_replacing_a_body_deletes_the_old_blob_once_unused(store: Store, root: Path) -> None:
    put(store, "a", b"one")
    put(store, "b", b"one")
    put(store, "a", b"two")
    assert blob_names(root) == {sha(b"one"), sha(b"two")}
    put(store, "b", b"three")
    assert blob_names(root) == {sha(b"two"), sha(b"three")}
    assert store.total_size == store.stats().bytes == len(b"two") + len(b"three")


def test_forget_drops_one_entry_and_its_unshared_blob(store: Store, root: Path) -> None:
    put(store, "hackage:bad.tar.gz", b"a bad copy")
    put(store, "a", b"shared")
    put(store, "b", b"shared")
    assert store.forget("hackage:bad.tar.gz") == Removal(entries=1, blobs=1, bytes=10)
    assert store.get("hackage:bad.tar.gz") is None
    # A blob another entry still uses stays.
    assert store.forget("a") == Removal(entries=1)
    assert lookup(store, "b").size == 6
    assert blob_names(root) == {sha(b"shared")}
    assert store.forget("missing") == Removal()
    assert store.stats() == StoreStats(entries=1, bytes=6)


def test_negative_entries_keep_their_status(store: Store) -> None:
    put(store, "k", b"not found", kind=Kind.METADATA, status=404)
    entry = store.get("k")
    assert entry is not None
    assert entry.status == 404
    assert entry.kind is Kind.METADATA


def test_access_times_are_written_at_most_once_a_minute(store: Store, clock: Clock) -> None:
    entry = put(store, "k", b"x")
    clock.now += ACCESS_INTERVAL - 1
    assert lookup(store, "k").accessed == entry.accessed
    stored = store.get("k")
    assert stored is not None
    assert stored.accessed == entry.accessed
    clock.now += 2
    assert lookup(store, "k").accessed == clock.now
    stored = store.get("k")
    assert stored is not None
    assert stored.accessed == clock.now


def test_eviction_removes_the_least_recently_used_to_ninety_percent(
    store: Store, clock: Clock, root: Path
) -> None:
    for name in "abcde":
        put(store, name, name.encode() * 200)
        clock.now += ACCESS_INTERVAL + 1
    lookup(store, "a")
    put(store, "f", b"f" * 200)
    assert store.over_capacity()
    removed = store.evict()
    assert removed == Removal(entries=2, blobs=2, bytes=400)
    assert store.keys() == ["d", "e", "a", "f"]
    assert store.stats() == StoreStats(entries=4, bytes=800)
    assert blob_names(root) == {sha(name.encode() * 200) for name in "adef"}
    assert not store.over_capacity()


def test_eviction_keeps_a_blob_while_an_entry_uses_it(store: Store, clock: Clock) -> None:
    shared = b"s" * 600
    put(store, "shared-old", shared)
    clock.now += ACCESS_INTERVAL + 1
    put(store, "unique-old", b"u" * 300)
    clock.now += ACCESS_INTERVAL + 1
    put(store, "shared-new", shared)
    clock.now += ACCESS_INTERVAL + 1
    put(store, "x", b"x" * 200)
    assert store.evict() == Removal(entries=2, blobs=1, bytes=300)
    assert store.keys() == ["shared-new", "x"]
    assert store.blob_path(sha(shared)).exists()
    assert not store.blob_path(sha(b"u" * 300)).exists()


def test_eviction_does_nothing_under_the_cap(store: Store) -> None:
    put(store, "a", b"x" * 1000)
    assert not store.over_capacity()
    assert store.evict() == Removal()
    assert store.keys() == ["a"]


def test_each_entry_counts_a_fixed_overhead_toward_the_cap(root: Path, clock: Clock) -> None:
    # Negative answers share one small blob, so only the overhead bounds their number.
    with Store(root, max_size=10_000, clock=clock) as store:
        for number in range(9):
            put(store, f"missing-{number}", b"not found", kind=Kind.METADATA, status=404)
            clock.now += ACCESS_INTERVAL + 1
        assert store.stats() == StoreStats(entries=9, bytes=9)
        assert store.charged_size == 9 + 9 * ENTRY_OVERHEAD
        assert not store.over_capacity()
        put(store, "missing-9", b"not found", kind=Kind.METADATA, status=404)
        assert store.over_capacity()
        assert store.evict() == Removal(entries=2)
        assert store.keys() == [f"missing-{number}" for number in range(2, 10)]
        assert store.charged_size == 9 + 8 * ENTRY_OVERHEAD <= 9000
        assert store.blob_path(sha(b"not found")).exists()


def test_eviction_purges_expired_negative_entries_first(root: Path, clock: Clock) -> None:
    with Store(root, max_size=10**6, clock=clock, negative_expiry=100) as store:
        put(store, "positive-old", b"package", kind=Kind.METADATA)
        for number in range(300):  # more than one batch
            put(store, f"gone-{number}", b"gone", kind=Kind.METADATA, status=410)
        clock.now += 50
        put(store, "missing-fresh", b"missing", kind=Kind.METADATA, status=404)
        clock.now += 51
        lookup(store, "gone-0")  # recently used, but expired all the same
        # Under the cap, so only the purge removes anything.
        assert store.evict() == Removal(entries=300, blobs=1, bytes=4)
        assert store.keys() == ["positive-old", "missing-fresh"]
        assert not store.blob_path(sha(b"gone")).exists()
        clock.now += 50
        assert store.evict() == Removal(entries=1, blobs=1, bytes=7)
        assert store.keys() == ["positive-old"]


def test_without_an_expiry_negative_entries_wait_for_the_cap(store: Store, clock: Clock) -> None:
    put(store, "missing", b"not found", kind=Kind.METADATA, status=404)
    clock.now += 10 * 86400
    assert store.evict() == Removal()
    assert store.keys() == ["missing"]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"entry_overhead": -1}, "entry_overhead"),
        ({"negative_expiry": 0}, "negative_expiry"),
        ({"max_size": 0}, "max_size"),
    ],
)
def test_store_options_are_checked(root: Path, options: dict[str, int], message: str) -> None:
    arguments: dict[str, int] = {"max_size": 10, **options}
    with pytest.raises(ValueError, match=message):
        Store(root, **arguments)  # pyright: ignore[reportArgumentType]


def test_eviction_skips_blob_names_from_malformed_rows(store: Store, root: Path) -> None:
    db = sqlite3.connect(root / "index.sqlite3")
    try:
        db.execute(
            "INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("bad", "../../escape", 1, "artifact", "x", 0.0, 0.0, None, None, 200, 0, None),
        )
        db.commit()
    finally:
        db.close()
    put(store, "big", b"x" * 1200)
    assert store.evict() == Removal(entries=2, blobs=2, bytes=1201)
    assert store.keys() == []


def test_blobs_without_entries_are_removed(store: Store, root: Path) -> None:
    put(store, "k", b"kept")
    old = store.blob_path(sha(b"orphan"))
    old.parent.mkdir(exist_ok=True)
    old.write_bytes(b"orphan")
    hour_ago = time.time() - 7200
    os.utime(old, (hour_ago, hour_ago))
    young = store.blob_path(sha(b"young"))
    young.parent.mkdir(exist_ok=True)
    young.write_bytes(b"young")
    foreign = root / "blobs" / "notes.txt"
    foreign.write_text("not a blob")
    assert store.remove_orphans(min_age=3600) == Removal(blobs=1, bytes=6)
    assert not old.exists()
    assert young.exists()
    assert store.remove_orphans() == Removal(blobs=1, bytes=5)
    assert not young.exists()
    assert foreign.exists()
    assert store.blob_path(sha(b"kept")).exists()


def test_recovery_cleans_up_and_locks_the_directory(root: Path) -> None:
    with Store(root, max_size=1000) as first:
        put(first, "k", b"kept")
        (root / "tmp" / "download-1").write_bytes(b"partial")
        (root / "tmp" / "leftover").mkdir()
        orphan = first.blob_path(sha(b"orphan"))
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_bytes(b"orphan")
        assert first.recover() == Removal(blobs=1, bytes=6)
        assert list((root / "tmp").iterdir()) == []
        with Store(root, max_size=1000) as second, pytest.raises(StoreError, match="in use"):
            second.recover()
    with Store(root, max_size=1000) as third:
        assert third.recover() == Removal()
        assert third.get("k") is not None


def test_entries_whose_blob_is_missing_or_damaged_are_dropped(store: Store) -> None:
    missing = put(store, "missing", b"aaaa")
    damaged = put(store, "damaged", b"bbbb")
    store.blob_path(missing.sha256).unlink()
    store.blob_path(damaged.sha256).write_bytes(b"bb")
    assert store.lookup("missing") is None
    assert store.lookup("damaged") is None
    assert store.get("missing") is None
    assert store.get("damaged") is None
    assert not store.blob_path(damaged.sha256).exists()
    assert store.stats() == StoreStats(entries=0, bytes=0)


def test_open_blob_reports_a_vanished_blob(store: Store) -> None:
    entry = put(store, "k", b"body")
    file = store.open_blob(entry)
    assert file is not None
    file.close()
    store.blob_path(entry.sha256).unlink()
    assert store.open_blob(entry) is None
    assert store.get("k") is None


def test_refresh_records_a_revalidation(store: Store, clock: Clock) -> None:
    entry = put(store, "k", b"body", kind=Kind.METADATA, etag='"1"')
    clock.now += 500
    refreshed = store.refresh(entry, etag='"2"', last_modified="Tue, 02 Jan 2024 00:00:00 GMT")
    assert refreshed is not None
    assert refreshed.fetched == refreshed.accessed == clock.now
    assert refreshed.etag == '"2"'
    assert store.get("k") == refreshed
    put(store, "k", b"another body", kind=Kind.METADATA)
    assert store.refresh(entry, etag=None, last_modified=None) is None


def test_put_only_takes_files_from_its_temp_directory(store: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError, match="temp directory"):
        store.put("k", TempBlob(outside, sha(b"x"), 1), kind=Kind.ARTIFACT, content_type="x")
    assert outside.exists()
    assert store.get("k") is None


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(
            ("k", "not-hex", 1, "artifact", "x", 0.0, 0.0, None, None, 200, 0, None), id="hash"
        ),
        pytest.param(
            ("k", "0" * 64, 1, "other", "x", 0.0, 0.0, None, None, 200, 0, None), id="kind"
        ),
        pytest.param(
            ("k", "0" * 64, "big", "artifact", "x", 0.0, 0.0, None, None, 200, 0, None), id="size"
        ),
        pytest.param(
            ("k", "0" * 64, 1, "artifact", "x", "now", 0.0, None, None, 200, 0, None), id="time"
        ),
        pytest.param(
            ("k", "0" * 64, -1, "artifact", "x", 0.0, 0.0, None, None, 200, 0, None), id="negative"
        ),
    ],
)
def test_malformed_index_rows_are_ignored(
    store: Store, root: Path, row: tuple[object, ...]
) -> None:
    db = sqlite3.connect(root / "index.sqlite3")
    try:
        db.execute("INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        db.commit()
    finally:
        db.close()
    assert store.get("k") is None
    assert store.lookup("k") is None
    assert store.keys() == []


def test_an_index_of_another_version_is_refused(root: Path) -> None:
    Store(root, max_size=10).close()
    db = sqlite3.connect(root / "index.sqlite3")
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()
    with pytest.raises(StoreError, match="version 99"):
        Store(root, max_size=10)


def test_entries_survive_reopening(root: Path) -> None:
    with Store(root, max_size=100) as store:
        put(store, "k", b"body")
    with Store(root, max_size=100) as store:
        assert store.get("k") is not None
        assert store.total_size == 4


def test_a_store_that_cannot_be_created_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(StoreError, match="cannot open"):
        Store(blocker / "cache", max_size=10)
