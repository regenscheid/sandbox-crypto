# SPDX-License-Identifier: MIT
"""Appending: the chain, the record encoding, durability settings and refusals."""

import hashlib
import sqlite3
import struct
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sandbox_science.event_store import (
    GENESIS,
    Checkpoint,
    EventStore,
    EventStoreError,
    RecordError,
    StoreReader,
    canonical_payload,
    record_hash,
    verify_store,
)

SOURCE = "policy-middleware"


def independent_hash(prev: str, seq: int, time: str, kind: str, source: str, payload: str) -> str:
    """The record hash as docs/EVENT-STORE.md defines it, written without the package's code."""
    fields = (
        b"sandbox-science-event-store/v1",
        bytes.fromhex(prev),
        struct.pack(">Q", seq),
        time.encode("ascii"),
        kind.encode("ascii"),
        source.encode("ascii"),
        payload.encode("utf-8"),
    )
    return hashlib.sha256(b"".join(struct.pack(">Q", len(f)) + f for f in fields)).hexdigest()


def test_the_documented_test_vector() -> None:
    payload = canonical_payload({"sandbox_id": "sbx-0001", "rule": "watched"})
    assert payload == '{"rule":"watched","sandbox_id":"sbx-0001"}'
    arguments = {
        "prev_hash": GENESIS,
        "seq": 1,
        "time": "2026-10-11T08:00:00.000000Z",
        "kind": "alert",
        "source": SOURCE,
        "payload": payload,
    }
    expected = "7155c4ba8d22fd08663539e775ac8df53cf3d200c771acbddba28937242d1419"
    assert record_hash(**arguments) == expected
    assert independent_hash(*arguments.values()) == expected  # pyright: ignore[reportArgumentType]


def test_appends_build_a_chain(path: Path) -> None:
    clock = [datetime(2026, 10, 11, 8, 0, tzinfo=UTC)]

    def tick() -> datetime:
        clock[0] += timedelta(microseconds=1500)
        return clock[0]

    with EventStore(path, clock=tick) as store:
        records = [
            store.append("alert", SOURCE, {"n": n, "text": "café \U0001f600"}) for n in range(5)
        ]
        assert store.head() == Checkpoint(5, records[-1].hash)
    assert [record.seq for record in records] == [1, 2, 3, 4, 5]
    assert records[0].prev_hash == GENESIS
    assert records[0].time == "2026-10-11T08:00:00.001500Z"
    for before, record in zip([None, *records], records, strict=False):
        if before is not None:
            assert record.prev_hash == before.hash
        assert record.hash == independent_hash(
            record.prev_hash, record.seq, record.time, record.kind, record.source, record.payload
        )
    with StoreReader(path) as reader:
        assert list(reader.records()) == records
        assert reader.head() == Checkpoint(5, records[-1].hash)


def test_a_reopened_store_continues_its_chain(path: Path) -> None:
    with EventStore(path) as store:
        first = store.append("alert", SOURCE, {})
    with EventStore(path) as store:
        second = store.append("monitor", SOURCE, {"rule": "chatty"})
    assert (second.seq, second.prev_hash) == (2, first.hash)
    assert second.data() == {"rule": "chatty"}


def test_an_empty_store_has_the_genesis_head(path: Path) -> None:
    with EventStore(path) as store:
        assert store.head() == Checkpoint(0, GENESIS)


def test_times_are_utc_with_microseconds(path: Path) -> None:
    eastern = timezone(timedelta(hours=-4))
    with EventStore(path, clock=lambda: datetime(2026, 10, 11, 4, 0, tzinfo=eastern)) as store:
        assert store.append("alert", SOURCE, {}).time == "2026-10-11T08:00:00.000000Z"
    with EventStore(path, clock=lambda: datetime(2026, 10, 11)) as store:
        with pytest.raises(EventStoreError, match="timezone"):
            store.append("alert", SOURCE, {})
        assert store.head().seq == 1


def test_commits_are_durable(path: Path) -> None:
    with EventStore(path) as store:
        store.append("alert", SOURCE, {})
        db = store._db
        assert db is not None
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert db.execute("PRAGMA synchronous").fetchone() == (2,)  # FULL


@pytest.mark.parametrize(
    ("kind", "source", "payload"),
    [
        pytest.param("Alert", SOURCE, {}, id="uppercase-kind"),
        pytest.param("", SOURCE, {}, id="empty-kind"),
        pytest.param("alert", "policy middleware", {}, id="space-in-source"),
        pytest.param("alert", "x" * 65, {}, id="long-source"),
        pytest.param("alert", SOURCE, {"ratio": 0.5}, id="float"),
        pytest.param("alert", SOURCE, ["a"], id="not-an-object"),
    ],
)
def test_invalid_records_are_refused(path: Path, kind: str, source: str, payload: object) -> None:
    with EventStore(path) as store:
        with pytest.raises(RecordError):
            store.append(kind, source, payload)  # pyright: ignore[reportArgumentType]
        assert store.head().seq == 0


def test_stored_records_cannot_be_updated_or_deleted_through_sql(path: Path) -> None:
    with EventStore(path) as store:
        store.append("alert", SOURCE, {})
    db = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE events SET kind = 'other'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM events")
    finally:
        db.close()


def test_a_closed_store_refuses_appends(path: Path) -> None:
    store = EventStore(path)
    store.close()
    with pytest.raises(EventStoreError, match="closed"):
        store.append("alert", SOURCE, {})


