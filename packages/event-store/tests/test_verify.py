# SPDX-License-Identifier: MIT
"""Verification: every kind of tampering it must catch, and the limits checkpoints cover."""

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from sandbox_science.event_store import (
    GENESIS,
    Checkpoint,
    CheckpointError,
    EventStore,
    Record,
    Verification,
    record_hash,
    verify_rows,
    verify_store,
)
from sandbox_science.event_store.verify import MAX_PROBLEMS

Tamper = Callable[[Path], AbstractContextManager[sqlite3.Connection]]
SOURCE = "policy-middleware"


@pytest.fixture
def records(path: Path) -> list[Record]:
    """Six records in a fresh store."""
    with EventStore(path) as store:
        return [store.append("alert", SOURCE, {"n": n}) for n in range(1, 7)]


def messages(result: Verification) -> list[str]:
    return [problem.message for problem in result.problems]


def rechain(db: sqlite3.Connection, start: int = 1) -> None:
    """Recompute every hash from record ``start`` on, as a tamperer covering their tracks would."""
    rows = db.execute("SELECT seq, time, kind, source, payload, hash FROM events ORDER BY seq")
    prev = GENESIS
    for seq, time, kind, source, payload, stored in rows.fetchall():
        if seq < start:
            prev = stored
            continue
        new = record_hash(
            prev_hash=prev, seq=seq, time=time, kind=kind, source=source, payload=payload
        )
        db.execute("UPDATE events SET prev_hash = ?, hash = ? WHERE seq = ?", (prev, new, seq))
        prev = new


def test_an_intact_store_verifies(path: Path, records: list[Record]) -> None:
    result = verify_store(path, [Checkpoint(3, records[2].hash), Checkpoint(0, GENESIS)])
    assert result.ok
    assert result.records == 6
    assert result.head == Checkpoint(6, records[-1].hash)


def test_an_empty_store_verifies(path: Path) -> None:
    EventStore(path).close()
    result = verify_store(path, [Checkpoint(0, GENESIS)])
    assert result.ok
    assert result.head == Checkpoint(0, GENESIS)


# --- Caught from the store alone ----------------------------------------------------------------


def test_a_changed_record_is_caught(path: Path, records: list[Record], tamper: Tamper) -> None:
    with tamper(path) as db:
        db.execute("""UPDATE events SET payload = '{"n":30}' WHERE seq = 3""")
    assert messages(verify_store(path)) == ["record 3 does not match its hash: it was changed"]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("time", "2026-10-11T00:00:00.000000Z", id="time"),
        pytest.param("kind", "monitor", id="kind"),
        pytest.param("source", "orchestrator", id="source"),
        pytest.param("prev_hash", "1" * 64, id="prev-hash"),
    ],
)
def test_every_field_is_covered_by_the_hash(
    path: Path, records: list[Record], tamper: Tamper, column: str, value: str
) -> None:
    with tamper(path) as db:
        db.execute(f"UPDATE events SET {column} = ? WHERE seq = 3", (value,))
    assert "record 3 does not match its hash: it was changed" in messages(verify_store(path))


def test_a_changed_record_with_its_hash_recomputed_breaks_the_next_link(
    path: Path, records: list[Record], tamper: Tamper
) -> None:
    with tamper(path) as db:
        db.execute("""UPDATE events SET payload = '{"n":30}' WHERE seq = 3""")
        row = db.execute("SELECT time, kind, source, payload FROM events WHERE seq = 3").fetchone()
        time, kind, source, payload = row
        new = record_hash(
            prev_hash=records[1].hash, seq=3, time=time, kind=kind, source=source, payload=payload
        )
        db.execute("UPDATE events SET hash = ? WHERE seq = 3", (new,))
    assert messages(verify_store(path)) == [
        "record 4 does not follow record 3: records before it were changed, removed or inserted"
    ]


def test_a_deleted_record_is_caught(path: Path, records: list[Record], tamper: Tamper) -> None:
    with tamper(path) as db:
        db.execute("DELETE FROM events WHERE seq IN (3, 4)")
    assert messages(verify_store(path)) == ["records 3 to 4 are missing"]


