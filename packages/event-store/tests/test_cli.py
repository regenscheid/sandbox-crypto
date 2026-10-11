# SPDX-License-Identifier: MIT
"""The command line: verify, checkpoint and export."""

import hashlib
import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.event_store import Checkpoint, EventStore, Record
from sandbox_science.event_store.cli import main

SOURCE = "policy-middleware"


@pytest.fixture
def records(path: Path) -> list[Record]:
    with EventStore(path) as store:
        return [
            store.append("alert", SOURCE, {"n": n, "host": "example.org", "z": ["é", None]})
            for n in range(1, 5)
        ]


def change_record(path: Path, seq: int) -> None:
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("""UPDATE events SET payload = '{"n":0}' WHERE seq = ?""", (seq,))
    finally:
        db.close()


def test_verify_reports_an_intact_store(
    path: Path, records: list[Record], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["verify", str(path)]) == 0
    assert capsys.readouterr().out == f"ok: 4 records, head 4 {records[-1].hash}\n"


def test_verify_reports_problems(
    path: Path, records: list[Record], capsys: pytest.CaptureFixture[str]
) -> None:
    change_record(path, 2)
    assert main(["verify", str(path)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "append-only triggers were dropped: events_no_update",
        "record 2 does not match its hash: it was changed",
        "failed: 2 problems in 4 records",
    ]


def test_checkpoints_are_taken_and_checked(
    path: Path, records: list[Record], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    saved = tmp_path / "checkpoint.json"
    assert main(["checkpoint", str(path), "--output", str(saved)]) == 0
    assert Checkpoint.from_json(saved.read_text()) == Checkpoint(4, records[-1].hash)
    with EventStore(path) as store:
        store.append("alert", SOURCE, {"n": 5})
    assert main(["verify", str(path), "--checkpoint", str(saved)]) == 0
    assert "agrees with 1 checkpoints" in capsys.readouterr().out
    # A store cut back to fewer records than the checkpoint names.
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("DROP TRIGGER events_no_delete")
    db.execute("DELETE FROM events WHERE seq > 3")
    db.execute(
        "CREATE TRIGGER events_no_delete BEFORE DELETE ON events "
        "BEGIN SELECT RAISE(ABORT, 'the event store is append-only'); END"
    )
    db.close()
    assert main(["verify", str(path)]) == 0
    assert main(["verify", str(path), "--checkpoint", str(saved)]) == 1
    assert "newer records were removed" in capsys.readouterr().out


def test_checkpoint_refuses_a_broken_store(
    path: Path, records: list[Record], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    change_record(path, 3)
    saved = tmp_path / "checkpoint.json"
    assert main(["checkpoint", str(path), "--output", str(saved)]) == 1
    assert not saved.exists()
    assert list(tmp_path.glob(".checkpoint.json.*")) == []
    assert "record 3 does not match its hash" in capsys.readouterr().err


def test_checkpoint_writes_to_stdout(
    path: Path, records: list[Record], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["checkpoint", str(path)]) == 0
    assert Checkpoint.from_json(capsys.readouterr().out) == Checkpoint(4, records[-1].hash)


def test_export_writes_verifiable_json_lines(
    path: Path, records: list[Record], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exported = tmp_path / "events.jsonl"
    assert main(["export", str(path), "--output", str(exported)]) == 0
    lines = exported.read_text(encoding="utf-8").splitlines()
    assert lines == [record.to_json() for record in records]
    # Another tool can check the export with nothing but the documented encoding.
    prev = "0" * 64
    for line in lines:
        entry = json.loads(line)
        assert entry["prev_hash"] == prev
        assert independent_record_hash(entry) == entry["hash"]
        prev = entry["hash"]
    assert main(["export", str(path), "--after", "2"]) == 0
    assert [json.loads(line)["seq"] for line in capsys.readouterr().out.splitlines()] == [3, 4]


def canonical(value: Any) -> str:
    """RFC 8785 for objects, arrays, strings, integers, booleans and null."""
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: str(key).encode("utf-16-be"))
        return "{" + ",".join(f"{canonical(key)}:{canonical(value[key])}" for key in keys) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def independent_record_hash(entry: dict[str, Any]) -> str:
    """Recompute an exported record's hash from docs/EVENT-STORE.md alone."""
    fields = (
        b"sandbox-science-event-store/v1",
        bytes.fromhex(str(entry["prev_hash"])),
        struct.pack(">Q", entry["seq"]),
        str(entry["time"]).encode(),
        str(entry["kind"]).encode(),
        str(entry["source"]).encode(),
        canonical(entry["payload"]).encode(),
    )
    return hashlib.sha256(b"".join(struct.pack(">Q", len(f)) + f for f in fields)).hexdigest()


def test_export_stops_at_a_malformed_record(
    path: Path, records: list[Record], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("DROP TRIGGER events_no_update")
    db.execute("UPDATE events SET payload = '{\"n\": 3}' WHERE seq = 3")
    db.close()
    exported = tmp_path / "events.jsonl"
    assert main(["export", str(path), "--output", str(exported)]) == 1
    assert "record 3 is malformed" in capsys.readouterr().err
    assert not exported.exists()


@pytest.mark.parametrize("command", ["verify", "checkpoint", "export"])
def test_a_missing_store_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    assert main([command, str(tmp_path / "missing.db")]) == 1
    assert capsys.readouterr().err.startswith("error: no event store")


def test_a_malformed_checkpoint_file_is_an_error(
    path: Path, records: list[Record], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"seq": 1}')
    assert main(["verify", str(path), "--checkpoint", str(bad)]) == 1
    assert capsys.readouterr().err.startswith(f"error: {bad}: ")
