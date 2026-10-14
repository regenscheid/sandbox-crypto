# SPDX-License-Identifier: MIT
"""Isolated records-only authority, raw-data, replay and storage probes."""

from __future__ import annotations

import json
import sqlite3
import threading
import tracemalloc
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from sandbox_science.orchestrator import delegation_records as module
from sandbox_science.orchestrator.delegation_records import (
    MAX_RAW,
    DelegationBrief,
    DelegationLimits,
    DelegationMutation,
    DelegationRecords,
    DelegationScope,
    HandbackAuthority,
    HostObservation,
    ParentAuthority,
    ProfileIntent,
    ReferenceClaim,
    RequestedLimits,
    StepPin,
    TrackPin,
)
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.store import DocumentStore


def parent(project: str = "p", session: str = "s", environment: str = "e", run: str = "main-one"):
    return ParentAuthority(
        scope=DelegationScope(project_id=project, session_id=session, environment_id=environment),
        parent_run_id=run,
    )


def brief(**changes: object) -> DelegationBrief:
    return DelegationBrief(
        **{
            "task": "Independently check the toy claim",
            "context_summary": "Only retained inputs are claims; execution is not enabled",
            "inputs": (),
            "profile": ProfileIntent(profile_id="check", profile_sha256="a" * 64),
            "limits": RequestedLimits(turns=8, wall_seconds=30, output_tokens=512),
            **changes,
        }
    )


def controller(prepared: DelegationMutation) -> HandbackAuthority:
    return HandbackAuthority(
        scope=prepared.scope,
        parent_run_id=prepared.parent_run_id,
        delegation_id=prepared.delegation_id,
        child_run_id=prepared.child_run_id,
    )


def returned(**changes: object) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "status": "partial",
            "summary": "One check remains",
            "results": [],
            "deviations": [],
            "open_questions": ["Missing observation"],
            **changes,
        },
        ensure_ascii=False,
    ).encode()


@pytest.fixture
def documents(tmp_path: Path) -> Generator[DocumentStore]:
    owner = DocumentStore(tmp_path / "control.sqlite3")
    yield owner
    owner.close()


@pytest.fixture
def records(documents: DocumentStore) -> DelegationRecords:
    return DelegationRecords(documents, initialize=True)


def prepared(records: DelegationRecords, operation: str = "prepare", **changes: object):
    return records.prepare(parent(), operation, brief(**changes))


def finish(
    records: DelegationRecords,
    value: DelegationMutation,
    raw: bytes | None = None,
    *,
    operation: str = "finish",
    ended: module.Ending = "returned",
) -> DelegationMutation:
    return records.finish(controller(value), operation, 1, HostObservation(ended=ended), raw)


def row_count(documents: DocumentStore) -> int:
    with documents._read() as db:
        assert db is not None
        return db.execute("SELECT count(*) FROM delegation_record_events").fetchone()[0]


def corrupt(
    documents: DocumentStore,
    sql: str,
    parameters: tuple[object, ...] = (),
    *,
    recharge: bool = True,
) -> None:
    with documents._write() as db:
        db.execute("DROP TRIGGER delegation_record_events_no_update")
        db.execute("DROP TRIGGER delegation_record_events_no_delete")
        db.execute(sql, parameters)
        # Keep unrelated accounting valid when probing deeper record/raw coherence.
        if recharge:
            statement = "UPDATE delegation_record_events SET bytes=" + module._CHARGE
            db.execute(statement)
        for verb in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER delegation_record_events_no_{verb.lower()} BEFORE {verb} ON "
                "delegation_record_events BEGIN SELECT RAISE(ABORT,'immutable'); END"
            )


