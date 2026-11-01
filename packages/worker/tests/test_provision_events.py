# SPDX-License-Identifier: MIT
"""Disposable event stores/scripted owners only; no real package/worker service."""

from __future__ import annotations

import asyncio
import json
import threading
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from sandbox_science.contracts import (
    ErrorInfo,
    ErrorKind,
    ExecutionContext,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
)
from sandbox_science.event_store import StoreReader, verify_store
from sandbox_science.policy.approvals import Approvals
from sandbox_science.testing import FakeClock
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.catalog import Catalog
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.permissions import Permissions
from sandbox_science.worker.provision_events import (
    MAX_ACTIVE,
    ObservationError,
    ProvisionEvents,
    checked_payload,
    project,
)
from sandbox_science.worker.provision_types import Operation as _Op
from sandbox_science.worker.provision_types import Stop as _Stop
from sandbox_science.worker.provisioner import Provisioner
from sandbox_science.worker.store import Store

NOW = datetime(2026, 10, 14, tzinfo=UTC)
PROVISION = "prv-" + "a" * 24


def record(**changes: object) -> ProvisionOperation:
    original = ProvisionOperation(
        provision_id=PROVISION,
        request=ProvisionRequest(
            operation_id="request",
            context=ExecutionContext(
                project_id="project", session_id="session", environment_id="environment"
            ),
            purpose="SYNTHETIC_PRIVATE_PURPOSE",
            conda=("example",),
        ),
        state=ProvisionState.QUEUED,
        submitted_at=NOW,
        installer_steps=(),
    )
    return original.model_copy(update=changes)


def test_projection_does_not_hash_or_serialize_unbounded_private_source():
    source = record()
    text = "SYNTHETIC_PRIVATE" * 200_000
    source = source.model_copy(
        update={"request": source.request.model_copy(update={"purpose": text})}
    )
    tracemalloc.start()
    payload = project(source, "provision.requested")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 100_000
    wire = json.dumps(payload)
    assert len(wire) < 4096
    assert "SYNTHETIC_PRIVATE" not in wire
    assert "purpose" not in payload
    assert "target" not in payload
    assert payload["authority"] == "observation-only"
    assert payload["revision_after"] is None
    assert payload["stage"] == "queue"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("version", True),
        ("operation_seq", True),
        ("execution_count", 129),
        ("operation_seq", 2**80),
        ("event_id", "a" * 64),
        ("provision_id", "X" * 129),
        ("state", "finished"),
        ("stage", "operator-approved"),
        ("submitted_at", "2026-99-99T00:00:00.000000Z"),
        ("trigger", "startup_classification"),
        ("authority", "operator"),
        ("context", {"project_id": "foreign"}),
    ],
)
def test_strict_bounded_payload_refuses_before_append(name: str, value: object):
    payload = project(record(), "provision.requested")
    payload[name] = value
    with pytest.raises(ObservationError):
        checked_payload("provision.requested", payload)


def test_payload_fields_are_independently_owned_and_kind_cannot_grant(tmp_path: Path):
    payload = project(record(), "provision.requested")
    owned = checked_payload("provision.requested", payload)
    assert owned["context"] is not payload["context"]
    permission = Permissions(tmp_path / "events.sqlite3")
    try:
        with pytest.raises(ObservationError, match="kind"):
            permission.observation("approval", payload)
        assert not permission.path.exists()
        permission.observation("provision.requested", payload)
        with StoreReader(permission.path) as reader:
            events = list(reader.records())
        assert len(events) == 1
        approvals = Approvals()
        approvals.load(events)
        assert approvals.answer_to(events[0].seq) is None
        assert events[0].source == "worker"
        assert verify_store(permission.path).ok
    finally:
        permission.close()


