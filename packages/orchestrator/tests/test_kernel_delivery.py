# SPDX-License-Identifier: MIT
"""Owned MAIN kernel notices: exact provenance, delivery and save boundaries."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import tracemalloc
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandbox_science.contracts import (
    Capabilities,
    ExecutionContext,
    KernelInfo,
    KernelLineageEntry,
    KernelLineageObservation,
    Location,
)
from sandbox_science.contracts.kernel_lineage import KernelBirthPin
from sandbox_science.orchestrator import cli, projects
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.kernel_delivery import KernelDeliveryController
from sandbox_science.orchestrator.kernel_notice_bindings import (
    BindingError,
    KernelBindings,
    WorkerConfiguration,
)
from sandbox_science.orchestrator.kernel_notice_receipts import (
    KernelNoticeDelivery,
    validate_delivery,
)
from sandbox_science.orchestrator.loop import SYSTEM_PROMPT, run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore, RunRecord
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock, eventually

POLICY = "sha256:" + "a" * 64
AUTH = "Bearer test-secret-kernel-notice-configuration"
CONFIG = WorkerConfiguration("http://127.0.0.1:9999", AUTH)


@pytest.fixture
def project_store(tmp_path: Path) -> ProjectStore:
    store = ProjectStore(tmp_path / "state")
    store.create("project", "minimal", POLICY)
    store.open_session("project", "a", dedicated=False)
    return store


def test_lifetime_is_opaque_rotates_even_with_equal_timestamps_and_baseline_is_durable(
    tmp_path, monkeypatch
):
    class SameTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 13, tzinfo=UTC)

    monkeypatch.setattr(projects, "datetime", SameTime)
    store = ProjectStore(tmp_path)
    store.create("project", "minimal", POLICY)
    first = store.open_session("project", "a", dedicated=False)
    assert first.kernel_notice_lifetime is not None
    assert store.kernel_notice_baseline("project", "a") == first
    store.close_session("project", "a")
    second = store.open_session("project", "a", dedicated=False)
    assert second.opened_at == first.opened_at
    assert second.kernel_notice_lifetime != first.kernel_notice_lifetime
    assert store.open_session("project", "a", dedicated=False) == second


def test_legacy_open_gets_present_baseline_once_and_closed_never_baselines(project_store):
    target = project_store.root / "project.json"
    data = json.loads(target.read_bytes())
    del data["sessions"]["a"]["kernel_notice_lifetime"]
    target.write_text(json.dumps(data))
    assert project_store.get("project").sessions["a"].kernel_notice_lifetime is None
    first = project_store.kernel_notice_baseline("project", "a")
    assert first.kernel_notice_lifetime is not None
    assert ProjectStore(project_store.root.parent).kernel_notice_baseline("project", "a") == first
    project_store.close_session("project", "a")
    with pytest.raises(ProjectError, match="no open session"):
        project_store.kernel_notice_baseline("project", "a")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "oversize"])
def test_lifetime_baseline_bounded_registry_and_lock_special_files(project_store, kind, tmp_path):
    target = project_store.root / "project.json"
    if kind == "oversize":
        target.write_bytes(b" " * (1024 * 1024 + 1))
    else:
        target = project_store.root / "project.lock"
        target.unlink()
        if kind == "symlink":
            target.symlink_to(tmp_path / "untouched")
        elif kind == "fifo":
            os.mkfifo(target)
        else:
            target.mkdir()
    with pytest.raises(ProjectError):
        project_store.kernel_notice_baseline("project", "a")
    assert not (tmp_path / "untouched").exists()


def test_private_binding_stable_by_logical_session_and_not_an_auth_export(tmp_path):
    state = KernelBindings(tmp_path)
    first = state.ensure("project", "a", CONFIG)
    assert state.ensure("project", "a", CONFIG) == first
    assert KernelBindings(tmp_path).ensure("project", "a", CONFIG) == first
    assert state.ensure("project", "b", CONFIG) != first
    assert "test-secret" not in repr(CONFIG)
    for item in tmp_path.iterdir():
        if item.is_file() and item.name != "kernel-notice.key":
            raw = item.read_bytes()
            assert AUTH.encode() not in raw
            assert hashlib.sha256(AUTH.encode()).hexdigest().encode() not in raw
    assert (tmp_path / "kernel-notice.key").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "kernel-notice-bindings.sqlite3").stat().st_mode & 0o777 == 0o600
    with pytest.raises(BindingError, match="changed"):
        state.ensure("project", "a", WorkerConfiguration(CONFIG.endpoint, AUTH + "-changed"))
    assert state.ensure("project", "a", CONFIG) == first
    with sqlite3.connect(tmp_path / "kernel-notice-bindings.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM bindings").fetchone() == (2,)
        assert db.execute("SELECT diagnostic FROM bindings WHERE session='a'").fetchone() == (
            "binding_changed",
        )


def test_concurrent_private_initializers_share_one_key_and_binding(tmp_path):
    with ThreadPoolExecutor(max_workers=2) as executor:
        tasks = [
            executor.submit(KernelBindings(tmp_path).ensure, "project", "a", CONFIG)
            for _ in range(2)
        ]
        assert tasks[0].result() == tasks[1].result()
    with sqlite3.connect(tmp_path / "kernel-notice-bindings.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM bindings").fetchone() == (1,)


@pytest.mark.parametrize(
    "kind", ["missing", "wrong", "large", "mode", "symlink", "fifo", "directory", "hardlink"]
)
def test_existing_private_key_cannot_be_replaced_recreated_or_followed(tmp_path, kind):
    state = KernelBindings(tmp_path)
    state.ensure("project", "a", CONFIG)
    key = tmp_path / "kernel-notice.key"
    if kind == "mode":
        key.chmod(0o644)
    elif kind == "wrong":
        key.write_bytes(b"x" * 32)
    elif kind == "large":
        key.write_bytes(b"x" * (20 * 1024 * 1024))
    elif kind == "hardlink":
        os.link(key, tmp_path / "alias")
    else:
        key.unlink()
        if kind == "symlink":
            (tmp_path / "elsewhere").write_bytes(b"x" * 32)
            key.symlink_to(tmp_path / "elsewhere")
        elif kind == "fifo":
            os.mkfifo(key)
        elif kind == "directory":
            key.mkdir()
    with pytest.raises(BindingError) as caught:
        state.ensure("project", "a", CONFIG)
    assert AUTH not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    if kind == "missing":
        assert not key.exists()


def test_binding_corruption_is_sql_bounded_and_substituted_scope_cannot_prove(tmp_path):
    state = KernelBindings(tmp_path)
    state.ensure("project", "a", CONFIG)
    state.ensure("project", "b", CONFIG)
    database = tmp_path / "kernel-notice-bindings.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("DROP TRIGGER bindings_immutable")
        other = db.execute("SELECT binding,pin FROM bindings WHERE session='b'").fetchone()
        db.execute("UPDATE bindings SET binding=?,pin=? WHERE session='a'", other)
    with pytest.raises(BindingError, match="changed"):
        state.ensure("project", "a", CONFIG)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE bindings SET pin=? WHERE session='a'", ("x" * (20 * 1024 * 1024),))
    tracemalloc.start()
    try:
        with pytest.raises(BindingError):
            state.ensure("project", "a", CONFIG)
        assert tracemalloc.get_traced_memory()[1] < 256 * 1024
    finally:
        tracemalloc.stop()


def test_binding_quotas_count_empty_metadata_and_do_not_repin(tmp_path, monkeypatch):
    from sandbox_science.orchestrator import kernel_notice_bindings as module

    state = KernelBindings(tmp_path)
    original = state.ensure("project", "a", CONFIG)
    monkeypatch.setattr(module, "MAX_BINDINGS", 1)
    with pytest.raises(BindingError, match="unavailable"):
        state.ensure("project", "b", CONFIG)
    assert state.ensure("project", "a", CONFIG) == original
    monkeypatch.setattr(module, "MAX_BYTES", 256)
    with pytest.raises(BindingError, match="unavailable"):
        state.ensure("project", "a", CONFIG)


def test_lost_registry_with_retained_key_or_inbox_never_rebinds_configuration(tmp_path):
    state = KernelBindings(tmp_path)
    state.ensure("project", "a", CONFIG)
    database = tmp_path / "kernel-notice-bindings.sqlite3"
    database.unlink()
    for configuration in (CONFIG, WorkerConfiguration(CONFIG.endpoint, AUTH + "-changed")):
        with pytest.raises(BindingError):
            state.ensure("project", "a", configuration)
        assert not database.exists()
    (tmp_path / "kernel-notice.key").unlink()
    (tmp_path / "kernel-notices.sqlite3").touch(mode=0o600)
    with pytest.raises(BindingError):
        state.ensure("project", "a", CONFIG)
    assert not database.exists()


def test_interrupted_private_initialization_is_unavailable_without_silent_reset(
    tmp_path, monkeypatch
):
    original = os.write

    def failed_write(descriptor, data):
        original(descriptor, data[:4])
        raise OSError("test-secret-partial-key")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", failed_write)
        with pytest.raises(BindingError):
            KernelBindings(tmp_path).ensure("project", "a", CONFIG)
    assert (tmp_path / "kernel-notice.key").stat().st_size == 4
    with pytest.raises(BindingError):
        KernelBindings(tmp_path).ensure("project", "a", CONFIG)
    assert (tmp_path / "kernel-notice.key").stat().st_size == 4


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_private_sqlite_sidecars_are_proved_regular_before_sqlite_open(tmp_path, suffix, kind):
    state = KernelBindings(tmp_path)
    state.ensure("project", "a", CONFIG)
    target = tmp_path / ("kernel-notice-bindings.sqlite3" + suffix)
    sentinel = tmp_path / "outside"
    if kind == "symlink":
        sentinel.write_text("Leave unchanged")
        target.symlink_to(sentinel)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    with pytest.raises(BindingError):
        state.ensure("project", "a", CONFIG)
    if kind == "symlink":
        assert sentinel.read_text() == "Leave unchanged"


@pytest.mark.parametrize("value", ["x" * (20 * 1024 * 1024), "test-secret-\ud800", "é" * 9000])
def test_configuration_rejects_before_unbounded_utf8_allocation_and_hides_encoding_errors(value):
    tracemalloc.start()
    try:
        with pytest.raises(BindingError) as caught:
            WorkerConfiguration(CONFIG.endpoint, value).encoded()
        assert tracemalloc.get_traced_memory()[1] < 128 * 1024
        assert "test-secret" not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ or caught.value.__context__ is None
    finally:
        tracemalloc.stop()


def lineage(
    context, *, current: int | None = 2, incarnation="inc-" + "1" * 24, sandbox_id="vm-original"
):
    birth = KernelBirthPin(
        project_id=context.project_id,
        environment_id=context.environment_id,
        session_id=context.session_id,
        language="python",
        sandbox="ss-original",
        sandbox_id=sandbox_id,
        incarnation=incarnation,
        generation=1,
        birth_id="birth-" + "2" * 24,
        kernel_id="kernel-original",
        start_revision=0,
    )
    kernel = KernelInfo(
        kernel_id=birth.kernel_id,
        language="python",
        generation=1,
        start_revision=0,
        current_revision=current,
        revision_status="unknown" if current is None else "predates",
    )
    return KernelLineageObservation(
        project_id=context.project_id,
        environment_id=context.environment_id,
        session_id=context.session_id,
        current_revision=current,
        observed_at=datetime.now(UTC),
        kernels=(KernelLineageEntry(kernel=kernel, birth=birth, lineage_status="proved"),),
    )


class Passive:
    def __init__(self, value):
        self.value = value
        self.calls = 0
        self.closed = False
        self.during: Callable[[], None] | None = None

    async def observe_kernel_lineage(self, context):
        self.calls += 1
        if self.during is not None:
            self.during()
        return self.value

    async def close(self):
        self.closed = True


async def controller(project_store, **options):
    context = ExecutionContext(
        project_id="project",
        session_id="a",
        environment_id=project_store.get("project").sessions["a"].environment_id,
    )
    observer = Passive(lineage(context))
    source = WorkerClient(CONFIG.endpoint, AUTH.removeprefix("Bearer "))
    owner = KernelDeliveryController(
        project_store, context, "main-run", source, observer=observer, **options
    )
    await owner.initialize()
    assert owner._ready
    return owner, observer, source


def kept(owner, transcript):
    now = datetime.now(UTC)
    return RunRecord(
        run_id=owner.run_id,
        context=owner.context,
        task="Continue",
        started_at=now,
        ended_at=now,
        stopped=transcript.stopped,
        final_text=transcript.final_text,
        failure=transcript.failure,
        prompt_provenance=transcript.prompt_provenance,
        messages=transcript.messages,
        tools=transcript.tool_records,
        kernel_notice_delivery=transcript.kernel_notice_delivery,
    )


@pytest.mark.asyncio
async def test_actual_model_boundary_one_historical_prefix_save_before_exact_ack_and_legacy(
    project_store, monkeypatch
):
    owner, passive, source = await controller(project_store)
    toolbox = Toolbox(source, owner.context, kernel_notices=owner)
    plain = Toolbox(source, owner.context)
    assert toolbox.specs == plain.specs

    async def observed(*args, **kwargs):
        return ToolOutcome('{"observed":"synthetic"}', {"observed": "synthetic"})

    monkeypatch.setattr(toolbox, "call", observed)

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            result = await super().complete(messages, tools)
            passive.value = lineage(owner.context, current=3)
            return result

    model = Model(
        [
            ModelTurn(None, (ToolCall("read", "read_file", '{"path":"synthetic"}'),)),
            ModelTurn("Observed historical evidence"),
        ]
    )
    try:
        transcript = await run_task(model, toolbox, "Continue", run_id=owner.run_id)
        assert transcript.stopped == "answered"
        assert transcript.kernel_notice_delivery is not None
        assert len(transcript.kernel_notice_delivery.claim.notices) == 1
        assert transcript.kernel_notice_delivery.claim.notices[0].observation.current_revision == 2
        for request in model.requests:
            assert request[0]["content"] == SYSTEM_PROMPT
            assert (
                sum("Historical kernel package observations" in str(m["content"]) for m in request)
                == 1
            )
            assert AUTH not in json.dumps(request)
        record = kept(owner, transcript)
        assert (
            RunRecord.model_validate_json(record.to_json()).kernel_notice_delivery
            == owner.delivered
        )
        assert owner.inbox is not None
        assert owner.actor is not None
        assert owner.inbox.status(owner.actor).pending == 2
        await source.close()  # Passive owner retains its separate capability through save/ACK.
        await owner.save(lambda: project_store.save_run(record))
        assert await owner.acknowledge_saved()
        assert owner.inbox.status(owner.actor).pending == 1
        raw = record.model_dump(mode="json")
        raw.pop("kernel_notice_delivery")
        assert RunRecord.model_validate(raw).kernel_notice_delivery is None
        assert (
            project_store.run(
                "project", "a", project_store.runs("project", "a")[0].name
            ).kernel_notice_delivery
            == record.kernel_notice_delivery
        )
    finally:
        await owner.close()
        await source.close()
    assert passive.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["empty", "unknown", "foreign", "partial", "current_unknown", "copied"]
)
async def test_unknown_partial_forged_or_missing_lineage_admits_no_notice_data(project_store, kind):
    owner, passive, source = await controller(project_store)
    value = passive.value
    if kind == "empty":
        passive.value = value.model_copy(update={"kernels": ()})
    elif kind in {"unknown", "partial"}:
        unknown = KernelLineageEntry(
            kernel=value.kernels[0].kernel.model_copy(
                update={"kernel_id": "unknown", "language": "sage"}
            ),
            lineage_status="unknown",
            unknown_reason="missing_birth",
        )
        passive.value = value.model_copy(
            update={"kernels": (unknown,) if kind == "unknown" else (*value.kernels, unknown)}
        )
    elif kind == "foreign":
        passive.value = value.model_copy(update={"session_id": "other"})
    elif kind == "current_unknown":
        passive.value = lineage(owner.context, current=None)
    else:
        assert value.kernels[0].birth is not None
        passive.value = value.model_copy(
            update={
                "kernels": (
                    value.kernels[0].model_copy(
                        update={
                            "birth": value.kernels[0].birth.model_copy(
                                update={"start_revision": True}
                            )
                        }
                    ),
                )
            }
        )
    try:
        assert await owner.before_model() is None
        assert owner.actor is None
        assert owner.claim is None
        with sqlite3.connect(project_store.root.parent / "kernel-notices.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM kernel_notice_items").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM kernel_notice_scopes").fetchone() == (0,)
        with sqlite3.connect(project_store.root.parent / "kernel-notice-bindings.sqlite3") as db:
            assert db.execute("SELECT diagnostic FROM bindings").fetchone() == (
                "unknown_revision" if kind == "current_unknown" else "unknown_lineage",
            )
    finally:
        await owner.close()
        await source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_delivery", [False, True])
async def test_failed_model_only_retains_prior_successful_delivery(project_store, after_delivery):
    owner, passive, source = await controller(project_store)

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            if not after_delivery or self.requests:
                raise RuntimeError("test-secret-provider-error-body")
            return await super().complete(messages, tools)

    model = Model([ModelTurn(None, (ToolCall("read", "read_file", "{}"),))])
    toolbox = Toolbox(source, owner.context, kernel_notices=owner)

    async def observed(*args, **kwargs):
        return ToolOutcome("{}")

    toolbox.call = observed
    try:
        transcript = await run_task(model, toolbox, "Continue", run_id=owner.run_id)
        assert transcript.failure == "model"
        assert (transcript.kernel_notice_delivery is not None) == after_delivery
        assert "test-secret-provider" not in json.dumps(transcript.messages)
        assert not owner._acked
    finally:
        await owner.close()
        await source.close()
    assert passive.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["reopen", "config", "vm", "empty"])
async def test_late_ack_needs_fresh_original_binding_and_preserves_historical_record(
    project_store, kind
):
    owner, passive, source = await controller(project_store)
    try:
        delivery = await owner.before_model()
        assert delivery is not None
        owner.model_delivered()
        if kind == "reopen":
            project_store.close_session("project", "a")
            project_store.open_session("project", "a", dedicated=False)
        elif kind == "config":
            source._headers["Authorization"] = AUTH + "-changed"
        elif kind == "vm":
            passive.value = lineage(owner.context, sandbox_id="replacement-vm")
        else:
            passive.value = passive.value.model_copy(update={"kernels": ()})
        assert not await owner.acknowledge_saved()
        assert owner.delivered == delivery
        assert owner.inbox is not None
        assert owner.actor is not None
        assert owner.inbox.status(owner.actor).pending == 1
        assert not owner._acked
    finally:
        await owner.close()
        await source.close()


@pytest.mark.asyncio
async def test_close_reopen_across_rpc_cannot_admit_or_enroll_old_scope(project_store):
    owner, passive, source = await controller(project_store)

    def replace():
        project_store.close_session("project", "a")
        project_store.open_session("project", "a", dedicated=False)

    passive.during = replace
    try:
        assert await owner.before_model() is None
        assert owner.actor is None
        assert owner.claim is None
    finally:
        await owner.close()
        await source.close()


@pytest.mark.asyncio
async def test_save_cancellation_drains_actual_fsync_owner_keeps_audit_and_never_acknowledges(
    project_store, monkeypatch
):
    owner, passive, source = await controller(project_store)
    transcript = await run_task(
        ScriptedModel([ModelTurn("Observed")]),
        Toolbox(source, owner.context, kernel_notices=owner),
        "Continue",
        run_id=owner.run_id,
    )
    record = kept(owner, transcript)
    entered, release = threading.Event(), threading.Event()
    original = project_store.save_run

    def blocked():
        entered.set()
        assert release.wait(5)
        return original(record)

    call = asyncio.create_task(owner.save(blocked))
    assert await asyncio.to_thread(entered.wait, 2)
    descriptor = owner._file
    call.cancel()
    call.cancel()
    closing = asyncio.create_task(owner.close())
    try:
        await asyncio.sleep(0.02)
        closing.cancel()
        assert not call.done()
        assert not closing.done()
        os.fstat(descriptor)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "default executor free"), 0.5)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert project_store.runs("project", "a")
    assert not owner._acked
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)
    assert passive.closed
    await source.close()


@pytest.mark.asyncio
async def test_proof_timeout_returns_unknown_but_keeps_actual_snapshot_owner_until_close(
    project_store, monkeypatch
):
    # The snapshot blocks until released, so the proof can only time out.
    owner, _passive, source = await controller(project_store, proof_timeout=0.02)
    entered, release = threading.Event(), threading.Event()
    retained = []
    original = owner._snapshot

    def blocked():
        result = original()
        retained.extend([result._descriptor, result._root])
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(owner, "_snapshot", blocked)
    try:
        assert await owner.before_model() is None
        assert entered.is_set()
        assert owner._proof is not None
        assert not owner._proof.done()
        assert await owner.before_model() is None
        closing = asyncio.create_task(owner.close())
        await asyncio.sleep(0.01)
        closing.cancel()
        assert not closing.done()
        for descriptor in retained:
            os.fstat(descriptor)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    for descriptor in retained:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)
    assert not owner._lane.pending
    await source.close()


@pytest.mark.asyncio
async def test_historical_renewal_does_not_queue_behind_long_save(project_store, monkeypatch):
    clock = FakeClock()
    owner, _passive, source = await controller(project_store, clock=clock, renew_seconds=10)
    entered, release = threading.Event(), threading.Event()
    renewed = threading.Event()
    assert owner.inbox is not None
    original = owner.inbox.renew

    def observed(*args):
        result = original(*args)
        renewed.set()
        return result

    monkeypatch.setattr(owner.inbox, "renew", observed)

    def save():
        entered.set()
        assert release.wait(5)

    call = None
    try:
        assert await owner.before_model() is not None
        owner.model_delivered()
        call = asyncio.create_task(owner.save(save))
        assert await asyncio.to_thread(entered.wait, 2)
        await eventually(lambda: clock.sleepers == 1, describe="the renewal waiting")
        assert not renewed.is_set()
        clock.advance(10)
        assert await asyncio.to_thread(renewed.wait, 2)
    finally:
        release.set()
        if call is not None:
            await call
        await owner.close()
        await source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "cleanup", "save_fail", "cancel_save"])
async def test_cli_actual_save_other_ack_order_and_postadmission_cancel(
    project_store, tmp_path, monkeypatch, mode, capsys
):
    context = ExecutionContext(
        project_id="project",
        session_id="a",
        environment_id=project_store.get("project").sessions["a"].environment_id,
    )
    events = []
    owners = []
    passive = Passive(lineage(context))
    closed = passive.close

    async def passive_close():
        events.append("passive-close")
        await closed()

    passive.close = passive_close

    class Client(WorkerClient):
        async def capabilities(self, environment_id: str) -> Capabilities:
            return Capabilities(
                protocol=1,
                location=Location(
                    isolation_evidence=None,
                    pool_id="p",
                    backend="local",
                    sandbox_id="synthetic",
                    hostname="synthetic",
                ),
                languages=(),
                features=(),
            )

        async def close(self):
            events.append("research-close")
            await super().close()

    class Model(ScriptedModel):
        def __init__(self, *args, **kwargs):
            super().__init__([ModelTurn("Observed historical kernel data")])

        async def close(self):
            events.append("model-close")
            if mode == "cleanup":
                raise RuntimeError("test-secret-provider-cleanup")

    def factory(projects, resolved, run, client):
        result = KernelDeliveryController(projects, resolved, run, client, observer=passive)
        owners.append(result)
        return result

    async def resolved(*args):
        return context

    original_save = ProjectStore.save_run
    entered, release = threading.Event(), threading.Event()

    def save(store, record):
        if mode == "save_fail":
            raise OSError("synthetic save refusal")
        if mode == "cancel_save":
            entered.set()
            assert release.wait(5)
        result = original_save(store, record)
        events.append("saved")
        return result

    original_jobs_ack = JobState.acknowledge

    def jobs_ack(*args):
        assert "saved" in events
        events.append("other-ack")
        return original_jobs_ack(*args)

    from sandbox_science.orchestrator.kernel_notices import KernelNotices

    original_kernel_ack = KernelNotices.acknowledge

    def kernel_ack(*args):
        assert "saved" in events
        assert "other-ack" in events
        events.append("kernel-ack")
        return original_kernel_ack(*args)

    monkeypatch.setattr(cli, "WorkerClient", Client)
    monkeypatch.setattr(cli, "OpenAICompatibleModel", Model)
    monkeypatch.setattr(cli, "KernelDeliveryController", factory)
    monkeypatch.setattr(cli, "_context", resolved)
    monkeypatch.setattr(cli, "_read_secret", lambda _: AUTH.removeprefix("Bearer "))
    monkeypatch.setattr(ProjectStore, "save_run", save)
    monkeypatch.setattr(JobState, "acknowledge", jobs_ack)
    monkeypatch.setattr(KernelNotices, "acknowledge", kernel_ack)
    args = argparse.Namespace(
        script=None,
        model_url="http://unused.invalid",
        model="main",
        api_key_file=None,
        run_id="main-run",
        worker=CONFIG.endpoint,
        token_file=None,
        allow_unisolated=True,
        state_dir=project_store.root.parent,
        watcher_model=None,
        reviewer_model=None,
        math_node=None,
        watcher_calls_per_hour=1,
        watcher_first_check=1,
        watcher_interval=1,
        task="Continue",
        max_turns=1,
        transcript=tmp_path / "transcript.json",
    )
    if mode == "cancel_save":
        call = asyncio.create_task(cli._run(args))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            call.cancel()
            call.cancel()
            await asyncio.sleep(0.02)
            assert not call.done()
            assert not passive.closed
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        assert await cli._run(args) == (0 if mode == "success" else 1)
    assert passive.closed
    assert len(owners) == 1
    if mode == "save_fail":
        assert not project_store.runs("project", "a")
        assert "saved" not in events
    else:
        entries = project_store.runs("project", "a")
        assert len(entries) == 1
        record = project_store.run("project", "a", entries[0].name)
        assert record.kernel_notice_delivery is not None
        assert record.failure == ("cleanup" if mode == "cleanup" else None)
        assert events.index("research-close") < events.index("saved")
    if mode in {"save_fail", "cancel_save"}:
        assert "kernel-ack" not in events
        assert "other-ack" not in events
    else:
        assert events.index("saved") < events.index("other-ack") < events.index("kernel-ack")
    assert AUTH not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_delivery_receipt_copied_corruption_scope_and_allocation_bounds(project_store):
    owner, _passive, source = await controller(project_store)
    try:
        value = await owner.before_model()
        assert value is not None
        original = value.claim
        for change in ({"run_id": "other"}, {"scope_digest": "0" * 64}):
            forged = value.model_copy(update={"claim": original.model_copy(update=change)})
            if "run_id" in change:
                validated = validate_delivery(forged)
                transcript = await run_task(
                    ScriptedModel([ModelTurn("Observed")]),
                    Toolbox(source, owner.context),
                    "Continue",
                    run_id=owner.run_id,
                )
                transcript.kernel_notice_delivery = validated
                with pytest.raises(ValueError, match="recorded run scope"):
                    kept(owner, transcript)
            else:
                with pytest.raises(ValueError, match="exact pin"):
                    validate_delivery(forged)
        large = "test-secret-" + "x" * (20 * 1024 * 1024)
        forged = value.model_copy(
            update={"claim": original.model_copy(update={"scope_digest": large})}
        )
        tracemalloc.start()
        try:
            with pytest.raises(ValueError, match="invalid or oversized") as caught:
                validate_delivery(forged)
            assert tracemalloc.get_traced_memory()[1] < 128 * 1024
            assert "test-secret" not in str(caught.value)
        finally:
            tracemalloc.stop()
    finally:
        await owner.close()
        await source.close()


@pytest.mark.parametrize(
    "name",
    [
        base + suffix
        for base in ("kernel-notice-bindings.sqlite3", "kernel-notices.sqlite3")
        for suffix in ("-wal", "-shm", "-journal")
    ],
)
def test_any_retained_private_sqlite_sidecar_refuses_fresh_configuration_state(tmp_path, name):
    retained = tmp_path / name
    retained.touch(mode=0o600)
    with pytest.raises(BindingError):
        KernelBindings(tmp_path).ensure("project", "a", CONFIG)
    assert retained.exists()
    assert not (tmp_path / "kernel-notice-bindings.sqlite3").exists()
    assert not (tmp_path / "kernel-notice.key").exists()


@pytest.mark.asyncio
async def test_previously_delivered_context_stays_historical_when_later_proof_is_unavailable(
    project_store, monkeypatch
):
    owner, passive, source = await controller(project_store)

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            result = await super().complete(messages, tools)
            passive.value = passive.value.model_copy(update={"kernels": ()})
            return result

    toolbox = Toolbox(source, owner.context, kernel_notices=owner)

    async def observed(*args, **kwargs):
        return ToolOutcome('{"observed":"synthetic"}')

    monkeypatch.setattr(toolbox, "call", observed)
    model = Model(
        [
            ModelTurn(None, (ToolCall("read", "read_file", "{}"),)),
            ModelTurn("Historical evidence remains dated"),
        ]
    )
    try:
        transcript = await run_task(model, toolbox, "Continue", run_id=owner.run_id)
        assert transcript.stopped == "answered"
        assert len(model.requests) == 2
        assert transcript.kernel_notice_delivery is not None
        assert "Historical kernel package observations" in str(model.requests[1])
        assert not await owner.acknowledge_saved()
    finally:
        await owner.close()
        await source.close()


@pytest.mark.asyncio
async def test_copied_large_lineage_is_rejected_before_serialization(project_store):
    owner, passive, source = await controller(project_store)
    try:
        huge = "x" * (20 * 1024 * 1024)
        passive.value = passive.value.model_copy(update={"project_id": huge})
        tracemalloc.start()
        try:
            assert await owner.before_model() is None
            assert tracemalloc.get_traced_memory()[1] < 256 * 1024
        finally:
            tracemalloc.stop()
        assert owner.claim is None
    finally:
        await owner.close()
        await source.close()


@pytest.mark.asyncio
async def test_receipt_requires_exactly_one_versioned_historical_user_input(
    project_store, monkeypatch
):
    from sandbox_science.orchestrator import kernel_notice_receipts as receipts

    owner, _passive, source = await controller(project_store)
    try:
        transcript = await run_task(
            ScriptedModel([ModelTurn("Observed")]),
            Toolbox(source, owner.context, kernel_notices=owner),
            "Continue",
            run_id=owner.run_id,
        )
        record = kept(owner, transcript)
        delivery = record.kernel_notice_delivery
        assert delivery is not None
        assert delivery.version == 1
        injected = {"role": "user", "content": delivery.text()}
        assert sum(message == injected for message in record.messages) == 1
        original = record.to_json()
        # An installed default prose alias or model JSON dump implementation is
        # not the historical v1 formatter. These changes cannot rewrite its text.
        monkeypatch.setattr(receipts, "NOTICE_PROSE", "future default prose")
        monkeypatch.setattr(KernelNoticeDelivery, "model_dump_json", lambda *_a, **_k: "changed")
        assert RunRecord.model_validate_json(original).kernel_notice_delivery == delivery
        assert validate_delivery(delivery).text() == injected["content"]
        data = json.loads(original)
        without = [message for message in data["messages"] if message != injected]
        for messages in (
            without,
            [*without, {"role": "user", "content": injected["content"] + " changed"}],
            [*without, {"role": "assistant", "content": injected["content"]}],
            [*data["messages"], injected],
        ):
            with pytest.raises(ValueError, match="exactly one retained historical user input"):
                RunRecord.model_validate_json(json.dumps({**data, "messages": messages}))
        assert (
            RunRecord.model_validate_json(
                json.dumps({**data, "messages": without, "kernel_notice_delivery": None})
            ).kernel_notice_delivery
            is None
        )
    finally:
        await owner.close()
        await source.close()


@pytest.mark.asyncio
async def test_copied_model_extras_subtypes_and_noninteger_format_are_refused_before_getters(
    project_store,
):
    owner, passive, source = await controller(project_store)
    visits = []

    class Subtype(KernelNoticeDelivery):
        def __getattribute__(self, name):
            if name == "claim":
                visits.append(name)
            return super().__getattribute__(name)

    try:
        value = await owner.before_model()
        assert value is not None
        candidates = [
            value.model_copy(update={"undeclared": "synthetic"}),
            value.model_copy(
                update={"claim": value.claim.model_copy(update={"undeclared": "synthetic"})}
            ),
            Subtype.model_construct(claim=value.claim),
            *(value.model_copy(update={"version": version}) for version in (True, 1.0, "1", 2)),
        ]
        for candidate in candidates:
            with pytest.raises(
                ValueError, match=r"invalid or oversized|unsupported kernel delivery"
            ):
                validate_delivery(candidate)
        assert not visits
        passive.value = passive.value.model_copy(update={"undeclared": "synthetic"})
        assert await owner.before_model() is None
    finally:
        await owner.close()
        await source.close()


@pytest.mark.asyncio
async def test_capacity_hold_is_not_erased_by_later_language_and_existing_prefix_can_drain(
    project_store,
):
    from sandbox_science.orchestrator.kernel_notices import KernelNoticeLimits, KernelNotices

    owner, passive, source = await controller(project_store)
    try:
        assert owner.inbox is not None
        owner.inbox = KernelNotices(owner.inbox.path, limits=KernelNoticeLimits(per_scope=1))
        first = await owner.before_model()
        assert first is not None
        assert owner.actor is not None
        # Free the lease only, leaving its unACKed historical prefix retained.
        owner.inbox.release(owner.actor, first.claim)
        owner.claim = owner._injected = None
        python = lineage(owner.context, current=3)
        original = python.kernels[0]
        assert original.birth is not None
        sage = KernelLineageEntry(
            kernel=original.kernel.model_copy(
                update={"language": "sage", "kernel_id": "sage-kernel"}
            ),
            birth=original.birth.model_copy(
                update={"language": "sage", "kernel_id": "sage-kernel"}
            ),
            lineage_status="proved",
        )
        passive.value = python.model_copy(update={"kernels": (*python.kernels, sage)})
        repeated = await owner.before_model()
        assert repeated is not None
        assert repeated.claim.pins == first.claim.pins
        assert owner.inbox.status(owner.actor).held == "capacity"
        with sqlite3.connect(owner.inbox.path) as db:
            assert db.execute(
                "SELECT language,revision FROM kernel_notice_watermarks"
            ).fetchall() == [("python", 2)]
    finally:
        await owner.close()
        await source.close()


@pytest.mark.asyncio
async def test_save_canceled_before_private_lane_admission_does_not_start_queued_write(
    project_store,
):
    owner, _passive, source = await controller(project_store)
    entered, release = threading.Event(), threading.Event()
    writes = []

    def blocked():
        entered.set()
        assert release.wait(3)

    first = asyncio.create_task(owner.save(blocked))
    assert await asyncio.to_thread(entered.wait, 2)
    queued = asyncio.create_task(owner.save(lambda: writes.append("unexpected")))
    await asyncio.sleep(0)
    queued.cancel()
    queued.cancel()
    await asyncio.sleep(0.01)
    assert not queued.done()
    release.set()
    try:
        await first
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert not writes
    finally:
        await owner.close()
        await source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("later", ["cancel", "exit"])
async def test_cleanup_origin_interrupt_and_prior_parent_cancellation_own_actual_close(
    project_store,
    later,
):
    owner, passive, source = await controller(project_store)
    entered, release = asyncio.Event(), asyncio.Event()
    cleanup_error = asyncio.CancelledError("cleanup") if later == "cancel" else SystemExit(9)

    async def closing():
        entered.set()
        await release.wait()
        raise cleanup_error

    passive.close = closing
    task = asyncio.create_task(owner.close())
    await entered.wait()
    parent_error = None
    if later == "exit":
        task.cancel("original")
        await asyncio.sleep(0)
        task.cancel("later")
        parent_error = "original"
        assert not task.done()
    release.set()
    try:
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        if parent_error is None:
            assert caught.value is cleanup_error
        else:
            assert caught.value.args == (parent_error,)
        assert owner._file == owner._root == -1
        assert owner._lane.closed
        assert owner._save_lane.closed
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_owned_passive_client_configuration_change_refuses_first_input(project_store):
    context = ExecutionContext(
        project_id="project",
        session_id="a",
        environment_id=project_store.get("project").sessions["a"].environment_id,
    )
    source = WorkerClient(CONFIG.endpoint, AUTH.removeprefix("Bearer "))
    owner = KernelDeliveryController(project_store, context, "main-run", source)
    await owner.initialize()
    assert isinstance(owner.client, WorkerClient)
    owner.client._headers["Authorization"] += "-changed"
    try:
        assert await owner.before_model() is None
        assert owner.actor is None
    finally:
        await owner.close()
        await source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "replacement", "truncate", "table"])
async def test_initialized_original_inbox_loss_never_recreates_or_repins_history(
    project_store,
    damage,
):
    owner, _passive, source = await controller(project_store)
    value = await owner.before_model()
    assert value is not None
    context, binding = owner.context, owner.binding
    await owner.close()
    await source.close()
    path = project_store.root.parent / "kernel-notices.sqlite3"
    if damage in {"missing", "replacement"}:
        # Retain the old inode in this process so an allocator cannot reuse it.
        old = os.open(path, os.O_RDONLY)
        path.unlink()
        if damage == "replacement":
            path.touch(mode=0o600)
    else:
        old = -1
        if damage == "truncate":
            path.write_bytes(b"")
        else:
            with sqlite3.connect(path) as db:
                db.execute("DROP TABLE kernel_notice_items")
    before = path.stat().st_size if path.exists() else None
    source = WorkerClient(CONFIG.endpoint, AUTH.removeprefix("Bearer "))
    again = KernelDeliveryController(
        project_store, context, "another-run", source, observer=Passive(lineage(context))
    )
    try:
        await again.initialize()
        assert not again._ready
        assert await again.before_model() is None
        assert again.binding == binding
        assert (path.stat().st_size if path.exists() else None) == before
        if damage == "table":
            with sqlite3.connect(path) as db:
                assert db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name='kernel_notice_items'"
                ).fetchone() == (0,)
    finally:
        await again.close()
        await source.close()
        if old >= 0:
            os.close(old)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_orphan_inbox_sidecar_after_binding_enrollment_refuses_initial_schema(tmp_path, suffix):
    from sandbox_science.orchestrator.kernel_notices import KernelNotices

    state = KernelBindings(tmp_path)
    state.ensure("project", "a", CONFIG)
    retained = tmp_path / ("kernel-notices.sqlite3" + suffix)
    retained.touch(mode=0o600)
    with pytest.raises(BindingError, match="unavailable"), state.inbox() as (_descriptor, first):
        KernelNotices(tmp_path / "kernel-notices.sqlite3", initialize=first)
    assert retained.exists()
    assert not (tmp_path / "kernel-notices.sqlite3").exists()


@pytest.mark.parametrize("point", ["create", "schema"])
def test_first_inbox_initialization_phase_is_durable_before_mutation_and_crash_stays_held(
    tmp_path,
    monkeypatch,
    point,
):
    from sandbox_science.orchestrator.kernel_notices import KernelNotices

    state = KernelBindings(tmp_path)
    state.ensure("project", "a", CONFIG)
    original = KernelBindings._file

    def failed_create(root, name, *, create=False):
        if name == "kernel-notices.sqlite3" and create:
            with sqlite3.connect(tmp_path / "kernel-notice-bindings.sqlite3") as db:
                assert db.execute("SELECT inbox_phase FROM meta").fetchone() == ("initializing",)
            raise OSError("synthetic partial creation")
        return original(root, name, create=create)

    def interrupted_schema():
        with state.inbox() as (_descriptor, _first):
            with sqlite3.connect(tmp_path / "kernel-notice-bindings.sqlite3") as db:
                assert db.execute("SELECT inbox_phase FROM meta").fetchone() == ("initializing",)
            raise OSError("synthetic partial schema")

    with monkeypatch.context() as patch:
        if point == "create":
            patch.setattr(KernelBindings, "_file", staticmethod(failed_create))
        with pytest.raises(BindingError, match="unavailable"):
            interrupted_schema()
    path = tmp_path / "kernel-notices.sqlite3"
    before = path.stat().st_size if path.exists() else None
    with pytest.raises(BindingError, match="unavailable"), state.inbox() as (_descriptor, first):
        KernelNotices(path, initialize=first)
    assert (path.stat().st_size if path.exists() else None) == before


def test_concurrent_first_inbox_initializers_commit_one_original_identity(tmp_path):
    from sandbox_science.orchestrator.kernel_notices import KernelNotices

    KernelBindings(tmp_path).ensure("project", "a", CONFIG)

    def initialize():
        with KernelBindings(tmp_path).inbox() as (descriptor, first):
            KernelNotices(tmp_path / "kernel-notices.sqlite3", initialize=first)
            info = os.fstat(descriptor)
            return first, info.st_dev, info.st_ino

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: initialize(), range(4)))
    assert sum(result[0] for result in results) == 1
    assert len({result[1:] for result in results}) == 1
    with sqlite3.connect(tmp_path / "kernel-notice-bindings.sqlite3") as db:
        assert db.execute("SELECT inbox_phase,inbox_device,inbox_inode FROM meta").fetchone() == (
            "ready",
            *results[0][1:],
        )


def test_primitive_subclasses_refuse_before_overrides_or_json_encoding(monkeypatch):
    from sandbox_science.orchestrator import kernel_notice_receipts as receipts

    visits = []

    class Text(str):
        def __len__(self):
            visits.append("length")
            return 1

        def encode(self, *args, **kwargs):
            visits.append("encoding")
            return super().encode(*args, **kwargs)

    class Mapping(dict):
        def items(self):
            visits.append("mapping")
            return super().items()

    class Sequence(tuple):
        def __iter__(self):
            visits.append("tuple")
            return super().__iter__()

    class Array(list):
        def __iter__(self):
            visits.append("list")
            return super().__iter__()

    class Timestamp(datetime):
        def isoformat(self, *args, **kwargs):
            visits.append("time")
            return super().isoformat(*args, **kwargs)

    def encoding(*args, **kwargs):
        visits.append("json")
        raise AssertionError("rejected host values must not reach encoding")

    values = (
        Text("x" * 9000),
        Mapping(x=1),
        Sequence((1,)),
        Array([1]),
        Timestamp(2026, 10, 13, tzinfo=UTC),
        "x" * 9000,
    )
    monkeypatch.setattr(json, "dumps", encoding)
    for value in values:
        with pytest.raises(ValueError, match="invalid or oversized"):
            receipts.bounded_kernel_data(value)
    assert not visits
