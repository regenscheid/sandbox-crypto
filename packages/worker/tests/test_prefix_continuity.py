# SPDX-License-Identifier: MIT
"""Retained-prefix continuity uses durable identity/revision, never package equality as ACK."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest

from sandbox_science.contracts import EnvironmentInfo, EnvironmentState
from sandbox_science.worker import provisioning, workspace_prefix
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import Backend as BackendProtocol
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.connections import Connection, EnvironmentConnections
from sandbox_science.worker.environments import EnvironmentRecords
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.journal import ExecutionJournal
from sandbox_science.worker.lifecycle import EnvironmentLifecycle
from sandbox_science.worker.package_changes import PackageChanges
from sandbox_science.worker.revisions import Revisions
from sandbox_science.worker.store import RevisionRecord, Store
from sandbox_science.worker.timeouts import MANAGER_TIMEOUTS
from sandbox_science.worker.usability import Usability

IDENTITY = "prefix-" + "1" * 32
INCARNATION = "inc-" + "1" * 24
NEXT = "inc-" + "2" * 24
A, B = "a" * 32, "b" * 32


def description(fingerprint: str = A, **changes: object) -> dict[str, object]:
    return {
        "protocol": 1,
        "path": "/sandbox/.sandbox-science-prefix",
        "identity": IDENTITY,
        "fingerprint": fingerprint,
        "journal_pending": False,
        **changes,
    }


class Worker:
    def __init__(self, root: Path, *, required: bool = True) -> None:
        self.store = Store(root / "worker.sqlite3")
        self.revisions = Revisions(self.store, Blobs(root / "lockfiles"))
        self.packages = PackageChanges(
            self.store,
            self.revisions,
            # Hello uses only lifecycle; unexpected journal work must fail in this fixture.
            cast(
                EnvironmentConnections,
                SimpleNamespace(
                    lifecycle=EnvironmentLifecycle(requires_workspace_prefix=lambda _: required)
                ),
            ),
            cast(ExecutionJournal, SimpleNamespace()),
            spawn=asyncio.create_task,
            timeouts=MANAGER_TIMEOUTS,
        )

    def hello(self, raw: object, fingerprint: str = A, incarnation: str = INCARNATION) -> None:
        self.packages.hello(
            "env",
            cast(
                ExecutorChannel,
                SimpleNamespace(
                    incarnation=incarnation,
                    hello={"workspace_prefix": raw, "environment": fingerprint, "package_epoch": 0},
                ),
            ),
        )

    def revision(self) -> RevisionRecord:
        record = self.store.revision("env")
        assert record is not None
        return record


async def unused_connection(environment_id: str) -> Connection:
    raise AssertionError("this fixture must not reconnect through its hook")


def unused_recycle(environment_id: str) -> asyncio.Task[None]:
    raise AssertionError("this fixture must not recycle its sandbox")


def test_clean_replacement_uses_latest_managed_revision_and_survives_store_restart(tmp_path: Path):
    worker = Worker(tmp_path)
    worker.hello(description())
    assert worker.revisions.observe("env", B, "attempt", managed=True) == 1
    worker.store.close()
    worker = Worker(tmp_path)
    try:
        worker.hello(description(B), B, NEXT)
        assert worker.revision().revision == 1
        assert worker.store.package_status("env") is None
        assert worker.store.workspace_prefix("env") == (IDENTITY, NEXT)
    finally:
        worker.store.close()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        description(protocol=True),
        description(protocol=2),
        description(identity="prefix-" + "x" * 32),
        description(path="/opt/sandbox-science"),
        description(extra="unrecognized"),
        description(journal_pending=1),
        description(fingerprint="a" * 33),
        description(identity="prefix-" + "2" * 32),
        description(B),
        description(journal_pending=True),
    ],
)
def test_replacement_loss_or_unresolved_journal_holds_original_revision(
    tmp_path: Path, raw: object
):
    worker = Worker(tmp_path)
    try:
        worker.hello(description())
        worker.hello(raw, B if raw == description(B) else A, NEXT)
        assert worker.revision().fingerprint == A
        assert worker.store.package_status("env") == ("needs_recovery", "workspace-prefix", 0)
        assert worker.store.package_gate("env").mode == "needs_recovery"
        assert worker.store.workspace_prefix("env") == (IDENTITY, INCARNATION)
    finally:
        worker.store.close()


def test_surviving_daemon_hello_never_races_ahead_of_or_rewinds_revision_replay(tmp_path: Path):
    worker = Worker(tmp_path)
    try:
        worker.hello(description())
        # The daemon may be ahead while its managed events wait in replay.
        worker.hello(description(B, journal_pending=True), B)
        assert worker.revision().revision == 0
        assert worker.store.package_status("env") is None
        worker.revisions.observe("env", B, "attempt", managed=True)
        # It may report an older settled baseline; current durable revision still wins.
        worker.hello(description(A, journal_pending=True), A)
        assert worker.revision().revision == 1
        assert worker.store.package_status("env") is None
    finally:
        worker.store.close()


def test_existing_interrupted_operation_hold_and_known_revision_are_preserved(tmp_path: Path):
    worker = Worker(tmp_path)
    try:
        worker.hello(description())
        worker.store.set_package_status("env", "needs_recovery", "interrupted-install", 0)
        worker.hello(description(B, journal_pending=True), B, NEXT)
        assert worker.store.package_status("env") == ("needs_recovery", "interrupted-install", 0)
        assert worker.revision().fingerprint == A
    finally:
        worker.store.close()


def test_legacy_and_explicit_escape_keep_previous_baseline_behavior(tmp_path: Path):
    worker = Worker(tmp_path, required=False)
    try:
        worker.hello(None)
        worker.hello(None, B, NEXT)
        assert worker.revision().revision == 1
        assert worker.store.workspace_prefix("env") is None
        assert worker.store.package_status("env") is None
    finally:
        worker.store.close()


def test_unknown_identity_cannot_adopt_an_existing_revision(tmp_path: Path):
    worker = Worker(tmp_path)
    try:
        worker.revisions.observe("env", A)
        worker.hello(description())
        assert worker.store.package_status("env") == ("needs_recovery", "workspace-prefix", 0)
    finally:
        worker.store.close()


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    prefix = tmp_path / ".sandbox-science-prefix"
    (prefix / "conda-meta").mkdir(parents=True)
    (prefix / "bin").mkdir()
    (prefix / "bin/python").touch()
    alias = tmp_path / "compatibility-prefix"
    alias.symlink_to(prefix, target_is_directory=True)
    monkeypatch.setattr(workspace_prefix, "PREFIX", prefix)
    monkeypatch.setattr(workspace_prefix, "ALIAS", alias)
    return prefix, alias


def test_marker_is_atomic_unique_and_retained_inside_actual_prefix(layout: tuple[Path, Path]):
    prefix, alias = layout
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(lambda _: workspace_prefix.describe(str(alias)), range(16)))
    assert len({report["identity"] for report in reports}) == 1
    assert (prefix / workspace_prefix.MARKER).read_text() == reports[0]["identity"]
    assert not list(prefix.glob(".prefix-id-*"))


@pytest.mark.parametrize(
    "failure", ["python", "alias", "prefix-symlink", "bad-marker", "marker-symlink"]
)
def test_invalid_layout_or_identity_never_marks_another_prefix(
    layout: tuple[Path, Path],
    tmp_path: Path,
    failure: str,
):
    prefix, alias = layout
    other = tmp_path / "developer-python"
    other.mkdir()
    python_prefix = str(alias)
    if failure == "python":
        python_prefix = str(other)
    elif failure == "alias":
        alias.unlink()
        alias.symlink_to(other)
    elif failure == "prefix-symlink":
        actual = tmp_path / "actual"
        prefix.rename(actual)
        prefix.symlink_to(actual)
    elif failure == "bad-marker":
        (prefix / workspace_prefix.MARKER).write_text("bad identity")
    else:
        (prefix / workspace_prefix.MARKER).symlink_to(other / "marker")
    assert "error" in workspace_prefix.report(python_prefix)
    assert list(other.iterdir()) == []
    if failure not in ("bad-marker", "marker-symlink"):
        assert not (prefix / workspace_prefix.MARKER).exists()


def test_pending_preserves_every_record_and_detects_interrupted_holder(tmp_path: Path):
    state = provisioning.State(tmp_path / "provisioning")
    assert not state.pending()
    state.record({"before": A, "after": B})
    state.record({"before": B, "after": A})
    before = {path.name: path.read_bytes() for path in state.journal.iterdir()}
    assert state.pending()
    assert {path.name: path.read_bytes() for path in state.journal.iterdir()} == before
    state.clear_journal()
    state.holder.write_text("{}")
    assert state.pending()


def test_original_requirement_is_immutable_and_legacy_migration_is_false(tmp_path: Path):
    path = tmp_path / "environments.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE sandbox_launch_settings (sandbox TEXT PRIMARY KEY,"
            " driver_config TEXT NOT NULL)"
        )
        db.execute("INSERT INTO sandbox_launch_settings VALUES ('legacy','{}')")
    records = EnvironmentRecords(path)
    info = EnvironmentInfo(
        environment_id="env",
        project_id="project",
        profile="minimal",
        image="synthetic",
        sandbox="sandbox",
        policy="sha256:" + "0" * 64,
        state=EnvironmentState.CREATING,
        created_at=datetime(2026, 10, 20, tzinfo=UTC),
        updated_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    try:
        assert not records.requires_workspace_prefix("legacy")
        records.save(info, driver_config="{}", require_workspace_prefix=True)
        records.save(info)  # ordinary persisted state updates keep the pin
        assert records.requires_workspace_prefix("sandbox")
        with pytest.raises(WorkerError, match="launch settings changed"):
            records.save(info, driver_config="{}", require_workspace_prefix=False)
        assert records.requires_workspace_prefix("sandbox")
    finally:
        records.close()


async def test_real_managed_revision_consumes_its_journal_before_clean_replacement(tmp_path: Path):
    from sandbox_science.worker.executor import Executor

    worker = Worker(tmp_path)
    worker.hello(description())

    async def send(message):
        if message.get("event") == "revision":
            worker.revisions.observe(
                "env", message["fingerprint"], "attempt", managed=message["managed"]
            )

    executor = Executor(tmp_path / "project", send, require_workspace_prefix=True)
    executor.admission.reported = A
    executor.admission.own("synthetic-owner", "exe-" + "1" * 24)
    executor.packages.record(
        {
            "before": A,
            "after": B,
            "owner": "synthetic-owner",
            "via": "pip",
            "command": ["pip", "install", "synthetic"],
        }
    )
    assert executor.packages.pending()
    records, complete = executor.packages.read_records()
    await executor.admission.report(
        "exe-" + "1" * 24, provisioning.Observation(records, B, complete=complete), None
    )
    assert not executor.packages.pending()
    assert worker.revision().fingerprint == B
    entry = worker.store.revision_entry("env", 1)
    assert entry is not None
    assert entry.managed is True
    worker.hello(description(B), B, NEXT)
    assert worker.store.package_status("env") is None
    assert worker.revision().revision == 1
    worker.store.close()


async def test_required_hello_preserves_unconsumed_round_trip_and_local_never_writes_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from sandbox_science.worker.executor import Executor

    sent = []

    async def send(message):
        sent.append(message)

    marker_calls = []

    def report(prefix):
        marker_calls.append(prefix)
        return description()

    monkeypatch.setattr(workspace_prefix, "report", report)
    required = Executor(tmp_path / "required", send, require_workspace_prefix=True)
    required.packages.record({"before": A, "after": B})
    required.packages.record({"before": B, "after": A})
    before = {p.name: p.read_bytes() for p in required.packages.journal.iterdir()}
    await required.op_hello({"id": 1})
    assert sent[-1]["workspace_prefix"]["journal_pending"] is True
    assert {p.name: p.read_bytes() for p in required.packages.journal.iterdir()} == before
    assert len(marker_calls) == 1
    local = Executor(tmp_path / "local", send)
    await local.op_hello({"id": 2})
    assert len(marker_calls) == 1
    assert "workspace_prefix" not in sent[-1]


async def test_connection_checks_loss_before_ready_and_rechecks_exact_restoration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from sandbox_science.contracts import SYSTEM_CLOCK
    from sandbox_science.worker import connections as module
    from sandbox_science.worker.connections import ConnectionHooks, EnvironmentConnections
    from sandbox_science.worker.isolation import failed
    from sandbox_science.worker.usability import Usability

    worker = Worker(tmp_path)
    worker.hello(description())
    order = []

    class Channel:
        closed = False
        daemon_token = "test-secret-daemon-token"
        incarnation = NEXT

        def __init__(self) -> None:
            self.hello: Message = {
                "environment": B,
                "workspace_prefix": description(B),
                "package_epoch": 0,
                "location": {"hostname": "synthetic"},
                "languages": ["python"],
                "features": [],
            }

        async def request(self, operation):
            assert operation == "hello"
            order.append("recheck")
            return {**self.hello, "daemon_incarnation": self.incarnation}

        async def shutdown(self):
            self.closed = True

    channel = Channel()

    class Backend:
        name = "openshell"
        isolation_tier = 0

        async def prepare(self, environment_id):
            pass

        async def open(self, *args):
            return channel

        def sandbox_id(self, environment_id):
            return "synthetic-sandbox"

    async def noop(*args):
        pass

    async def sync(environment_id, opened):
        order.append(worker.store.package_gate(environment_id).mode)
        return True

    async def probe(*args):
        return failed("synthetic probe")

    monkeypatch.setattr(module, "probe", probe)

    def hello(environment_id, opened):
        order.append("hello")
        worker.packages.hello(environment_id, opened)

    hooks = ConnectionHooks(
        connect=unused_connection,
        on_event=noop,
        on_closed=noop,
        incarnation_lost=lambda *_: None,
        recycle=unused_recycle,
        sync_package_gate=sync,
        hello=hello,
        connected=noop,
    )
    connections = EnvironmentConnections(
        cast(BackendProtocol, Backend()),
        worker.store,
        Usability(worker.store),
        hooks,
        EnvironmentLifecycle(requires_workspace_prefix=lambda _: True),
        clock=SYSTEM_CLOCK,
        pool_id="synthetic",
        state_dir=tmp_path,
    )
    await connections.connect("env")
    assert order == ["hello", "needs_recovery"]
    held_generation = worker.store.package_gate("env").generation
    channel.hello = {**channel.hello, "environment": A, "workspace_prefix": description()}
    await connections.connect("env")
    assert order[-3:] == ["recheck", "hello", "ready"]
    assert worker.store.package_gate("env").generation > held_generation
    assert worker.store.package_status("env") is None
    assert worker.revision().fingerprint == A
    worker.store.close()


def test_restoration_never_clears_an_unrelated_operation_hold(tmp_path: Path):
    worker = Worker(tmp_path)
    try:
        worker.hello(description())
        worker.store.set_package_status("env", "needs_recovery", "interrupted-install", 0)
        worker.hello(description(B), B, NEXT)
        worker.hello(description(), A, NEXT)
        assert worker.store.package_status("env") == ("needs_recovery", "interrupted-install", 0)
    finally:
        worker.store.close()


async def test_actual_executor_hello_restoration_aligns_only_at_verified_ready_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import os

    from sandbox_science.worker.executor import Executor
    from sandbox_science.worker.sandbox_files import ToolError

    meta = tmp_path / "meta"
    meta.mkdir()
    package = meta / "synthetic.json"
    package.write_text('{"version":"original"}')
    original = package.stat()
    places = provisioning.Places(str(meta), ())
    before = provisioning.fingerprint(places)
    worker = Worker(tmp_path)
    worker.hello(description(before), before)
    package.write_text('{"version":"incompatible-replacement"}')
    after = provisioning.fingerprint(places)
    sent = []

    async def send(message):
        sent.append(message)

    executor = Executor(tmp_path / "project", send, require_workspace_prefix=True)
    executor.places, executor.admission.reported = places, after
    monkeypatch.setattr(
        workspace_prefix,
        "report",
        lambda _: {"protocol": 1, "path": "/sandbox/.sandbox-science-prefix", "identity": IDENTITY},
    )
    await executor.op_hello({"id": 1})
    channel = cast(ExecutorChannel, SimpleNamespace(incarnation=NEXT, hello=sent[-1]))
    worker.packages.hello("env", channel)
    assert worker.store.package_status("env") is not None
    package.write_text('{"version":"original"}')
    os.utime(package, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert provisioning.fingerprint(places) == before
    await executor.op_hello({"id": 2})
    assert sent[-1]["environment"] == after  # the actual stale-baseline case
    assert sent[-1]["workspace_prefix"]["fingerprint"] == before
    channel.hello = sent[-1]
    worker.packages.hello("env", channel)
    assert worker.store.package_status("env") is None
    assert worker.store.workspace_prefix_restore_pending("env") == 0
    # Crash at the dangerous point: durable hold cleared, guest ACK not yet sent.
    worker.store.close()
    worker = Worker(tmp_path)
    await executor.op_hello({"id": 20})
    assert sent[-1]["environment"] == after
    channel.hello = sent[-1]
    worker.packages.hello("env", channel)
    assert channel.hello["workspace_prefix_restore"]["fingerprint"] == before
    gate = worker.store.package_gate("env")
    request = {
        "id": 3,
        "incarnation": executor.admission.incarnation,
        **gate.fields(),
        "workspace_prefix_restore": channel.hello["workspace_prefix_restore"],
    }
    # A changed prefix between hello and ACK never opens the guest gate.
    package.write_text("changed-after-evidence")
    with pytest.raises(ToolError, match="evidence changed"):
        await executor.op_package_gate(request)
    assert executor.admission.mode == "hold"
    package.write_text('{"version":"original"}')
    os.utime(package, ns=(original.st_atime_ns, original.st_mtime_ns))
    attached = [True]
    original_report = workspace_prefix.report

    def detach_during_read(prefix):
        attached[0] = False
        return original_report(prefix)

    monkeypatch.setattr(workspace_prefix, "report", detach_during_read)
    request["_admission_guard"] = lambda: attached[0]
    with pytest.raises(ToolError, match="controller detached"):
        await executor.op_package_gate(request)
    assert executor.admission.mode == "hold"
    assert executor.admission.reported == after
    attached[0] = True
    monkeypatch.setattr(workspace_prefix, "report", original_report)
    await executor.op_package_gate(request)
    assert executor.admission.mode == "ready"
    assert executor.admission.reported == before
    assert worker.revision().fingerprint == before
    from sandbox_science.worker.lifecycle import UNMANAGED
    from sandbox_science.worker.package_admission import PackageAdmission

    class RestoredChannel:
        incarnation = NEXT
        closed = False
        hello = channel.hello

        async def request(self, op, **fields):
            assert op == "package_gate"
            await executor.op_package_gate({"id": 4, **fields})
            return sent[-1]

    async def noop(*args):
        pass

    admission = PackageAdmission(
        worker.store,
        cast(BackendProtocol, SimpleNamespace(binding_key=lambda _: "synthetic")),
        Usability(worker.store),
        connect=unused_connection,
        spawn=asyncio.create_task,
        sync=noop,
        lifecycle=UNMANAGED,
    )
    assert await admission.sync("env", cast(ExecutorChannel, RestoredChannel()))
    # The first new kernel's epoch has a known revision without another package event.
    assert worker.store.kernel_starts.epoch("env", NEXT, executor.admission.epoch, before) == 0
    assert worker.store.workspace_prefix_restore_pending("env") is None
    assert worker.store.workspace_prefix("env") == (IDENTITY, NEXT)
    worker.store.close()


async def test_restore_directive_survives_cancelled_ack_and_is_consumed_once(tmp_path: Path):
    from sandbox_science.worker.lifecycle import UNMANAGED
    from sandbox_science.worker.package_admission import PackageAdmission

    worker = Worker(tmp_path)
    worker.hello(description())
    worker.hello(description(B), B, NEXT)
    worker.hello(description(), A, NEXT)
    assert worker.store.workspace_prefix_restore_pending("env") == 0
    instruction = {"identity": IDENTITY, "fingerprint": A}

    class Channel:
        hello: ClassVar[dict] = {
            "package_gate": "gate-" + "1" * 24,
            "workspace_prefix_restore": instruction,
        }
        closed = False
        cancel = True
        incarnation = INCARNATION
        requests: ClassVar[list] = []

        async def request(self, op, **fields):
            self.requests.append(fields)
            if self.cancel:
                raise asyncio.CancelledError
            return {
                "incarnation": self.hello["package_gate"],
                "package_epoch": 1,
                "environment": A,
                **{k: fields[k] for k in ("generation", "mode", "operation", "revision")},
            }

    channel = Channel()

    async def noop(*args):
        pass

    admission = PackageAdmission(
        worker.store,
        cast(BackendProtocol, SimpleNamespace(binding_key=lambda _: "synthetic")),
        Usability(worker.store),
        connect=unused_connection,
        spawn=asyncio.create_task,
        sync=noop,
        lifecycle=UNMANAGED,
    )
    with pytest.raises(WorkerError, match="awaits gate acknowledgement"):
        admission.admission("env")
    missing = channel.hello.pop("workspace_prefix_restore")
    with pytest.raises(WorkerError, match="lacks evidence"):
        await admission.sync("env", cast(ExecutorChannel, channel))
    assert channel.requests == []
    channel.hello["workspace_prefix_restore"] = missing
    with pytest.raises(asyncio.CancelledError):
        await admission.sync("env", cast(ExecutorChannel, channel))
    assert channel.hello["workspace_prefix_restore"] is instruction
    channel.cancel = False
    assert await admission.sync("env", cast(ExecutorChannel, channel))
    assert "workspace_prefix_restore" not in channel.hello
    assert await admission.sync("env", cast(ExecutorChannel, channel))
    assert len(channel.requests) == 2
    assert admission.admission("env").mode == "ready"
    worker.store.close()


@pytest.mark.parametrize(
    ("code", "output", "positive_loss"),
    [
        (0, b"SS_PREFIX_MISSING\n", True),
        (1, b"transport unavailable", False),
        (0, b"unrecognized", False),
    ],
)
async def test_preflight_only_classifies_positive_layout_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    output: bytes,
    positive_loss: bool,
):
    from sandbox_science.worker import backends
    from sandbox_science.worker.backends import OpenShellBackend
    from sandbox_science.worker.errors import PrefixRecoveryError
    from sandbox_science.worker.settings import OpenShellBackendSettings

    commands = []

    async def command(*argv, **kwargs):
        commands.append(argv)
        return code, output

    monkeypatch.setattr(backends, "_command", command)
    backend = OpenShellBackend(
        OpenShellBackendSettings(isolation_tier=2, sandboxes={"env": "synthetic"}),
        tmp_path,
        requires_workspace_prefix=lambda _: True,
    )

    async def noop(*args):
        pass

    with pytest.raises(WorkerError) as error:
        await backend.open("env", noop, noop)
    assert isinstance(error.value, PrefixRecoveryError) == positive_loss
    assert len(commands) == 1
    assert "/bin/sh" in commands[0]


@pytest.mark.parametrize("unfinished", [True, False])
def test_prefix_restoration_preserves_later_canonical_provisioning_recovery(
    tmp_path: Path, unfinished: bool
):
    from sandbox_science.contracts import (
        ExecutionContext,
        ProvisionOperation,
        ProvisionOutcome,
        ProvisionRequest,
        ProvisionState,
    )

    worker = Worker(tmp_path)
    try:
        worker.hello(description())
        worker.hello(description(B), B, NEXT)
        operation = ProvisionOperation(
            provision_id="prv-" + "3" * 24,
            request=ProvisionRequest(
                operation_id="synthetic-request",
                context=ExecutionContext(
                    project_id="project", session_id="session", environment_id="env"
                ),
                purpose="Synthetic recovery record",
                conda=("synthetic",),
            ),
            state=ProvisionState.QUEUED if unfinished else ProvisionState.FINISHED,
            outcome=None if unfinished else ProvisionOutcome.NEEDS_RECOVERY,
            submitted_at=datetime(2026, 10, 20, tzinfo=UTC),
        )
        worker.store.insert_provision(operation, "0" * 64)
        worker.hello(description(), A, NEXT)
        assert worker.store.package_status("env") == ("needs_recovery", "workspace-prefix", 0)
        assert worker.store.provision(operation.provision_id) == operation
    finally:
        worker.store.close()