@pytest.mark.asyncio
async def test_stored_unknown_disabled_are_distinct_and_no_ambiguous_retry(tmp_path: Path):
    class LostAck(Permissions):
        calls = 0

        def observation(self, kind, payload):
            self.calls += 1
            super().observation(kind, payload)
            raise RuntimeError("SYNTHETIC_PRIVATE lost ack")

    writer = LostAck(tmp_path / "events.sqlite3")
    owner = ProvisionEvents(writer)
    try:
        delivery, interrupted = await owner.emit(record(), "provision.requested")
        assert delivery.status == "unknown"
        assert delivery.sequence is None
        assert interrupted is None
        await owner.close()
        assert writer.calls == 1
        with StoreReader(writer.path) as reader:
            assert len(list(reader.records())) == 1
        disabled, _ = await ProvisionEvents(None).emit(record(), "provision.requested")
        assert disabled.status == "disabled"
    finally:
        await owner.close()
        writer.close()


@pytest.mark.asyncio
async def test_cap_repeated_cancel_and_close_own_actual_threads_and_borrow_writer():
    entered = 0
    release = threading.Event()
    lock = threading.Lock()

    class Writer:
        closed = False

        def observation(self, kind, payload):
            nonlocal entered
            with lock:
                entered += 1
            assert release.wait(5)
            return 1

    writer = Writer()
    owner = ProvisionEvents(writer)
    tasks = [
        asyncio.create_task(owner.emit(record(), "provision.requested")) for _ in range(MAX_ACTIVE)
    ]
    close = None
    try:
        while entered < MAX_ACTIVE:
            await asyncio.sleep(0)
        delivery, _ = await owner.emit(record(), "provision.requested")
        assert delivery.status == "unavailable"
        tasks[0].cancel("original observation cancellation")
        await asyncio.sleep(0)
        tasks[0].cancel("later cancellation")
        close = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
        close.cancel("original close cancellation")
        await asyncio.sleep(0)
        close.cancel("later close cancellation")
        assert not tasks[0].done()
        assert not close.done()
        release.set()
        results = await asyncio.gather(*tasks)
        assert results[0][1] is not None
        assert results[0][1].args == ("original observation cancellation",)
        with pytest.raises(asyncio.CancelledError, match="original close cancellation"):
            await close
        assert not owner._work
        assert writer.closed is False
        after, _ = await owner.emit(record(), "provision.requested")
        assert after.status == "unavailable"
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if close is not None:
            await asyncio.gather(close, return_exceptions=True)
        await owner.close()


class ScriptedProvisioner(Provisioner):
    async def _operate(self, op: _Op):
        self._update(
            op,
            state=ProvisionState.AWAITING_PERMISSION,
            outcome=ProvisionOutcome.AWAITING_PERMISSION,
            permission_request=7,
        )
        await self._observe(op.record, "provision.awaiting_permission")
        raise _Stop(
            ProvisionOutcome.FAILED,
            "resolve_failed",
            "resolve",
            ErrorInfo(kind=ErrorKind.UNAVAILABLE, message="SYNTHETIC_PRIVATE_FAILURE"),
        )


@pytest.mark.asyncio
async def test_production_run_emits_original_saved_transitions_without_effect_inference(
    tmp_path: Path,
):
    store = Store(tmp_path / "worker.sqlite3")
    permission = Permissions(tmp_path / "events.sqlite3")
    manager = SimpleNamespace(
        store=store,
        blobs=Blobs(tmp_path / "blobs"),
        max_artifact_size=65536,
        revisions=SimpleNamespace(),
        refresh_package_gate=lambda env: None,
    )
    provisioner = ScriptedProvisioner(
        cast(ExecutionManager, manager),
        catalog=cast(Catalog, SimpleNamespace()),
        policies=None,
        permissions=permission,
        cache=None,
        clock=FakeClock(NOW),
    )
    source = record()
    store.insert_provision(source, "a" * 64)
    op = _Op(source, asyncio.get_running_loop().create_future())
    op.turn.set()
    try:
        await provisioner._run(op)
        with StoreReader(permission.path) as reader:
            events = [x for x in reader.records() if x.kind.startswith("provision.")]
        assert [x.kind for x in events] == [
            "provision.requested",
            "provision.awaiting_permission",
            "provision.finished",
        ]
        payloads = [x.data() for x in events]
        assert [x["operation_seq"] for x in payloads] == [0, 1, 2]
        assert all(x["provision_id"] == PROVISION for x in payloads)
        assert payloads[-1]["outcome"] == "failed"
        assert payloads[-1]["error_kind"] == "unavailable"
        assert payloads[-1]["revision_after"] is None
        assert "SYNTHETIC_PRIVATE" not in json.dumps(payloads)
        assert store.provision(PROVISION) == op.record
        assert verify_store(permission.path).ok
    finally:
        await provisioner.close()
        permission.close()
        store.close()