def test_a_deleted_record_is_caught_after_renumbering(
    path: Path, records: list[Record], tamper: Tamper
) -> None:
    with tamper(path) as db:
        db.execute("DELETE FROM events WHERE seq = 3")
        db.execute("UPDATE events SET seq = seq - 1 WHERE seq > 3")
    found = messages(verify_store(path))
    assert found[:2] == [
        "record 3 does not follow record 2: records before it were changed, removed or inserted",
        "record 3 does not match its hash: it was changed",
    ]


def test_an_inserted_record_is_caught(path: Path, records: list[Record], tamper: Tamper) -> None:
    # A forged record 3 that itself verifies, with the later records moved up.
    with tamper(path) as db:
        db.execute("UPDATE events SET seq = seq + 100 WHERE seq >= 3")
        db.execute("UPDATE events SET seq = seq - 99 WHERE seq >= 103")
        time = records[2].time
        payload = '{"n":99}'
        forged = record_hash(
            prev_hash=records[1].hash,
            seq=3,
            time=time,
            kind="alert",
            source=SOURCE,
            payload=payload,
        )
        db.execute(
            "INSERT INTO events VALUES (3, ?, 'alert', ?, ?, ?, ?)",
            (time, SOURCE, payload, records[1].hash, forged),
        )
    found = messages(verify_store(path))
    assert found[:2] == [
        "record 4 does not follow record 3: records before it were changed, removed or inserted",
        "record 4 does not match its hash: it was changed",
    ]


def test_reordered_records_are_caught(path: Path, records: list[Record], tamper: Tamper) -> None:
    with tamper(path) as db:
        db.execute("UPDATE events SET seq = 100 WHERE seq = 3")
        db.execute("UPDATE events SET seq = 3 WHERE seq = 4")
        db.execute("UPDATE events SET seq = 4 WHERE seq = 100")
    found = messages(verify_store(path))
    assert "record 3 does not match its hash: it was changed" in found
    assert "record 4 does not match its hash: it was changed" in found
    assert any(message.startswith("record 5 does not follow record 4") for message in found)


def test_malformed_rows_are_reported_not_fatal(
    path: Path, records: list[Record], tamper: Tamper
) -> None:
    with tamper(path) as db:
        db.execute("UPDATE events SET time = 'yesterday' WHERE seq = 2")
        db.execute("""UPDATE events SET payload = '{"n": 4}' WHERE seq = 4""")
        db.execute("UPDATE events SET hash = upper(hash) WHERE seq = 5")
        db.execute(
            "INSERT INTO events VALUES (0, ?, 'alert', ?, '{}', ?, ?)",
            (records[0].time, SOURCE, GENESIS, GENESIS),
        )
    found = messages(verify_store(path))
    assert found[0] == "record 0 is malformed: sequence number 0 is out of range"
    assert "record 2 is malformed: time 'yesterday' is malformed" in found
    assert "record 4 is malformed: the payload is not in canonical form" in found
    assert any(message.startswith("record 5 is malformed: hash") for message in found)


def test_dropped_triggers_are_reported(path: Path, records: list[Record]) -> None:
    db = sqlite3.connect(path)
    db.execute("DROP TRIGGER events_no_delete")
    db.close()
    assert messages(verify_store(path)) == ["append-only triggers were dropped: events_no_delete"]


# --- Caught only against a checkpoint -----------------------------------------------------------


def test_truncation_is_caught_only_against_a_checkpoint(
    path: Path, records: list[Record], tamper: Tamper
) -> None:
    checkpoint = Checkpoint(6, records[-1].hash)
    with tamper(path) as db:
        db.execute("DELETE FROM events WHERE seq > 4")
    assert verify_store(path).ok
    assert messages(verify_store(path, [checkpoint])) == [
        "the store ends at record 4, but a checkpoint names record 6: newer records were removed"
    ]


def test_a_changed_newest_record_is_caught_only_against_a_checkpoint(
    path: Path, records: list[Record], tamper: Tamper
) -> None:
    checkpoint = Checkpoint(6, records[-1].hash)
    with tamper(path) as db:
        db.execute("""UPDATE events SET payload = '{"n":60}' WHERE seq = 6""")
        rechain(db, start=6)
    assert verify_store(path).ok
    assert messages(verify_store(path, [checkpoint])) == [
        "record 6 differs from its checkpoint: records up to it were changed and the hashes "
        "recomputed"
    ]