def test_exact_intent_and_terminal_replay_after_reopen(records, documents, tmp_path, monkeypatch):
    value = prepared(records)
    assert value.action == "prepare"
    assert value.revision == 1
    assert value.observation is None
    assert value.scope == parent().scope
    assert value.child_run_id != value.parent_run_id
    outcome = finish(records, value, returned())
    assert outcome.observation == HostObservation(ended="returned", resolved_model=None)
    assert outcome.result is not None
    assert outcome.result.status == "partial"
    records.prepare(parent(), "later", brief(task="Later intent"))
    documents.close()
    reader = DocumentStore(tmp_path / "control.sqlite3")
    try:
        reopened = DelegationRecords(reader)
        monkeypatch.setattr(module, "new_id", lambda _: pytest.fail("replay allocated a new ID"))
        assert reopened.prepare(parent(), "prepare", brief()) == value
        assert finish(reopened, value, returned()) == outcome
        snapshot = reopened.get(parent(), value.delegation_id)
        assert snapshot.raw_payload == returned()
        assert snapshot.prepared == value
        assert snapshot.outcome == outcome
        assert row_count(reader) == 3
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (None, "missing_payload"),
        (b"", "invalid_json"),
        (b"\xff", "invalid_utf8"),
        (b"not json", "invalid_json"),
        (b'{"schema_version":1,"schema_version":1}', "duplicate_keys"),
        (b'{"object":{"a":1,"\\u0061":2}}', "duplicate_keys"),
        (returned(status="other"), "invalid_result"),
        (returned(schema_version=True), "invalid_result"),
        (returned(schema_version=1.0), "invalid_json"),
    ],
)
def test_raw_none_empty_invalid_and_duplicates_are_distinct(records, raw, error):
    value = prepared(records)
    outcome = finish(records, value, raw, ended="interrupted")
    assert outcome.observation is not None
    assert outcome.observation.ended == "interrupted"
    assert outcome.result is None
    assert outcome.result_error == error
    assert (outcome.raw_size is None) == (raw is None)
    assert records.get(parent(), value.delegation_id).raw_payload == raw


@pytest.mark.parametrize(
    "missing", ["schema_version", "status", "summary", "results", "deviations", "open_questions"]
)
def test_structured_fields_are_never_filled(records, missing):
    data = json.loads(returned())
    del data[missing]
    value = prepared(records)
    outcome = finish(records, value, json.dumps(data).encode())
    assert outcome.result is None
    assert outcome.result_error == "invalid_result"


def test_host_failure_and_child_complete_are_separate(records):
    value = prepared(records)
    result = finish(records, value, returned(status="complete"), ended="failed")
    assert result.observation is not None
    assert result.observation.ended == "failed"
    assert result.result is not None
    assert result.result.status == "complete"


def test_derived_result_budget_keeps_raw_and_host_ending(records):
    raw = returned(summary="x" * 4096, deviations=["y" * 4096, "z" * 4096])
    value = prepared(records)
    result = finish(records, value, raw, ended="limit_reached")
    assert result.result is None
    assert result.result_error == "result_too_large"
    assert records.get(parent(), value.delegation_id).raw_payload == raw
    assert result.observation is not None
    assert result.observation.ended == "limit_reached"


def test_foreign_result_claim_is_retained_as_invalid(records):
    value = prepared(records)
    raw = returned(
        results=[
            {
                "project_id": "foreign",
                "kind": "artifact",
                "reference_id": "a",
                "revision": 1,
                "sha256": "b" * 64,
            }
        ]
    )
    result = finish(records, value, raw)
    assert result.result is None
    assert result.result_error == "invalid_result"
    assert records.get(parent(), value.delegation_id).raw_payload == raw


def test_changed_request_and_terminal_overwrite_refuse(records, documents):
    value = prepared(records)
    with pytest.raises(DocumentError) as changed:
        prepared(records, task="Changed")
    assert changed.value.kind == "conflict"
    finish(records, value, returned())
    for operation, raw in [("finish", returned(summary="Changed")), ("other", returned())]:
        with pytest.raises(DocumentError) as conflict:
            finish(records, value, raw, operation=operation)
        assert conflict.value.kind == "conflict"
    assert row_count(documents) == 2