@pytest.mark.asyncio
async def test_startup_classifies_original_unfinished_once_without_backfill(tmp_path: Path):
    store = Store(tmp_path / "worker.sqlite3")
    permission = Permissions(tmp_path / "events.sqlite3")
    calls: list[str] = []

    async def sync(environment: str):
        calls.append(environment)

    manager = SimpleNamespace(
        store=store,
        blobs=Blobs(tmp_path / "blobs"),
        max_artifact_size=65536,
        revisions=SimpleNamespace(),
        sync_package_gate=sync,
    )
    provisioner = Provisioner(
        cast(ExecutionManager, manager),
        catalog=cast(Catalog, SimpleNamespace()),
        policies=None,
        permissions=permission,
        cache=None,
        clock=FakeClock(NOW),
    )
    store.insert_provision(record(), "a" * 64)
    try:
        await provisioner.start()
        await provisioner.start()
        with StoreReader(permission.path) as reader:
            [event] = list(reader.records())
        assert event.kind == "provision.finished"
        assert event.data()["trigger"] == "startup_classification"
        assert event.data()["reason"] == "worker_restarted"
        assert calls == ["environment"]
        finished = store.provision(PROVISION)
        assert finished is not None
        assert finished.state is ProvisionState.FINISHED
    finally:
        await provisioner.close()
        permission.close()
        store.close()


@pytest.mark.asyncio
async def test_observation_failures_do_not_change_original_operation_outcome_or_log_private_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    class Failing(Permissions):
        def observation(self, kind, payload):
            raise RuntimeError("SYNTHETIC_PRIVATE_WRITER_ERROR")

    store = Store(tmp_path / "worker.sqlite3")
    permission = Failing(tmp_path / "events.sqlite3")
    manager = SimpleNamespace(
        store=store,
        blobs=Blobs(tmp_path / "blobs"),
        max_artifact_size=65536,
        revisions=SimpleNamespace(),
        refresh_package_gate=lambda env: None,
    )
    provisioner = ScriptedProvisioner(
        cast(ExecutionManager, manager),
        catalog=cast(Catalog, SimpleNamespace()),
        policies=None,
        permissions=permission,
        cache=None,
        clock=FakeClock(NOW),
    )
    source = record()
    store.insert_provision(source, "a" * 64)
    op = _Op(source, asyncio.get_running_loop().create_future())
    op.turn.set()
    try:
        await provisioner._run(op)
        assert op.record.outcome is ProvisionOutcome.FAILED
        assert op.record.reason == "resolve_failed"
        assert op.record.seq == 2
        assert "SYNTHETIC_PRIVATE_WRITER_ERROR" not in caplog.text
        assert not permission.path.exists()
    finally:
        await provisioner.close()
        permission.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_following", [False, True])
