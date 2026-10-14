# SPDX-License-Identifier: MIT
"""The control store's transactions and schema versions, through its public API."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.control import ControlError, ControlStore, migrations
from sandbox_science.orchestrator.documents import DocumentError

CURRENT = migrations.VERSION
"""The version this code writes, before a test appends a step."""


def _store(path: Path, *, readonly: bool = False) -> ControlStore:
    return ControlStore(path, readonly=readonly)


def _runs(tx_sql: sqlite3.Connection) -> list[tuple[str]]:
    return tx_sql.execute("SELECT project FROM job_runs ORDER BY project").fetchall()


def test_a_change_commits_and_a_failed_one_rolls_back(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite3")
    assert not store.in_transaction
    with store.write() as tx:
        assert store.in_transaction
        tx.sql.execute("INSERT INTO job_runs(project) VALUES ('kept')")
    assert not store.in_transaction

    def failing() -> None:
        with store.write() as tx:
            tx.sql.execute("INSERT INTO job_runs(project) VALUES ('dropped')")
            raise RuntimeError("the change fails")

    with pytest.raises(RuntimeError):
        failing()
    with store.read() as tx:
        assert _runs(tx.sql) == [("kept",)]
    store.close()


def test_a_nested_transaction_is_refused_clearly(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite3")

    def nested() -> None:
        with store.write() as tx:
            tx.sql.execute("INSERT INTO job_runs(project) VALUES ('outer')")
            with store.write():
                pass

    with pytest.raises(ControlError, match="already open") as raised:
        nested()
    assert raised.value.kind == "internal"
    with pytest.raises(ControlError, match="already open"), store.write(), store.read():
        pass
    with store.read() as tx:
        assert _runs(tx.sql) == []
    store.close()


def test_a_read_only_store_is_missing_until_a_writer_makes_it(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    reader = _store(path, readonly=True)
    assert reader.missing
    with pytest.raises(ControlError) as raised, reader.read():
        pass
    assert raised.value.kind == "not_found"
    assert not path.exists()
    with pytest.raises(ControlError, match="read-only"), reader.write():
        pass
    writer = _store(path)
    writer.initialize()
    assert not reader.missing
    with reader.read() as tx:
        assert _runs(tx.sql) == []
        assert tx.sql.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
    reader.close()
    writer.close()


def test_the_document_error_is_the_control_error() -> None:
    assert DocumentError is ControlError


def _files(path: Path) -> dict[str, str]:
    """Each of the database's files (main, -wal, -shm) and the digest of its bytes. A
    reader of a WAL database may add an empty -wal and -shm: compare with ``_kept``."""
    return {
        found.name: hashlib.sha256(found.read_bytes()).hexdigest()
        for found in sorted(path.parent.glob(path.name + "*"))
    }


def _database(path: Path, version: int, application: int = 0, *, wal: bool = True) -> None:
    """An SQLite database in ``version`` with one table and one row."""
    db = sqlite3.connect(path)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO documents VALUES ('doc-1')")
    db.execute(f"PRAGMA user_version = {version}")
    db.execute(f"PRAGMA application_id = {application}")
    db.commit()
    db.close()


def _kept(before: dict[str, str], path: Path) -> bool:
    """Whether each file that was there before is unchanged."""
    after = _files(path)
    return {name: after.get(name) for name in before} == before


def _refused(path: Path, match: str) -> ControlError:
    """The refusal of ``path`` by a writer and by a reader, the same for both."""
    errors = []
    for readonly in (False, True):
        store = _store(path, readonly=readonly)
        with pytest.raises(ControlError, match=match) as raised, store.read():
            pass
        store.close()
        errors.append(raised.value)
    assert errors[0].kind == errors[1].kind == "unavailable"
    return errors[0]


def test_a_development_store_from_before_the_baseline_is_refused_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.sqlite3"
    _database(path, migrations.BASELINE - 1)
    before = _files(path)
    error = _refused(path, "document export")
    assert "move the file aside" in error.message
    assert _kept(before, path)


def test_a_store_whose_writer_crashed_is_refused_with_its_wal_unchanged(
    tmp_path: Path,
) -> None:
    live = tmp_path / "live" / "control.sqlite3"
    live.parent.mkdir()
    _database(live, migrations.BASELINE - 1)
    holder = sqlite3.connect(live, isolation_level=None)
    holder.execute("PRAGMA wal_autocheckpoint=0")
    holder.execute("INSERT INTO documents VALUES ('doc-2')")
    path = tmp_path / "crashed" / "control.sqlite3"
    path.parent.mkdir()
    for suffix in ("", "-wal"):  # the files as a crash leaves them
        shutil.copy(f"{live}{suffix}", f"{path}{suffix}")
    holder.close()
    before = _files(path)
    _refused(path, "document export")
    assert _kept(before, path)


def test_a_newer_store_is_refused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    _database(path, migrations.VERSION + 1, migrations.APPLICATION_ID)
    before = _files(path)
    _refused(path, "newer")
    assert _kept(before, path)


@pytest.mark.parametrize("version", [0, migrations.BASELINE, migrations.VERSION + 1])
def test_another_applications_database_is_refused_unchanged(tmp_path: Path, version: int) -> None:
    path = tmp_path / "control.sqlite3"
    _database(path, version, wal=False)
    before = _files(path)
    _refused(path, "not a control store")
    assert _kept(before, path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    db.close()


def test_a_file_that_is_not_sqlite_is_refused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    path.write_bytes(b"not a database " * 100)
    before = _files(path)
    _refused(path, "not a control store")
    assert _kept(before, path)


def test_a_reader_racing_the_first_writer_finds_the_store_missing(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    sqlite3.connect(path).close()  # created, nothing written yet
    reader = _store(path, readonly=True)
    assert reader.missing
    _store(path).initialize()
    assert not reader.missing
    reader.close()


def test_a_store_another_process_creates_during_the_check_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first writer's commit lands while a second one checks the file read-only.
    path = tmp_path / "control.sqlite3"
    sqlite3.connect(path).close()  # created, nothing written yet
    connect = sqlite3.connect
    statements: list[str] = []

    def first_commit(statement: str) -> None:
        if statement.startswith("--"):  # a pragma's own statement, inside another
            return
        statements.append(statement)
        if len(statements) == 2:
            other = connect(path, isolation_level=None)
            other.execute("PRAGMA journal_mode=WAL")
            other.execute("BEGIN IMMEDIATE")
            other.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY)")
            other.execute(f"PRAGMA application_id = {migrations.APPLICATION_ID}")
            other.execute(f"PRAGMA user_version = {migrations.VERSION}")
            other.execute("COMMIT")
            other.close()

    def traced(target: object, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = connect(target, *args, **kwargs)  # pyright: ignore
        if str(target).endswith("?mode=ro"):
            db.set_trace_callback(first_commit)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    store = _store(path)
    store.initialize()
    with store.read() as tx:
        assert tx.sql.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
    store.close()


_FIRST_OPEN = """
import sys, time
from pathlib import Path
from sandbox_science.orchestrator.control import ControlStore
time.sleep(max(0.0, float(sys.argv[2]) - time.time()))
ControlStore(Path(sys.argv[1])).initialize()
"""


def test_concurrent_first_opens_wait_for_each_other(tmp_path: Path) -> None:
    # Six processes create each of three new stores at the same moment.
    paths = [tmp_path / f"t{trial}" / "control.sqlite3" for trial in range(3)]
    start = str(time.time() + 3)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _FIRST_OPEN, str(path), start],
            stderr=subprocess.PIPE,
            text=True,
        )
        for path in paths
        for _ in range(6)
    ]
    for process in processes:
        _, err = process.communicate(timeout=120)
        assert process.returncode == 0, err
    for path in paths:
        db = sqlite3.connect(path)
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
        db.close()


def test_a_writer_runs_the_pending_steps_all_or_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control.sqlite3"
    _store(path).initialize()
    baseline = migrations.MIGRATIONS

    def add_notes(schema: migrations.Schema) -> None:
        schema.execute("CREATE TABLE notes (text TEXT NOT NULL) STRICT")

    def fail(schema: migrations.Schema) -> None:
        schema.execute("SELECT no_such_function()")

    monkeypatch.setattr(migrations, "MIGRATIONS", (*baseline, add_notes, fail))
    monkeypatch.setattr(migrations, "VERSION", CURRENT + 2)
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        _store(path).initialize()
    reader = _store(path, readonly=True)
    with pytest.raises(ControlError, match="open it with the harness"), reader.read():
        pass
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone() == (CURRENT,)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='notes'").fetchone() is None
    db.close()

    monkeypatch.setattr(migrations, "MIGRATIONS", (*baseline, add_notes))
    monkeypatch.setattr(migrations, "VERSION", CURRENT + 1)
    writer = _store(path)
    with writer.write() as tx:
        tx.sql.execute("INSERT INTO notes VALUES ('migrated')")
    writer.close()
    with reader.read() as tx:
        assert tx.sql.execute("PRAGMA user_version").fetchone() == (CURRENT + 1,)
        assert tx.sql.execute("SELECT text FROM notes").fetchall() == [("migrated",)]
    reader.close()


@pytest.mark.parametrize("statement", ["COMMIT", "-- a comment\n  begin", "ROLLBACK", "VACUUM"])
def test_a_step_cannot_end_the_writers_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    path = tmp_path / "control.sqlite3"
    _store(path).initialize()

    def ending(schema: migrations.Schema) -> None:
        schema.execute("CREATE TABLE notes (text TEXT)")
        schema.execute(statement)

    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, ending))
    monkeypatch.setattr(migrations, "VERSION", CURRENT + 1)
    with pytest.raises(ControlError, match="inside the writer's transaction"):
        _store(path).initialize()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone() == (CURRENT,)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='notes'").fetchone() is None
    db.close()


def test_a_new_store_carries_the_application_id(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    _store(path).initialize()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA application_id").fetchone() == (migrations.APPLICATION_ID,)
    db.close()


def test_the_baseline_is_never_edited() -> None:
    # A database made from the baseline keeps it: change the schema by appending a
    # step to migrations.MIGRATIONS instead.
    text = Path(migrations.__file__).with_name("baseline.sql").read_bytes()
    assert (
        hashlib.sha256(text).hexdigest()
        == "f78d0068e2cf6a2ad8339b2de73106b04d2d3cb5e18eb421ac2a8e1c4717c2aa"
    )


def test_after_commit_runs_once_a_change_commits_and_never_after_a_rollback(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "control.sqlite3")
    ran: list[str] = []
    with pytest.raises(ControlError, match="open change"):
        store.after_commit(lambda: ran.append("outside"))
    with store.write() as tx:
        tx.sql.execute("INSERT INTO job_runs(project) VALUES ('kept')")
        store.after_commit(lambda: ran.append("kept"))
        assert ran == []

    def failing() -> None:
        with store.write() as tx:
            tx.sql.execute("INSERT INTO job_runs(project) VALUES ('dropped')")
            store.after_commit(lambda: ran.append("dropped"))
            raise RuntimeError("the change fails")

    with pytest.raises(RuntimeError):
        failing()
    with store.read(), pytest.raises(ControlError, match="open change"):
        store.after_commit(lambda: ran.append("read"))
    assert ran == ["kept"]
    store.close()


def test_a_failing_after_commit_action_is_logged_and_the_others_still_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = _store(tmp_path / "control.sqlite3")
    ran: list[str] = []

    def failing() -> None:
        raise RuntimeError("the action fails")

    with store.write() as tx:  # the change stands, so nothing reaches its caller
        tx.sql.execute("INSERT INTO job_runs(project) VALUES ('kept')")
        store.after_commit(lambda: ran.append("first"))
        store.after_commit(failing)
        store.after_commit(lambda: ran.append("last"))
    assert ran == ["first", "last"]
    assert "an action after a committed change failed" in caplog.text
    with store.read() as tx:
        assert tx.sql.execute("SELECT project FROM job_runs").fetchall() == [("kept",)]
    store.close()


def _v3_with_a_thread(path: Path, monkeypatch: pytest.MonkeyPatch, anchor: str) -> None:
    """A version-3 store, from before the comment checks, with one comment thread."""
    from sandbox_science.orchestrator.documents import USER, DocumentStore

    with monkeypatch.context() as old:
        old.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:2])
        old.setattr(migrations, "VERSION", migrations.BASELINE + 1)
        documents = DocumentStore(path)
        doc = documents.create("p", name="n", title="N", text="x\n", author=USER).document_id
        with documents.control.write() as tx:
            tx.sql.execute(
                "INSERT INTO annotation_threads VALUES ('ann-1', ?, ?, '{}', 1.0)", (doc, anchor)
            )
            # A child row: rebuilding its parent table must keep its foreign key.
            tx.sql.execute(
                "INSERT INTO annotation_entries VALUES "
                "('cmt-1', 'ann-1', 1, 'create', '{}', 'A comment.', '{}', 1.0)"
            )
        documents.close()


def test_the_comment_checks_keep_rows_triggers_and_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control.sqlite3"
    _v3_with_a_thread(path, monkeypatch, "{}")
    before = sqlite3.connect(path)
    kept = before.execute("SELECT rowid, * FROM annotation_threads").fetchall()
    before.close()
    _store(path).initialize()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
    assert db.execute("SELECT rowid, * FROM annotation_threads").fetchall() == kept
    assert db.execute("SELECT entry_id, thread_id FROM annotation_entries").fetchall() == [
        ("cmt-1", "ann-1")
    ]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE annotation_threads SET anchor = '[]'")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db.execute("INSERT INTO annotation_threads SELECT 'ann-2', document_id, 'no json', "
                   "'{}', 1.0 FROM annotation_threads")  # fmt: skip
    assert db.execute("SELECT count(*) FROM math_current_checks").fetchone() == (0,)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_a_row_the_comment_checks_refuse_leaves_the_store_at_version_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control.sqlite3"
    _v3_with_a_thread(path, monkeypatch, "not json")
    db = sqlite3.connect(path)
    schema = db.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    db.close()
    with pytest.raises(ControlError) as refused:
        _store(path).initialize()
    assert refused.value.kind == "unavailable"
    message = refused.value.message
    assert "stays at version 3" in message
    assert "annotation_threads" in message
    assert "CHECK" in message
    assert "export" in message
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone() == (migrations.BASELINE + 1,)
    assert db.execute("SELECT anchor FROM annotation_threads").fetchall() == [("not json",)]
    assert db.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == (
        schema
    )
    db.close()


def _in_memory() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys=OFF")  # as migrate() runs
    db.execute("BEGIN")
    return db


def test_rebuild_keeps_another_tables_trigger_that_names_the_table() -> None:
    db = _in_memory()
    db.execute("CREATE TABLE b (id TEXT PRIMARY KEY, n INTEGER) STRICT")
    db.execute("CREATE TABLE a (id TEXT PRIMARY KEY) STRICT")
    db.execute("CREATE TRIGGER a_ins AFTER INSERT ON a BEGIN INSERT INTO b VALUES (new.id, 1); END")
    db.execute("INSERT INTO b VALUES ('x', 1)")
    migrations.rebuild(
        migrations.Schema(db),
        "b",
        "CREATE TABLE b (id TEXT PRIMARY KEY, n INTEGER CHECK (n > 0)) STRICT",
    )
    db.execute("INSERT INTO a VALUES ('y')")
    assert db.execute("SELECT count(*) FROM b").fetchone() == (2,)
    db.close()


def test_rebuild_names_the_row_a_new_foreign_key_refuses() -> None:
    db = _in_memory()
    db.execute("CREATE TABLE parent (id TEXT PRIMARY KEY) STRICT")
    db.execute("CREATE TABLE child (parent TEXT NOT NULL) STRICT")
    db.execute("INSERT INTO child VALUES ('missing')")
    with pytest.raises(migrations.RefusedRowError, match="row 1 of child names no row of parent"):
        migrations.rebuild(
            migrations.Schema(db),
            "child",
            "CREATE TABLE child (parent TEXT NOT NULL REFERENCES parent(id)) STRICT",
        )
    db.close()


def test_foreign_keys_are_on_for_the_writer_after_a_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control.sqlite3"
    _v3_with_a_thread(path, monkeypatch, "{}")
    store = _store(path)
    store.initialize()
    with store.write() as tx:
        assert tx.sql.execute("PRAGMA foreign_keys").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), store.write() as tx:
        tx.sql.execute(
            "INSERT INTO annotation_threads VALUES ('ann-2', 'doc-missing', '{}', '{}', 1.0)"
        )
    store.close()


def test_a_writer_that_opens_during_a_migration_waits_and_does_not_migrate_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control.sqlite3"
    _v3_with_a_thread(path, monkeypatch, "{}")
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []
    step = migrations.MIGRATIONS[2]

    def slow(schema: migrations.Schema) -> None:
        calls.append(threading.current_thread().name)
        step(schema)
        started.set()
        release.wait(5)

    monkeypatch.setattr(
        migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:2], slow, *migrations.MIGRATIONS[3:])
    )
    first = threading.Thread(target=lambda: _store(path).initialize(), name="first")
    first.start()
    assert started.wait(5)
    # A reader racing the migration sees the old version, and is told to open a writer.
    with pytest.raises(ControlError) as refused, _store(path, readonly=True).read():
        pass
    assert refused.value.kind == "unavailable"
    seen: list[int] = []

    def second() -> None:
        store = _store(path)
        store.initialize()
        with store.read() as tx:
            seen.append(tx.sql.execute("PRAGMA user_version").fetchone()[0])
        store.close()

    waiting = threading.Thread(target=second, name="second")
    waiting.start()
    time.sleep(0.3)
    assert not seen  # it waits on the first writer's transaction
    release.set()
    first.join(10)
    waiting.join(10)
    assert seen == [migrations.VERSION]
    assert calls == ["first"]