def test_finish_operation_cannot_be_reused_for_another_child(records, documents):
    first = prepared(records)
    finish(records, first, returned())
    second = prepared(records, "second")
    with pytest.raises(DocumentError, match="another handback"):
        finish(records, second, returned())
    assert records.get(parent(), second.delegation_id).outcome is None
    assert row_count(documents) == 3


@pytest.mark.parametrize(
    "authority", [parent(project="other"), parent(session="other"), parent(run="other")]
)
def test_wrong_parent_scope_has_no_record(records, authority):
    value = prepared(records)
    with pytest.raises(DocumentError) as absent:
        records.get(authority, value.delegation_id)
    assert absent.value.kind == "not_found"
    assert records.list(authority).items == ()


def test_environment_cannot_rebind_original_parent(records):
    value = prepared(records)
    for action in (
        lambda: records.get(parent(environment="other"), value.delegation_id),
        lambda: records.prepare(parent(environment="other"), "new", brief()),
        lambda: records.list(parent(environment="other")),
    ):
        with pytest.raises(DocumentError, match="original parent"):
            action()


@pytest.mark.parametrize("field", ["child_run_id", "parent_run_id", "delegation_id"])
def test_controller_must_match_generated_original_identity(records, field):
    value = prepared(records)
    authority = controller(value).model_copy(update={field: "other"})
    with pytest.raises(DocumentError):
        records.finish(authority, "finish", 1, HostObservation(ended="returned"), returned())
    assert records.get(parent(), value.delegation_id).outcome is None


def test_prepare_claims_cannot_cross_project(records):
    ref = ReferenceClaim(project_id="foreign", kind="execution", reference_id="e", attempt_id="a")
    with pytest.raises(DocumentError, match="another project"):
        prepared(records, inputs=(ref,))


def test_historical_track_pin_does_not_issue_current_grant(records, documents):
    pin = TrackPin(
        plan_id="plan-historical",
        version=1,
        track_id="track-a",
        steps=(StepPin(step_id="step", sha256="c" * 64),),
    )
    value = prepared(records, track=pin)
    assert records.get(parent(), value.delegation_id).prepared.brief == brief(track=pin)
    with documents._read() as db:
        assert db is not None
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'session_plan_%'"
            ).fetchone()[0]
            == 0
        )
    # No plan lookup or grant creation is implied, even for a nonexistent historical claim.


@pytest.mark.parametrize(
    ("field", "value"), [("extra", True), ("kind", "user"), ("parent_run_id", "USER")]
)
def test_copied_parent_authority_cannot_inject_role_or_fields(records, field, value):
    authority = parent().model_copy(update={field: value})
    with pytest.raises(DocumentError, match="invalid"):
        records.prepare(authority, "prepare", brief())


def test_host_subtypes_and_overrides_refuse_before_visits(records, documents):
    visits: list[str] = []

    class EvilText(str):
        def __len__(self):
            visits.append("len")
            return 1

        def encode(self, *args, **kwargs):
            visits.append("encode")
            return b"short"

    class EvilDict(dict):
        def items(self):
            visits.append("items")
            return super().items()

    class EvilTuple(tuple):
        def __iter__(self):
            visits.append("iter")
            return super().__iter__()

    class EvilBytes(bytes):
        def __len__(self):
            visits.append("bytes")
            return 0

    class EvilBrief(DelegationBrief):
        def __getattribute__(self, name: str) -> object:
            if name == "__dict__":
                visits.append("fields")
            return super().__getattribute__(name)

    invalids = (
        brief().model_copy(update={"task": EvilText("x" * 20_000)}),
        brief().model_copy(update={"profile": EvilDict({})}),
        brief().model_copy(update={"inputs": EvilTuple(())}),
        EvilBrief.model_construct(**brief().__dict__),
    )
    visits.clear()
    for invalid in invalids:
        with pytest.raises(DocumentError):
            records.prepare(parent(), "prepare", invalid)
    value = prepared(records)
    with pytest.raises(DocumentError):
        finish(records, value, EvilBytes(b"abc"))
    assert visits == []
    assert row_count(documents) == 1