async def test_terminal_observation_releases_queue_before_append_but_remains_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_following: bool
):
    entered = threading.Event()
    release = threading.Event()

    class BlockingTerminal(Permissions):
        def observation(self, kind, payload):
            if kind == "provision.finished":
                entered.set()
                assert release.wait(5)
            return 1

    store = Store(tmp_path / "worker.sqlite3")
    permission = BlockingTerminal(tmp_path / "events.sqlite3")
    manager = SimpleNamespace(
        store=store,
        blobs=Blobs(tmp_path / "blobs"),
        max_artifact_size=65536,
        revisions=SimpleNamespace(),
        refresh_package_gate=lambda env: None,
    )
    provisioner = ScriptedProvisioner(
        cast(ExecutionManager, manager),
        catalog=cast(Catalog, SimpleNamespace()),
        policies=None,
        permissions=permission,
        cache=None,
        clock=FakeClock(NOW),
        retain=0.001,
    )
    source = record()
    store.insert_provision(source, "a" * 64)
    op = _Op(source, asyncio.get_running_loop().create_future())
    following = _Op(
        source.model_copy(update={"provision_id": "prv-" + "b" * 24}),
        asyncio.get_running_loop().create_future(),
    )
    provisioner._ops[PROVISION] = op
    provisioner._queues["environment"] = [PROVISION]
    if has_following:
        provisioner._ops[following.record.provision_id] = following
        provisioner._queues["environment"].append(following.record.provision_id)
    op.turn.set()
    task = asyncio.create_task(provisioner._run(op))
    op.task = task
    drain = None
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0)
        assert op.record.state is ProvisionState.FINISHED
        assert store.provision(PROVISION) == op.record
        expected = (following.record.provision_id,) if has_following else ()
        assert (
            tuple(x.provision_id for x in provisioner.queue("environment").operations) == expected
        )
        assert following.turn.is_set() is has_following
        assert not task.done()
        assert len(provisioner.events._work) == 1
        tick = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_later(0, tick.set_result, None)
        await tick
        assert provisioner._ops.get(PROVISION) is op
        drain = asyncio.create_task(
            provisioner.close() if has_following else provisioner.forget("environment")
        )
        await asyncio.sleep(0)
        assert not drain.done()
        if has_following:
            drain.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if has_following:
            await asyncio.gather(drain, return_exceptions=True)
        else:
            await drain
        assert not provisioner.events._work
        assert store.provision(PROVISION) == op.record
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if drain is not None:
            await asyncio.gather(drain, return_exceptions=True)
        await provisioner.close()
        permission.close()
        store.close()


@pytest.mark.asyncio
async def test_actual_operation_close_retains_original_cancellation_and_store_until_append_drain(
    tmp_path: Path,
):
    entered = threading.Event()
    release = threading.Event()

    class Blocking(Permissions):
        def observation(self, kind, payload):
            entered.set()
            assert release.wait(5)
            return 1

    store = Store(tmp_path / "worker.sqlite3")
    permission = Blocking(tmp_path / "events.sqlite3")
    manager = SimpleNamespace(
        store=store,
        blobs=Blobs(tmp_path / "blobs"),
        max_artifact_size=65536,
        revisions=SimpleNamespace(),
        refresh_package_gate=lambda env: None,
    )
    provisioner = ScriptedProvisioner(
        cast(ExecutionManager, manager),
        catalog=cast(Catalog, SimpleNamespace()),
        policies=None,
        permissions=permission,
        cache=None,
        clock=FakeClock(NOW),
    )
    source = record()
    store.insert_provision(source, "a" * 64)
    op = _Op(source, asyncio.get_running_loop().create_future())
    op.turn.set()
    task = asyncio.create_task(provisioner._run(op))
    op.task = task
    provisioner._ops[PROVISION] = op
    close = None
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel("original operation cancellation")
        await asyncio.sleep(0)
        task.cancel("later operation cancellation")
        close = asyncio.create_task(provisioner.close())
        await asyncio.sleep(0)
        assert not close.done()
        assert not task.done()
        assert store.provision(PROVISION) is not None
        release.set()
        with pytest.raises(asyncio.CancelledError, match="original operation cancellation"):
            await task
        await close
        assert op.record.reason == "worker_restarted"
        assert not provisioner.events._work
        assert store.provision(PROVISION) == op.record
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if close is not None:
            await asyncio.gather(close, return_exceptions=True)
        await provisioner.close()
        permission.close()
        store.close()
