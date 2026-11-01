# SPDX-License-Identifier: MIT
"""Bounded files/SQLite and scripted step owners only; no real installer or service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import tracemalloc
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import (
    Execution,
    ExecutionContext,
    ExecutionState,
    ProvisionOperation,
    ProvisionRequest,
    ProvisionState,
    ShellRequest,
)
from sandbox_science.contracts.provisioning import InstallerLabel, InstallerStep
from sandbox_science.contracts.worker import Result, StreamOutput
from sandbox_science.worker import installer_records as logs
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.catalog import Catalog
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.identity import execution_digest
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.provision_types import Operation as _Op
from sandbox_science.worker.provision_types import Stop as _Stop
from sandbox_science.worker.provisioner import Provisioner
from sandbox_science.worker.store import Store

NOW = datetime(2026, 10, 14, tzinfo=UTC)
PROVISION = "prv-" + "a" * 24
EXECUTION = "exe-" + "b" * 24
ATTEMPT = "att-" + "c" * 24
CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="e")


def fixture_step(label: InstallerLabel = "install-conda", stdout: str = "Transaction finished\n"):
    execution = Execution(
        execution_id=EXECUTION,
        attempt_id=ATTEMPT,
        operation_id=PROVISION + "." + label,
        context=CONTEXT,
        kind="shell",
        origin=PROVISION,
        state=ExecutionState.SUCCEEDED,
        submitted_at=NOW,
        started_at=NOW,
        ended_at=NOW,
        result=Result(
            exit_code=0,
            elapsed_seconds=0.25,
            stdout=StreamOutput(
                name="stdout", text=stdout, total_bytes=len(stdout.encode("utf-8", "surrogatepass"))
            ),
            stderr=StreamOutput(name="stderr", text="", total_bytes=0),
        ),
    )
    operation = ProvisionOperation(
        provision_id=PROVISION,
        request=ProvisionRequest(
            operation_id="request", context=CONTEXT, purpose="synthetic pilot", conda=("example",)
        ),
        state=ProvisionState.RESOLVING,
        submitted_at=NOW,
        executions=(EXECUTION,),
        installer_steps=(
            InstallerStep(
                label=label,
                installer="micromamba" if label.endswith("conda") else "pip",
                operation_id=execution.operation_id,
                execution_id=EXECUTION,
                attempt_id=ATTEMPT,
            ),
        ),
    )
    return operation, execution


@pytest.mark.parametrize(
    ("label", "text", "expected"),
    [
        ("version-conda", "2.9.0\n", "2.9.0"),
        ("version-pip", "pip 25.2 from /synthetic/private/path (python 3.13)\n", "25.2"),
        ("version-conda", "untrusted-token\n", None),
        ("version-pip", "pip invalid from /path (python 3.13)\n", None),
        ("version-conda", "2.9.0\nextra\n", None),
        ("version-conda", "L" * 4097, None),
    ],
)
def test_versions_are_actual_bounded_tokens_not_guesses(
    label: InstallerLabel, text: str, expected: str | None
):
    op, ex = fixture_step(label, text)
    prepared = logs.prepare(op, ex, label)
    assert prepared.step.observed_version == expected
    assert "/synthetic/private/path" not in prepared.raw.decode()
    assert "untrusted-token" not in prepared.raw.decode()


def test_arbitrary_output_credentials_paths_and_raw_hashes_are_not_retained():
    sentinel = "SYNTHETIC_CREDENTIAL_VALUE"
    text = (
        f"Transaction finished\nhttps://user:{sentinel}@example.invalid/path?token={sentinel}\n"
        f"Authorization: Bearer {sentinel}\nTransaction finished {sentinel}\n"
    )
    op, ex = fixture_step(stdout=text)
    prepared = logs.prepare(op, ex, "install-conda")
    payload = json.loads(prepared.raw)
    assert sentinel.encode() not in prepared.raw
    assert hashlib.sha256(text.encode()).hexdigest().encode() not in prepared.raw
    assert payload["streams"]["stdout"]["fixed_messages"] == ["Transaction finished"]
    assert payload["streams"]["stdout"]["omitted_scanned_lines"] == 3
    assert payload["complete_raw_log"] is False
    assert prepared.artifact.produced_by is None
    assert prepared.artifact.source_path == f"provisioning/{PROVISION}/install-conda.json"
    assert prepared.artifact.sha256 == hashlib.sha256(prepared.raw).hexdigest()


def test_huge_untrusted_stream_is_not_aggregate_encoded_or_split():
    op, ex = fixture_step(stdout="\ud800" * 2_000_000)
    tracemalloc.start()
    prepared = logs.prepare(op, ex, "install-conda")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 500_000
    assert len(prepared.raw) < 4096
    assert json.loads(prepared.raw)["streams"]["stdout"]["unscanned_characters"] > 1_000_000


@pytest.mark.parametrize(
    "kind",
    [
        "foreign-project",
        "foreign-environment",
        "foreign-attempt",
        "unadmitted",
        "extra",
        "wrong-state",
        "wrong-type",
    ],
)
def test_original_scope_and_copied_metadata_refuse(kind: str):
    op, ex = fixture_step()
    if kind == "foreign-project":
        ex = ex.model_copy(update={"context": CONTEXT.model_copy(update={"project_id": "foreign"})})
    elif kind == "foreign-environment":
        ex = ex.model_copy(
            update={"context": CONTEXT.model_copy(update={"environment_id": "foreign"})}
        )
    elif kind == "foreign-attempt":
        ex = ex.model_copy(update={"attempt_id": "att-other"})
    elif kind == "unadmitted":
        op = op.model_copy(update={"executions": ()})
    elif kind == "extra":
        ex = ex.model_copy(update={"undeclared": "synthetic"})
    elif kind == "wrong-state":
        ex = ex.model_copy(update={"state": ExecutionState.RUNNING})
    else:
        assert ex.result is not None
        assert ex.result.stdout is not None
        ex = ex.model_copy(
            update={
                "result": ex.result.model_copy(
                    update={"stdout": ex.result.stdout.model_copy(update={"text": []})}
                )
            }
        )
    with pytest.raises(logs.InstallerLogError):
        logs.prepare(op, ex, "install-conda")


@pytest.mark.asyncio
async def test_blob_then_project_reference_exact_replay_and_changed_payload_refusal(tmp_path: Path):
    store = Store(tmp_path / "store.sqlite3")
    blobs = Blobs(tmp_path / "artifacts")
    owner = logs.InstallerArtifacts(store, blobs, 64 * 1024)
    try:
        op, ex = fixture_step()
        prepared = logs.prepare(op, ex, "install-conda")
        step, interrupted = await owner.publish(prepared)
        assert interrupted is None
        assert store.artifact(prepared.artifact.artifact_id) == prepared.artifact
        assert blobs.path(prepared.artifact.sha256).read_bytes() == prepared.raw
        again, _ = await owner.publish(prepared)
        assert again == step
        changed = logs.prepare(*fixture_step(stdout="Transaction starting\n"), "install-conda")
        with pytest.raises(logs.InstallerLogError):
            await owner.publish(changed)
        assert store.artifact(prepared.artifact.artifact_id) == prepared.artifact
        blobs.path(prepared.artifact.sha256).unlink()
        with pytest.raises(logs.InstallerLogError):
            await owner.publish(prepared)
    finally:
        await owner.close()
        store.close()


@pytest.mark.asyncio
async def test_repeated_cancel_and_close_keep_actual_thread_and_reference_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = Store(tmp_path / "store.sqlite3")
    blobs = Blobs(tmp_path / "artifacts")
    owner = logs.InstallerArtifacts(store, blobs, 65536)
    entered = threading.Event()
    release = threading.Event()
    original = owner._blob

    def delayed(prepared: logs.PreparedLog, existing: bool):
        entered.set()
        assert release.wait(5)
        original(prepared, existing)

    monkeypatch.setattr(owner, "_blob", delayed)
    op, ex = fixture_step()
    prepared = logs.prepare(op, ex, "install-conda")
    task = asyncio.create_task(owner.publish(prepared))
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel("original log cancellation")
        await asyncio.sleep(0)
        task.cancel("later cancellation")
        close = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
        assert not task.done()
        assert not close.done()
        assert len(owner._work) == 1
        release.set()
        step, interrupted = await task
        assert interrupted is not None
        assert interrupted.args == ("original log cancellation",)
        await close
        assert step.log_status == "stored"
        assert store.artifact(prepared.artifact.artifact_id) == prepared.artifact
        assert not owner._work
    finally:
        release.set()
        await owner.close()
        store.close()


@pytest.mark.parametrize("replacement", [False, True])
def test_blob_constructor_fdopen_failure_closes_only_original_owned_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: bool
):
    blobs = Blobs(tmp_path / "artifacts")
    opened = []
    mkstemp = tempfile.mkstemp

    def create(*args, **kwargs):
        fd, name = mkstemp(*args, **kwargs)
        opened.append((fd, Path(name)))
        return fd, name

    def fail(fd: int, mode: str):
        if replacement:
            _, path = opened[0]
            path.rename(path.with_suffix(".owned"))
            path.write_text("foreign replacement")
        raise OSError("synthetic fdopen failure")

    monkeypatch.setattr(tempfile, "mkstemp", create)
    monkeypatch.setattr(os, "fdopen", fail)
    with pytest.raises(OSError, match="synthetic fdopen"):
        blobs.writer()
    fd, path = opened[0]
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(fd)
    assert (path.read_text() == "foreign replacement") if replacement else not path.exists()


class ScriptedManager:
    def __init__(self, root: Path, state: ExecutionState = ExecutionState.SUCCEEDED):
        self.store = Store(root / "worker.sqlite3")
        self.blobs = Blobs(root / "artifacts")
        self.max_artifact_size = 65536
        self.revisions = SimpleNamespace()
        self.requests: list[ShellRequest] = []
        self.results = {}
        self.version_state = state

    async def submit(self, request: ShellRequest, *, origin: str):
        assert origin == PROVISION
        assert self.store.provision_step(PROVISION, request.operation_id) == execution_digest(
            request
        )
        self.requests.append(request)
        n = len(self.requests)
        ex = Execution(
            execution_id=f"exe-{n:024x}",
            attempt_id=f"att-{n:024x}",
            operation_id=request.operation_id,
            context=request.context,
            kind="shell",
            origin=origin,
            state=ExecutionState.QUEUED,
            submitted_at=NOW,
        )
        label = request.operation_id.rsplit(".", 1)[-1]
        text = (
            "2.9.0\n"
            if label == "version-conda"
            else "pip 25.2 from /synthetic (python 3.13)\n"
            if label == "version-pip"
            else '{"actions":{"LINK":[]},"success":true}'
        )
        state = self.version_state if label.startswith("version-") else ExecutionState.SUCCEEDED
        self.results[ex.execution_id] = ex.model_copy(
            update={
                "state": state,
                "started_at": NOW,
                "ended_at": NOW,
                "result": Result(
                    exit_code=0,
                    stdout=StreamOutput(name="stdout", text=text, total_bytes=len(text)),
                ),
            }
        )
        return ex, True

    async def wait(self, execution_id: str):
        return self.results[execution_id]

    async def cancel(self, execution_id: str):
        raise AssertionError("no implicit cancel in this fixture")


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [False, True])
async def test_production_resolve_records_version_in_original_turn_without_step_retry(
    tmp_path: Path, lost: bool
):
    manager = ScriptedManager(tmp_path, ExecutionState.LOST if lost else ExecutionState.SUCCEEDED)
    provisioner = Provisioner(
        cast(ExecutionManager, manager),
        catalog=cast(Catalog, SimpleNamespace()),
        policies=None,
        permissions=None,
        cache=None,
    )
    base, _ = fixture_step()
    record = base.model_copy(update={"executions": (), "installer_steps": ()})
    manager.store.insert_provision(record, "d" * 64)
    op = _Op(record, asyncio.get_running_loop().create_future())
    try:
        if lost:
            with pytest.raises(_Stop):
                await provisioner.stages._resolve(op, ("example",), ())
            assert len(manager.requests) == 1
        else:
            assert await provisioner.stages._resolve(op, ("example",), ()) == []
            assert [r.operation_id.rsplit(".", 1)[-1] for r in manager.requests] == [
                "version-conda",
                "resolve-conda",
            ]
            assert manager.requests[0].argv == ("micromamba", "--version")
            assert manager.requests[0].timeout_seconds == 10
            assert manager.requests[0].output_limit_bytes == 4096
            assert op.record.installer_steps is not None
            assert op.record.installer_steps[0].observed_version == "2.9.0"
            assert all(s.log_status == "stored" for s in op.record.installer_steps)
            with pytest.raises(WorkerError, match="retained intent"):
                await provisioner.stages.execute(
                    op,
                    ("micromamba", "--version"),
                    label="version-conda",
                    stage="resolve",
                    timeout=10,
                    limit=4096,
                )
            assert len(manager.requests) == 2
    finally:
        await provisioner.close()
        manager.store.close()


def test_legacy_record_stays_unknown_and_new_references_are_scope_checked():
    operation, execution = fixture_step()
    raw = operation.model_dump(mode="json")
    raw.pop("installer_steps")
    assert ProvisionOperation.model_validate(raw).installer_steps is None
    stored = logs.prepare(operation, execution, "install-conda").step
    value = operation.model_copy(update={"installer_steps": (stored,)}).model_dump(mode="json")
    assert ProvisionOperation.model_validate(value).installer_steps == (stored,)
    value["installer_steps"][0]["log"]["project_id"] = "foreign"
    with pytest.raises(ValidationError, match="project/generated"):
        ProvisionOperation.model_validate(value)
    with pytest.raises(ValidationError):
        InstallerStep(
            label="version-conda",
            installer="micromamba",
            operation_id="version",
            execution_id=EXECUTION,
            attempt_id=ATTEMPT,
            execution_state=ExecutionState.SUCCEEDED,
            observed_version="2.9.0\n",
        )


@pytest.mark.parametrize("corrupt", ["large", "duplicate", "blob"])
def test_retained_artifact_refuses_bounded_before_decode(tmp_path: Path, corrupt: str):
    store = Store(tmp_path / "store.sqlite3")
    prepared = logs.prepare(*fixture_step(), "install-conda")
    try:
        if corrupt == "large":
            raw = "X" * 2_000_000
        elif corrupt == "duplicate":
            raw = prepared.artifact.model_dump_json()[:-1] + ',"size":1}'
        else:
            raw = prepared.artifact.model_dump_json().encode()
        store._db.execute(
            "INSERT INTO artifacts(artifact_id,record) VALUES(?,?)",
            (prepared.artifact.artifact_id, raw),
        )
        tracemalloc.start()
        with pytest.raises(ValueError, match="retained installer artifact"):
            store.installer_artifact(prepared.artifact)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 100_000
    finally:
        store.close()


@pytest.mark.asyncio
async def test_artifact_write_failure_is_explicit_without_resubmitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = ScriptedManager(tmp_path)
    provisioner = Provisioner(
        cast(ExecutionManager, manager),
        catalog=cast(Catalog, SimpleNamespace()),
        policies=None,
        permissions=None,
        cache=None,
    )
    base, _ = fixture_step()
    record = base.model_copy(update={"executions": (), "installer_steps": ()})
    manager.store.insert_provision(record, "d" * 64)
    op = _Op(record, asyncio.get_running_loop().create_future())

    def fail(prepared: logs.PreparedLog, existing: bool):
        raise OSError("SYNTHETIC_PRIVATE_EXCEPTION_MUST_NOT_BE_RETAINED")

    monkeypatch.setattr(provisioner.installer_logs, "_blob", fail)
    try:
        await provisioner.stages._installer_version(op, True)
        assert len(manager.requests) == 1
        assert op.record.installer_steps is not None
        [step] = op.record.installer_steps
        assert step.log_status == "unavailable"
        assert step.unavailable_reason == "artifact_unavailable"
        assert step.execution_state is ExecutionState.SUCCEEDED
        assert step.observed_version == "2.9.0"
        assert "SYNTHETIC_PRIVATE_EXCEPTION" not in op.record.model_dump_json()
        with pytest.raises(WorkerError, match="retained intent"):
            await provisioner.stages._installer_version(op, True)
        assert len(manager.requests) == 1
    finally:
        await provisioner.close()
        manager.store.close()


@pytest.mark.asyncio
async def test_four_physical_owners_and_size_bound_refuse_before_extra_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = Store(tmp_path / "store.sqlite3")
    owner = logs.InstallerArtifacts(store, Blobs(tmp_path / "artifacts"), 65536)
    release = threading.Event()
    lock = threading.Lock()
    count = 0

    def block(prepared: logs.PreparedLog, existing: bool):
        nonlocal count
        with lock:
            count += 1
        assert release.wait(5)

    monkeypatch.setattr(owner, "_blob", block)
    prepared = logs.prepare(*fixture_step(), "install-conda")
    tasks = [asyncio.create_task(owner.publish(prepared)) for _ in range(4)]
    try:
        while count < 4:
            await asyncio.sleep(0)
        with pytest.raises(logs.InstallerLogError):
            await owner.publish(prepared)
        assert count == 4
        release.set()
        await asyncio.gather(*tasks)
        small = logs.InstallerArtifacts(store, owner.blobs, 1)
        with pytest.raises(logs.InstallerLogError):
            await small.publish(prepared)
        await small.close()
        assert count == 4
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await owner.close()
        store.close()


@pytest.mark.asyncio
async def test_prepared_copy_cannot_replace_safe_bytes_or_escape_size_limit(tmp_path: Path):
    store = Store(tmp_path / "store.sqlite3")
    owner = logs.InstallerArtifacts(store, Blobs(tmp_path / "artifacts"), 128)
    prepared = logs.prepare(*fixture_step(), "install-conda")

    class FalseLength(bytes):
        def __len__(self):
            return 0

    try:
        for raw in (FalseLength(b"SYNTHETIC_PRIVATE" * 100), b"SYNTHETIC_PRIVATE"):
            with pytest.raises(logs.InstallerLogError):
                await owner.publish(replace(prepared, raw=raw))
        assert store.artifact(prepared.artifact.artifact_id) is None
        assert not list(owner.blobs.root.rglob("art-inst-*"))
        assert not list((owner.blobs.root / "tmp").iterdir())
    finally:
        await owner.close()
        store.close()


def test_foreign_origin_cannot_be_promoted_to_original_step():
    operation, execution = fixture_step()
    execution = execution.model_copy(update={"origin": "prv-foreign"})
    with pytest.raises(logs.InstallerLogError, match="scope/state"):
        logs.prepare(operation, execution, "install-conda")


@pytest.mark.asyncio
async def test_actual_blob_fsync_failure_closes_fd_and_publishes_no_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = Store(tmp_path / "store.sqlite3")
    owner = logs.InstallerArtifacts(store, Blobs(tmp_path / "artifacts"), 65536)
    prepared = logs.prepare(*fixture_step(), "install-conda")
    opened: list[int] = []

    def fail(fd: int):
        opened.append(fd)
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(os, "fsync", fail)
    try:
        with pytest.raises(logs.InstallerLogError, match="retention"):
            await owner.publish(prepared)
        assert len(opened) == 1
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(opened[0])
        assert not list((owner.blobs.root / "tmp").iterdir())
        assert store.artifact(prepared.artifact.artifact_id) is None
    finally:
        await owner.close()
        store.close()


@pytest.mark.parametrize("name", ["operation_id", "execution_id", "attempt_id", "log_status"])
def test_copied_admitted_pin_subclasses_refuse_before_equality(name: str):
    operation, execution = fixture_step()
    visits: list[str] = []

    class WrongPin(str):
        def __eq__(self, other):
            visits.append("eq")
            return True

        def __ne__(self, other):
            visits.append("ne")
            return False

        __hash__ = str.__hash__

    assert operation.installer_steps is not None
    step = operation.installer_steps[0].model_copy(update={name: WrongPin("foreign-original-step")})
    operation = operation.model_copy(update={"installer_steps": (step,)})
    with pytest.raises(logs.InstallerLogError):
        logs.prepare(operation, execution, "install-conda")
    assert visits == []


def test_scan_bound_cannot_promote_a_partial_line_to_fixed_whole_message():
    message = "Transaction finished"
    text = (
        "X" * (logs.MAX_SCAN_CHARS - len(message) - 1) + "\n" + message + " actual continuation\n"
    )
    prepared = logs.prepare(*fixture_step(stdout=text), "install-conda")
    stream = json.loads(prepared.raw)["streams"]["stdout"]
    assert stream["fixed_messages"] == []
    assert stream["omitted_scanned_lines"] == 2
    assert stream["unscanned_characters"] > 0