@pytest.mark.parametrize("value", [True, 1.0, -1, 2**63])
def test_strict_cursor_and_revision(records, value):
    item = prepared(records)
    with pytest.raises(DocumentError):
        records.list(parent(), after=value)
    with pytest.raises(DocumentError):
        records.finish(controller(item), "finish", value, HostObservation(ended="returned"), None)


def test_raw_oversize_and_host_encoding_are_bounded_before_write(records, documents):
    value = prepared(records)
    with pytest.raises(DocumentError):
        finish(records, value, b"x" * (MAX_RAW + 1))
    for text in ("x" * 20_000_000, "synthetic-credential-\ud800"):
        invalid = brief().model_copy(update={"task": text})
        tracemalloc.start()
        try:
            with pytest.raises(DocumentError) as error:
                records.prepare(parent(), "oversize", invalid)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 200_000
        assert "synthetic-credential" not in repr(error.value)
    assert row_count(documents) == 1


def test_copied_aggregate_is_refused_before_json_encoder(records, monkeypatch):
    encoder = json.dumps
    aggregate_calls: list[object] = []

    def tracked(value, *args, **kwargs):
        if type(value) is dict and "inputs" in value:
            aggregate_calls.append(value)
        return encoder(value, *args, **kwargs)

    monkeypatch.setattr(json, "dumps", tracked)
    malformed = brief().model_copy(update={"task": "x" * 50_000, "context_summary": "y" * 50_000})
    with pytest.raises(DocumentError):
        records.prepare(parent(), "oversize", malformed)
    assert aggregate_calls == []


def test_concurrent_identical_and_distinct_prepares(tmp_path):
    first = DocumentStore(tmp_path / "control.sqlite3")
    second = DocumentStore(tmp_path / "control.sqlite3")
    try:
        a = DelegationRecords(first, initialize=True)
        b = DelegationRecords(second)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(store.prepare, parent(), "same", brief()) for store in (a, b)]
            one, replay = [future.result(timeout=5) for future in pending]
            assert one == replay
            pending = [
                pool.submit(store.prepare, parent(), op, brief())
                for store, op in ((a, "a"), (b, "b"))
            ]
            values = [future.result(timeout=5) for future in pending]
        assert sorted(value.sequence for value in values) == [2, 3]
        assert len({value.child_run_id for value in [one, *values]}) == 3
        assert row_count(first) == 3
    finally:
        second.close()
        first.close()


def test_concurrent_explicit_namespace_initializers(tmp_path):
    path = tmp_path / "control.sqlite3"
    original = DocumentStore(path)
    original.initialize()
    original.close()

    def initialize():
        owner = DocumentStore(path)
        try:
            store = DelegationRecords(owner, initialize=True)
            return store.prepare(parent(), "same", brief())
        finally:
            owner.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: initialize(), range(4)))
    assert all(value == values[0] for value in values)


def test_atomic_quota_has_no_orphan_or_eviction(tmp_path):
    limits = DelegationLimits(project_delegations=1, total_delegations=1)
    first = DocumentStore(tmp_path / "control.sqlite3")
    second = DocumentStore(tmp_path / "control.sqlite3")
    try:
        stores = (
            DelegationRecords(first, initialize=True, limits=limits),
            DelegationRecords(second, limits=limits),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(store.prepare, parent(), op, brief())
                for store, op in zip(stores, ("a", "b"), strict=True)
            ]
            successes = []
            errors = []
            for future in futures:
                try:
                    successes.append(future.result(timeout=5))
                except DocumentError as error:
                    errors.append(error.kind)
        assert errors == ["invalid_request"]
        assert len(successes) == 1
        assert row_count(first) == 1
        finish(stores[0], successes[0], returned())
        assert row_count(first) == 2
        assert stores[1].get(parent(), successes[0].delegation_id).outcome is not None
    finally:
        second.close()
        first.close()


