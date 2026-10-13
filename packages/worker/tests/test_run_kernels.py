# SPDX-License-Identifier: MIT
"""Original run bindings only: isolated records/fake channels, never kernel execution."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tracemalloc
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.run_kernels import (
    RunKernelBinding,
    RunKernelEnrollment,
    RunKernelScope,
    run_kernel_binding_sha256,
)
from sandbox_science.worker import run_kernels as module
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import Backend
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager, _Environment
from sandbox_science.worker.run_kernels import RunKernelBindings
from sandbox_science.worker.store import Store

# Explicit isolated tests inspect the borrowed store/channel lifetime seams.
# pyright: reportPrivateUsage=false
INC = "inc-" + "a" * 24
VM = ("ss-synthetic-original", "vm-synthetic-original")
TOKEN = "test-secret-unused-binding"  # noqa: S105 - inert synthetic channel fixture, never connected


def scope(**changes: str) -> RunKernelScope:
    return RunKernelScope(
        **{
            "project_id": "p",
            "session_id": "s",
            "environment_id": "e",
            "parent_run_id": "run-parent",
            "delegation_id": "dlg-original",
            "child_run_id": "run-child",
            **changes,
        }
    )


def binding(**changes: object) -> RunKernelBinding:
    return RunKernelBinding.model_validate(
        {
            "scope": scope(),
            "sandbox": VM[0],
            "sandbox_id": VM[1],
            "incarnation": INC,
            **changes,
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> Generator[Store]:
    owner = Store(tmp_path / "worker.sqlite3")
    try:
        yield owner
    finally:
        owner.close()


@pytest.fixture
def records(store: Store) -> RunKernelBindings:
    return RunKernelBindings(store)


def count(store: Store) -> int:
    return store._db.execute("SELECT count(*) FROM run_kernel_bindings").fetchone()[0]


def test_no_namespace_or_legacy_schema_change_until_explicit_enrollment(store, records):
    before = list(store._db.iterdump())
    with pytest.raises(WorkerError) as error:
        records.get(scope())
    assert error.value.kind == ErrorKind.NOT_FOUND
    with pytest.raises(WorkerError):
        records.enroll(binding(), "enroll")
    assert list(store._db.iterdump()) == before
    value = records.enroll(binding(), "enroll", initialize=True)
    assert records.get(scope()) == value
    assert value.binding.scope.session_id == "s"
    assert value.binding.scope.child_run_id != value.binding.scope.parent_run_id


def test_exact_historical_replay_after_reopen_and_no_birth_claim(store, records, tmp_path):
    value = records.enroll(binding(), "enroll", initialize=True)
    store.close()
    reopened = Store(tmp_path / "worker.sqlite3")
    try:
        other = RunKernelBindings(reopened)
        assert other.enroll(binding(), "enroll") == value
        assert other.get(scope()) == value
        assert count(reopened) == 1
        assert reopened._db.execute("SELECT count(*) FROM kernels").fetchone()[0] == 0
        assert (
            reopened._db.execute("SELECT count(*) FROM kernel_start_revisions").fetchone()[0] == 0
        )
        assert reopened._db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "field", ["session_id", "environment_id", "parent_run_id", "delegation_id"]
)
def test_original_child_cannot_be_rebound(records, store, field):
    original = records.enroll(binding(), "enroll", initialize=True)
    changed = binding(scope=scope(**{field: "replacement"}))
    with pytest.raises(WorkerError) as conflict:
        records.enroll(changed, "enroll")
    assert conflict.value.kind == ErrorKind.CONFLICT
    assert records.get(scope()) == original
    assert count(store) == 1


@pytest.mark.parametrize("field", ["sandbox", "sandbox_id", "incarnation"])
def test_original_endpoint_never_rebases(records, field):
    original = records.enroll(binding(), "enroll", initialize=True)
    value = "inc-" + "b" * 24 if field == "incarnation" else "replacement"
    with pytest.raises(WorkerError) as conflict:
        records.enroll(binding(**{field: value}), "enroll")
    assert conflict.value.kind == ErrorKind.CONFLICT
    assert records.get(scope()) == original


@pytest.mark.parametrize("field", ["project_id", "session_id", "environment_id", "parent_run_id"])
def test_wrong_scope_reads_no_original_binding(records, field):
    records.enroll(binding(), "enroll", initialize=True)
    with pytest.raises(WorkerError) as absent:
        records.get(scope(**{field: "other"}))
    assert absent.value.kind == ErrorKind.NOT_FOUND


def test_original_delegation_and_operation_cannot_alias_another_child(records, store):
    records.enroll(binding(), "enroll", initialize=True)
    for changed in (
        scope(child_run_id="other-child"),
        scope(child_run_id="other-child", delegation_id="other-delegation"),
    ):
        with pytest.raises(WorkerError) as error:
            records.enroll(binding(scope=changed), "enroll")
        assert error.value.kind == ErrorKind.CONFLICT
    assert count(store) == 1


def test_identity_hash_covers_every_scope_and_original_endpoint_field():
    original = binding()
    digest = run_kernel_binding_sha256(original)
    for field in RunKernelScope.model_fields:
        assert run_kernel_binding_sha256(binding(scope=scope(**{field: "other"}))) != digest
    for field in ("sandbox", "sandbox_id", "incarnation"):
        value = "inc-" + "b" * 24 if field == "incarnation" else "other"
        assert run_kernel_binding_sha256(binding(**{field: value})) != digest


def test_copied_types_extras_and_subclasses_refuse_before_callbacks(records, store):
    visits: list[str] = []

    class BadText(str):
        def __len__(self) -> int:
            visits.append("len")
            return 1

        def encode(self, *args: object, **kwargs: object) -> bytes:
            visits.append("encode")
            return b"short"

    class BadBinding(RunKernelBinding):
        def __getattribute__(self, name: str) -> object:
            visits.append("attribute")
            return super().__getattribute__(name)

    invalid = (
        binding().model_copy(update={"sandbox": BadText("synthetic")}),
        binding().model_copy(update={"extra": True}),
        binding(scope=scope().model_copy(update={"extra": True})),
        BadBinding.model_construct(**binding().__dict__),
    )
    visits.clear()
    before = list(store._db.iterdump())
    for value in invalid:
        with pytest.raises(WorkerError) as error:
            records.enroll(value, "enroll", initialize=True)
        assert error.value.kind == ErrorKind.INVALID_REQUEST
    assert visits == []
    assert list(store._db.iterdump()) == before


@pytest.mark.parametrize("value", [True, 1.0, "1", 2])
def test_retained_version_is_explicit_exact_integer(records, value):
    raw = records.enroll(binding(), "enroll", initialize=True).model_dump(mode="json")
    raw["format_version"] = value
    with pytest.raises(ValidationError):
        RunKernelEnrollment.model_validate_json(json.dumps(raw), strict=True)


def test_missing_retained_version_is_not_invented(records):
    raw = records.enroll(binding(), "enroll", initialize=True).model_dump(mode="json")
    del raw["format_version"]
    with pytest.raises(ValidationError):
        RunKernelEnrollment.model_validate_json(json.dumps(raw), strict=True)


def test_duplicate_or_substituted_retained_request_refuses(records, store):
    first = records.enroll(binding(), "enroll", initialize=True)
    store._db.execute("DROP TRIGGER run_kernel_bindings_no_update")
    raw = first.model_dump(mode="json")
    raw["operation_id"] = "different"
    store._db.execute("UPDATE run_kernel_bindings SET record=?", (json.dumps(raw),))
    statement = "UPDATE run_kernel_bindings SET bytes=" + module._CHARGE  # noqa: S608 - fixed internal fragment
    store._db.execute(statement)
    store._db.execute(
        "CREATE TRIGGER run_kernel_bindings_no_update BEFORE UPDATE ON "
        "run_kernel_bindings BEGIN SELECT RAISE(ABORT,'immutable'); END"
    )
    with pytest.raises(WorkerError) as error:
        records.get(scope())
    assert error.value.kind == ErrorKind.UNAVAILABLE
    # A distinct, valid-looking same-value duplicate is not normalized on a later read.
    store._db.execute("DROP TRIGGER run_kernel_bindings_no_update")
    text = first.model_dump_json().replace(
        '"format_version":1', '"format_version":1,"format_version":1'
    )
    store._db.execute("UPDATE run_kernel_bindings SET record=?", (text,))
    store._db.execute(statement)
    store._db.execute(
        "CREATE TRIGGER run_kernel_bindings_no_update BEFORE UPDATE ON "
        "run_kernel_bindings BEGIN SELECT RAISE(ABORT,'immutable'); END"
    )
    with pytest.raises(WorkerError) as error:
        records.get(scope())
    assert error.value.kind == ErrorKind.UNAVAILABLE


def test_quota_counts_each_record_and_rolls_back_new_append(records, store, monkeypatch):
    one = records.enroll(binding(), "enroll", initialize=True)
    monkeypatch.setattr(module, "MAX_PROJECT_BINDINGS", 1)
    with pytest.raises(WorkerError) as error:
        records.enroll(
            binding(scope=scope(child_run_id="child-two", delegation_id="dlg-two")), "two"
        )
    assert error.value.kind == ErrorKind.UNAVAILABLE
    assert count(store) == 1
    assert records.enroll(binding(), "enroll") == one
    with store._transaction():
        row = store._db.execute(
            "SELECT project,session,environment,parent,delegation,child,"
            "operation,record,bytes FROM run_kernel_bindings"
        ).fetchone()
    assert row[-1] == sum(len(value.encode("utf-8")) for value in row[:-1]) + 512


def test_partial_schema_and_immutable_rows_refuse(records, store):
    records.enroll(binding(), "enroll", initialize=True)
    for sql in ("UPDATE run_kernel_bindings SET child='other'", "DELETE FROM run_kernel_bindings"):
        with pytest.raises(sqlite3.IntegrityError):
            store._db.execute(sql)
    store._db.execute("DROP TABLE run_kernel_bindings")
    for initialize in (False, True):
        with pytest.raises(WorkerError) as error:
            records.enroll(binding(), "enroll", initialize=initialize)
        assert error.value.kind == ErrorKind.UNAVAILABLE


def test_same_request_concurrent_first_initialization_is_one_record(tmp_path):
    owners = [Store(tmp_path / "concurrent.sqlite3") for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(RunKernelBindings(owner).enroll, binding(), "same", initialize=True)
                for owner in owners
            ]
            first, replay = [future.result(timeout=5) for future in futures]
        assert first == replay
        assert count(owners[0]) == 1
    finally:
        for owner in owners:
            owner.close()


def test_proof_failure_inside_transaction_rolls_back_namespace_and_record(records, store):
    calls = 0

    def validate() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise WorkerError(ErrorKind.UNAVAILABLE, "synthetic binding change")

    before = list(store._db.iterdump())
    with pytest.raises(WorkerError):
        records.enroll(binding(), "enroll", initialize=True, validate=validate)
    assert list(store._db.iterdump()) == before


def test_actual_commit_lost_ack_preserves_original_identity(records, store, monkeypatch):
    transaction = store._transaction

    @contextmanager
    def lost_ack():
        with transaction():
            yield
        raise OSError("synthetic acknowledgement lost after actual commit")

    monkeypatch.setattr(store, "_transaction", lost_ack)
    with pytest.raises(OSError, match="after actual commit"):
        records.enroll(binding(), "enroll", initialize=True)
    monkeypatch.setattr(store, "_transaction", transaction)
    retained = records.get(scope())
    assert records.enroll(binding(), "enroll") == retained
    assert count(store) == 1


def test_corrupt_record_clipped_before_python_and_legacy_unaffected(records, store, tmp_path):
    records.enroll(binding(), "enroll", initialize=True)
    store._db.execute("DROP TRIGGER run_kernel_bindings_no_update")
    store._db.execute("UPDATE run_kernel_bindings SET record=?", ("x" * 2_000_000,))
    store._db.execute(
        "CREATE TRIGGER run_kernel_bindings_no_update BEFORE UPDATE ON "
        "run_kernel_bindings BEGIN SELECT RAISE(ABORT,'immutable'); END"
    )
    tracemalloc.start()
    try:
        with pytest.raises(WorkerError) as error:
            records.get(scope())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert error.value.kind == ErrorKind.UNAVAILABLE
    assert peak < 100_000
    # Unwired derived history never enters ordinary Store startup or kernel reads.
    assert store.kernel("e", "s", "python") is None
    assert store.kernel_starts.retained("e", "s", "python") == (None, "missing_birth")
    store.close()
    reopened = Store(tmp_path / "worker.sqlite3")
    try:
        assert reopened.kernel("e", "s", "python") is None
        with pytest.raises(WorkerError):
            RunKernelBindings(reopened).get(scope())
    finally:
        reopened.close()


@pytest.fixture
def manager(store: Store, tmp_path: Path) -> Generator[ExecutionManager]:
    value = ExecutionManager(
        store,
        cast(Backend, object()),
        Blobs(tmp_path / "blobs"),
        pool_id="synthetic",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )

    async def event(_: Message) -> None:
        pytest.fail("no executor event is allowed")

    async def closed(_: ExecutorChannel) -> None:
        pytest.fail("no executor close is allowed")

    channel = ExecutorChannel([], event, closed, daemon_token=TOKEN)
    channel.incarnation = INC
    value._environments["e"] = _Environment(channel=channel, last_used=123.0)
    value.daemon_identity = lambda _: VM
    store.remember_endpoint("e", INC, VM)
    try:
        yield value
    finally:
        value._blob_pool.shutdown(wait=True, cancel_futures=True)


def test_manager_enrolls_existing_channel_without_activity_rpc_or_kernel(manager, store):
    environment = manager._environments["e"]
    value = manager.enroll_run_kernel(scope(), "enroll", initialize=True)
    assert value.binding == binding()
    assert (environment.requests, environment.last_used) == (0, 123.0)
    assert manager._tasks == set()
    assert manager._kernel_observations == {}
    assert store._db.execute("SELECT count(*) FROM kernels").fetchone()[0] == 0
    assert store._db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
    assert manager.enroll_run_kernel(scope(), "enroll") == value


@pytest.mark.parametrize(
    "unavailable", ["cold", "closed", "detaching", "locked", "unmanaged", "endpoint"]
)
def test_manager_missing_or_ambiguous_proof_never_initializes(manager, store, unavailable):
    environment = manager._environments["e"]
    channel = environment.channel
    assert channel is not None
    if unavailable == "cold":
        manager._environments.clear()
    elif unavailable == "closed":
        channel.closed = True
    elif unavailable == "detaching":
        channel.detaching = True
    elif unavailable == "locked":
        # The synchronous proof tests the lifecycle lock without waiting for it.
        asyncio.run(environment.lock.acquire())
    elif unavailable == "unmanaged":
        manager.daemon_identity = None
    else:
        store.forget_endpoint("e")
    before = list(store._db.iterdump())
    with pytest.raises(WorkerError) as error:
        manager.enroll_run_kernel(scope(), "enroll", initialize=True)
    assert error.value.kind == ErrorKind.UNAVAILABLE
    assert list(store._db.iterdump()) == before
    assert environment.requests == 0
    assert environment.last_used == 123.0


@pytest.mark.parametrize("column", ["incarnation", "sandbox", "sandbox_id"])
def test_manager_retained_endpoint_types_and_bounds_refuse(manager, store, column):
    query = {
        "incarnation": "UPDATE executor_endpoints SET incarnation=? WHERE environment_id='e'",
        "sandbox": "UPDATE executor_endpoints SET sandbox=? WHERE environment_id='e'",
        "sandbox_id": "UPDATE executor_endpoints SET sandbox_id=? WHERE environment_id='e'",
    }[column]
    # Original BLOB values cannot be normalized to plausible TEXT identity.
    expected = INC if column == "incarnation" else VM[0] if column == "sandbox" else VM[1]
    store._db.execute(query, (expected.encode(),))
    with pytest.raises(WorkerError) as error:
        manager.enroll_run_kernel(scope(), "enroll", initialize=True)
    assert error.value.kind == ErrorKind.UNAVAILABLE


def test_manager_new_incarnation_refuses_but_original_historical_record_survives(manager, store):
    value = manager.enroll_run_kernel(scope(), "enroll", initialize=True)
    channel = manager._environments["e"].channel
    assert channel is not None
    channel.incarnation = "inc-" + "b" * 24
    store.remember_endpoint("e", channel.incarnation, VM)
    with pytest.raises(WorkerError) as error:
        manager.enroll_run_kernel(scope(), "enroll")
    assert error.value.kind == ErrorKind.CONFLICT
    assert RunKernelBindings(store).get(scope()) == value


def test_postcommit_binding_change_is_unknown_with_original_audit(manager, store):
    visits = 0

    def identity(_: str) -> tuple[str, str]:
        nonlocal visits
        visits += 1
        return VM if visits < 4 else (VM[0], "vm-replacement")

    manager.daemon_identity = identity
    with pytest.raises(WorkerError) as error:
        manager.enroll_run_kernel(scope(), "enroll", initialize=True)
    assert error.value.kind == ErrorKind.UNAVAILABLE
    assert RunKernelBindings(store).get(scope()).binding == binding()
    assert count(store) == 1


def test_managed_identity_subtype_refuses_before_equality(manager, store):
    visits: list[str] = []

    class BadText(str):
        def __eq__(self, other: object) -> bool:
            visits.append("equality")
            return True

    manager.daemon_identity = lambda _: (BadText("foreign"), VM[1])
    before = list(store._db.iterdump())
    with pytest.raises(WorkerError) as error:
        manager.enroll_run_kernel(scope(), "enroll", initialize=True)
    assert error.value.kind == ErrorKind.UNAVAILABLE
    assert visits == []
    assert list(store._db.iterdump()) == before


def test_large_retained_endpoint_refuses_without_full_python_allocation(manager, store):
    store._db.execute("UPDATE executor_endpoints SET sandbox=?", ("x" * 2_000_000,))
    tracemalloc.start()
    try:
        with pytest.raises(WorkerError) as error:
            manager.enroll_run_kernel(scope(), "enroll", initialize=True)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert error.value.kind == ErrorKind.UNAVAILABLE
    assert peak < 100_000


def test_same_incarnation_replacement_channel_preserves_only_original_binding(manager, store):
    original = manager.enroll_run_kernel(scope(), "enroll", initialize=True)
    previous = manager._environments["e"].channel
    assert previous is not None
    replacement = ExecutorChannel([], previous._on_event, previous._on_closed, daemon_token=TOKEN)
    replacement.incarnation = INC
    manager._environments["e"].channel = replacement
    assert manager.enroll_run_kernel(scope(), "enroll") == original
    assert count(store) == 1


def test_managed_proof_change_before_commit_rolls_back_first_enrollment(manager, store):
    visits = 0

    def identity(_: str) -> tuple[str, str]:
        nonlocal visits
        visits += 1
        return VM if visits < 3 else (VM[0], "vm-replacement")

    manager.daemon_identity = identity
    before = list(store._db.iterdump())
    with pytest.raises(WorkerError) as error:
        manager.enroll_run_kernel(scope(), "enroll", initialize=True)
    assert error.value.kind == ErrorKind.UNAVAILABLE
    assert list(store._db.iterdump()) == before