def test_a_rewritten_chain_is_caught_by_checkpoints_at_or_after_the_change(
    path: Path, records: list[Record], tamper: Tamper
) -> None:
    with tamper(path) as db:
        db.execute("""UPDATE events SET payload = '{"n":30}' WHERE seq = 3""")
        rechain(db)
    assert verify_store(path).ok
    assert verify_store(path, [Checkpoint(2, records[1].hash)]).ok
    for seq in (3, 5):
        result = verify_store(path, [Checkpoint(seq, records[seq - 1].hash)])
        assert [problem.seq for problem in result.problems] == [seq]


def test_a_checkpointed_record_that_is_missing(
    path: Path, records: list[Record], tamper: Tamper
) -> None:
    with tamper(path) as db:
        db.execute("DELETE FROM events WHERE seq = 3")
    assert messages(verify_store(path, [Checkpoint(3, records[2].hash)])) == [
        "record 3 is missing",
        "record 3, named by a checkpoint, is missing",
    ]


def test_an_appended_forgery_is_not_caught(path: Path, records: list[Record]) -> None:
    # The chain does not say who wrote a record: anyone who can write the
    # database can append records that verify (docs/EVENT-STORE.md).
    with EventStore(path) as store:
        store.append("alert", "someone-else", {"n": 7})
    assert verify_store(path, [Checkpoint(6, records[-1].hash)]).ok


def test_disagreeing_checkpoints_are_reported(path: Path, records: list[Record]) -> None:
    result = verify_store(path, [Checkpoint(2, records[1].hash), Checkpoint(2, records[2].hash)])
    assert "two checkpoints for record 2 disagree" in messages(result)


def test_listing_stops_at_the_limit() -> None:
    rows = [
        (seq, "2026-10-11T08:00:00.000000Z", "alert", SOURCE, "{}", GENESIS, GENESIS)
        for seq in range(1, MAX_PROBLEMS + 10)
    ]
    result = verify_rows(rows)
    assert len(result.problems) == MAX_PROBLEMS
    assert result.truncated
    assert not result.ok


# --- Checkpoints --------------------------------------------------------------------------------


def test_checkpoints_round_trip_through_json() -> None:
    checkpoint = Checkpoint(42, "ab" * 32)
    text = checkpoint.to_json()
    assert text == '{"format":"sandbox-science-event-store/v1","hash":"' + "ab" * 32 + '","seq":42}'
    assert Checkpoint.from_json(text) == checkpoint
    spaced = (
        '{\n  "seq": 42,\n  "hash": "'
        + "ab" * 32
        + '",\n  "format": "sandbox-science-event-store/v1"\n}\n'
    )
    assert Checkpoint.from_json(spaced) == checkpoint


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"format":"other/v1","hash":"' + "0" * 64 + '","seq":0}', id="format"),
        pytest.param('{"format":"sandbox-science-event-store/v1","seq":1}', id="no-hash"),
        pytest.param(
            '{"format":"sandbox-science-event-store/v1","hash":"' + "A" * 64 + '","seq":1}',
            id="uppercase-hash",
        ),
        pytest.param(
            '{"format":"sandbox-science-event-store/v1","hash":"' + "a" * 64 + '","seq":true}',
            id="boolean-seq",
        ),
        pytest.param(
            '{"format":"sandbox-science-event-store/v1","hash":"' + "a" * 64 + '","seq":-1}',
            id="negative-seq",
        ),
        pytest.param(
            '{"format":"sandbox-science-event-store/v1","hash":"' + "a" * 64 + '","seq":0}',
            id="empty-store-with-a-hash",
        ),
        pytest.param(
            '{"format":"sandbox-science-event-store/v1","hash":"' + "a" * 64 + '","seq":1,"x":1}',
            id="extra-key",
        ),
        pytest.param("[]", id="not-an-object"),
    ],
)
def test_malformed_checkpoints_are_refused(text: str) -> None:
    with pytest.raises(CheckpointError):
        Checkpoint.from_json(text)