def test_every_stored_variable_column_and_raw_byte_is_charged(records, documents):
    value = prepared(records)
    finish(records, value, returned(summary="unicode π"))
    with documents._read() as db:
        assert db is not None
        rows = db.execute(
            "SELECT project,session,environment,parent,action,delegation,child,operation,"
            "request_sha,metadata,raw,bytes FROM delegation_record_events"
        ).fetchall()
    assert len(rows) == 2
    for row in rows:
        expected = sum(len(text.encode("utf-8")) for text in row[:10])
        assert row[11] == expected + len(row[10] or b"") + 512
    limited = DelegationLimits(project_bytes=1024, total_bytes=1024)
    with pytest.raises(DocumentError, match="quota"):
        DelegationRecords(documents, initialize=True, limits=limited)
    assert row_count(documents) == 2


def test_post_insert_fault_rolls_back_and_same_operation_can_retry(records, documents, monkeypatch):
    append = records._append

    def fail(db, value, raw):
        append(db, value, raw)
        raise RuntimeError("synthetic post-insert fault")

    monkeypatch.setattr(records, "_append", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        prepared(records)
    assert row_count(documents) == 0
    monkeypatch.setattr(records, "_append", append)
    assert prepared(records).sequence == 1


def test_post_commit_unknown_ack_keeps_exact_intent_and_replays(records, documents, monkeypatch):
    write = documents._write

    @contextmanager
    def unknown_ack():
        with write() as db:
            yield db
        raise OSError("synthetic lost acknowledgement after actual commit")

    monkeypatch.setattr(documents, "_write", unknown_ack)
    with pytest.raises(OSError, match="after actual commit"):
        prepared(records)
    monkeypatch.setattr(documents, "_write", write)
    event = records.list(parent()).items[0]
    replay = prepared(records)
    assert replay.delegation_id == event.delegation_id
    assert replay.child_run_id == event.child_run_id
    assert records.get(parent(), replay.delegation_id).outcome is None
    assert row_count(documents) == 1


def test_pinned_parent_local_pages_and_missing_sequence(records, documents):
    one = prepared(records)
    finish(records, one, returned())
    first = records.list(parent(), limit=1)
    assert first.target == 2
    assert first.next_after == 1
    records.prepare(parent(run="other"), "other", brief())
    prepared(records, "later")
    second = records.list(parent(), after=first.next_after, target=first.target, limit=1)
    assert [item.sequence for item in second.items] == [2]
    assert second.next_after is None
    assert records.list(parent(run="other")).items[0].sequence == 1
    with pytest.raises(DocumentError):
        records.list(parent(), target=99)
    corrupt(
        documents, "DELETE FROM delegation_record_events WHERE parent='main-one' AND sequence=2"
    )
    with pytest.raises(DocumentError, match="cursor"):
        records.get(parent(), one.delegation_id)


def test_mutation_replay_cannot_substitute_another_same_author_record(records, documents):
    prepared(records, "first")
    prepared(records, "second", task="Different intent")
    with documents._read() as db:
        assert db is not None
        raw = db.execute(
            "SELECT metadata FROM delegation_record_events WHERE sequence=2"
        ).fetchone()[0]
    corrupt(documents, "UPDATE delegation_record_events SET metadata=? WHERE sequence=1", (raw,))
    with pytest.raises(DocumentError, match="retained"):
        prepared(records, "first")


def test_derived_result_must_reconstruct_from_exact_retained_raw(records, documents):
    item = prepared(records)
    finish(records, item, returned())
    with documents._read() as db:
        assert db is not None
        metadata = json.loads(
            db.execute(
                "SELECT metadata FROM delegation_record_events WHERE action='finish'"
            ).fetchone()[0]
        )
    metadata["result"]["summary"] = "Changed derived assertion"
    corrupt(
        documents,
        "UPDATE delegation_record_events SET metadata=? WHERE action='finish'",
        (json.dumps(metadata),),
    )
    with pytest.raises(DocumentError, match="retained"):
        records.get(parent(), item.delegation_id)


def test_reader_refuses_wrong_retained_byte_charge(records, documents):
    item = prepared(records)
    corrupt(documents, "UPDATE delegation_record_events SET bytes=bytes+1", recharge=False)
    with pytest.raises(DocumentError, match="retained"):
        records.get(parent(), item.delegation_id)


@pytest.mark.parametrize("column", ["metadata", "raw"])
def test_corrupt_twenty_mib_rows_are_clipped_before_python_parse(records, documents, column):
    item = prepared(records)
    finish(records, item, returned())
    value = "x" * 20_000_000 if column == "metadata" else b"x" * 20_000_000
    sql = {
        "metadata": "UPDATE delegation_record_events SET metadata=? WHERE action='finish'",
        "raw": "UPDATE delegation_record_events SET raw=? WHERE action='finish'",
    }[column]
    corrupt(documents, sql, (value,))
    tracemalloc.start()
    try:
        with pytest.raises(DocumentError):
            records.get(parent(), item.delegation_id)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 600_000


def test_orphan_history_is_counted_and_refused_even_in_another_parent(records, documents):
    one = prepared(records)
    finish(records, one, returned())
    prepared(records, "second")
    prepared(records, "third")
    corrupt(documents, "DELETE FROM delegation_record_events WHERE sequence=1")
    with pytest.raises(DocumentError, match="accounting"):
        records.prepare(parent(run="new-parent"), "new", brief())
    assert row_count(documents) == 3


def test_absent_readonly_namespace_and_partial_schema_never_auto_initialize(tmp_path):
    path = tmp_path / "control.sqlite3"
    reader = DocumentStore(path, readonly=True)
    try:
        with pytest.raises(DocumentError) as absent:
            DelegationRecords(reader)
        assert absent.value.kind == "not_found"
        assert not path.exists()
    finally:
        reader.close()
    owner = DocumentStore(path)
    try:
        with pytest.raises(DocumentError) as absent:
            DelegationRecords(owner)
        assert absent.value.kind == "not_found"
        records = DelegationRecords(owner, initialize=True)
        prepared(records)
        with owner._write() as db:
            db.execute("DROP TABLE delegation_record_events")
        for initialize in (False, True):
            with pytest.raises(DocumentError, match="incomplete"):
                DelegationRecords(owner, initialize=initialize)
    finally:
        owner.close()


def test_immutable_rows_and_store_does_not_own_close(records, documents):
    prepared(records)
    assert not hasattr(records, "close")
    with documents._write() as db:
        for sql in (
            "UPDATE delegation_record_events SET operation='other'",
            "DELETE FROM delegation_record_events",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(sql)
    assert row_count(documents) == 1


def test_actual_admitted_write_holds_borrowed_owner_until_commit(records, documents, monkeypatch):
    entered, release, close_started, closed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    append = records._append

    def blocked(db, value, raw):
        append(db, value, raw)
        entered.set()
        assert release.wait(5)

    def close():
        close_started.set()
        documents.close()
        closed.set()

    monkeypatch.setattr(records, "_append", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(prepared, records)
        assert entered.wait(5)
        assert write.cancel() is False  # Actual admitted Python/SQL work is still running.
        closing = pool.submit(close)
        assert close_started.wait(5)
        assert closed.wait(0.03) is False
        release.set()
        value = write.result(timeout=5)
        closing.result(timeout=5)
    assert closed.is_set()
    # The owner permits an intentional reopen; the module did not close it itself.
    assert records.get(parent(), value.delegation_id).prepared == value