def test_other_databases_are_not_taken_over(tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE entries (key TEXT)")
    db.commit()
    db.close()
    with pytest.raises(EventStoreError, match="not an event store"):
        EventStore(other)
    text = tmp_path / "text.db"
    text.write_text("not a database, but long enough to have a header " * 4)
    with pytest.raises(EventStoreError):
        EventStore(text)


def test_an_unknown_format_is_refused(path: Path) -> None:
    EventStore(path).close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 2")
    db.close()
    with pytest.raises(EventStoreError, match="format 2"):
        EventStore(path)
    with pytest.raises(EventStoreError, match="not an event store"):
        StoreReader(path)


def test_a_missing_store_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(EventStoreError, match="no event store"):
        StoreReader(tmp_path / "missing.db")


def test_appends_fail_while_another_writer_holds_the_lock(path: Path) -> None:
    with EventStore(path, timeout=0.05) as store:
        blocker = sqlite3.connect(path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with pytest.raises(EventStoreError, match="locked"):
                store.append("alert", SOURCE, {})
            blocker.execute("ROLLBACK")
        finally:
            blocker.close()
        assert store.append("alert", SOURCE, {}).seq == 1


def test_an_append_waits_for_the_locks_no_longer_than_its_timeout(path: Path) -> None:
    timeout = 0.3
    with EventStore(path, timeout=timeout) as store, ThreadPoolExecutor(1) as pool:
        blocker = sqlite3.connect(path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            # The first append holds the store's lock while it waits for SQLite's.
            first = pool.submit(store.append, "alert", SOURCE, {"n": 1})
            time.sleep(0.05)
            start = time.monotonic()
            with pytest.raises(EventStoreError):
                store.append("alert", SOURCE, {"n": 2})
            elapsed = time.monotonic() - start
            with pytest.raises(EventStoreError, match="locked"):
                first.result(10)
            blocker.execute("ROLLBACK")
        finally:
            blocker.close()
    # Not the first append's wait followed by a full wait of its own.
    assert elapsed < timeout + 0.15


def test_a_failed_append_leaves_no_trace(path: Path) -> None:
    calls = [0]

    def failing_clock() -> datetime:
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("test: clock failure")
        return datetime.now(UTC)

    with EventStore(path, clock=failing_clock) as store:
        store.append("alert", SOURCE, {"n": 1})
        with pytest.raises(EventStoreError, match="clock failure") as failure:
            store.append("alert", SOURCE, {"n": 2})
        assert isinstance(failure.value.__cause__, RuntimeError)
        third = store.append("alert", SOURCE, {"n": 3})
    assert third.seq == 2
    with StoreReader(path) as reader:
        assert [record.data()["n"] for record in reader.records()] == [1, 3]


def blocking_clock() -> tuple[Callable[[], datetime], threading.Event, threading.Event]:
    """A clock whose first reading waits until released, holding up the first commit."""
    entered = threading.Event()
    release = threading.Event()
    first = [True]

    def clock() -> datetime:
        if first[0]:
            first[0] = False
            entered.set()
            release.wait(10)
        return datetime.now(UTC)

    return clock, entered, release


def test_appends_that_arrive_during_a_commit_share_the_next(path: Path) -> None:
    clock, entered, release = blocking_clock()
    statements: list[str] = []
    with EventStore(path, clock=clock) as store, ThreadPoolExecutor(max_workers=9) as pool:
        db = store._db
        assert db is not None
        db.set_trace_callback(statements.append)
        first = pool.submit(store.append, "alert", SOURCE, {"n": 0})
        assert entered.wait(10)
        later = [pool.submit(store.append, "alert", SOURCE, {"n": n}) for n in range(1, 9)]
        while len(store._waiting) < 8:
            time.sleep(0.001)
        release.set()
        records = [first.result(10)] + [future.result(10) for future in later]
    assert sorted(record.seq for record in records) == list(range(1, 10))
    assert statements.count("COMMIT") == 2
    assert verify_store(path).ok


def test_an_append_that_timed_out_is_not_stored_later(path: Path) -> None:
    clock, entered, release = blocking_clock()
    with EventStore(path, timeout=0.1, clock=clock) as store, ThreadPoolExecutor(1) as pool:
        first = pool.submit(store.append, "alert", SOURCE, {"n": 1})
        assert entered.wait(10)
        with pytest.raises(EventStoreError, match="timed out"):
            store.append("alert", SOURCE, {"n": 2})
        release.set()
        assert first.result(10).seq == 1
        assert store.append("alert", SOURCE, {"n": 3}).seq == 2
    with StoreReader(path) as reader:
        assert [record.data()["n"] for record in reader.records()] == [1, 3]


def test_records_after_a_sequence_number(path: Path) -> None:
    with EventStore(path) as store:
        for n in range(4):
            store.append("alert", SOURCE, {"n": n})
    with StoreReader(path) as reader:
        assert [record.seq for record in reader.records(after=2)] == [3, 4]


def test_exported_records_are_canonical_json_lines(path: Path) -> None:
    with EventStore(path) as store:
        record = store.append("alert", SOURCE, {"b": [1], "a": "é"})
    line = record.to_json()
    assert line == (
        f'{{"hash":"{record.hash}","kind":"alert","payload":{{"a":"é","b":[1]}},'
        f'"prev_hash":"{GENESIS}","seq":1,"source":"{SOURCE}","time":"{record.time}"}}'
    )
