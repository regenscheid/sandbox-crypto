# SPDX-License-Identifier: MIT
"""Terminal atomicity, bounded retention, honest stat observations and actual HTTP composition."""

from __future__ import annotations

import asyncio
import base64
import copy
import importlib.util
import json
import os
import sqlite3
import stat
import tracemalloc
from collections import OrderedDict
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sandbox_science.contracts import (
    Execution,
    ExecutionContext,
    ExecutionState,
    JobRequest,
    ResultEvent,
    ShellRequest,
)
from sandbox_science.contracts.output_observations import (
    MAX_PAGE_BYTES,
    OutputMarker,
    OutputObservation,
    OutputReport,
    OutputScope,
    OutputStat,
)
from sandbox_science.worker import daemon, executor, output_observations
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

from .harness import shut_down_daemon, view

pytestmark = pytest.mark.slow

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
SCOPE = OutputScope(**CONTEXT.model_dump(include={"project_id", "session_id", "environment_id"}))
TOKEN = "synthetic-observation-token-000000000000"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def execution(name: str = "a", context: ExecutionContext = CONTEXT) -> Execution:
    return Execution(
        execution_id=f"exe-{name}",
        attempt_id=f"att-{name}",
        operation_id=f"op-{name}",
        kind="shell",
        context=context,
        state=ExecutionState.QUEUED,
        submitted_at=datetime.now(UTC),
    )


def terminal(store: Store, name: str = "a", context: ExecutionContext = CONTEXT) -> Execution:
    value = execution(name, context)
    store.insert_execution(value, "a" * 64)
    return value.model_copy(
        update={"state": ExecutionState.SUCCEEDED, "ended_at": datetime.now(UTC), "last_seq": 1}
    )


def scan(root: Path) -> executor.SnapshotFiles:
    result = executor.SnapshotFiles()
    for area, prefix, path in (
        ("session", "", root / "session"),
        ("shared", "shared/", root / "shared"),
    ):
        path.mkdir(exist_ok=True)
        executor.scan_files(os.open(path, executor._DIRECTORY), prefix, result, area=area)
    return result


def test_sidecar_keeps_byte_and_root_identity_without_extra_file_reads(tmp_path: Path) -> None:
    before = scan(tmp_path)
    session = tmp_path / "session"
    for raw in (b"\xfe", b"\xff", "�".encode()):
        fd = os.open(os.fsencode(session) + b"/" + raw, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    (session / "shared").mkdir()
    (session / "shared/x").write_bytes(b"session")
    (tmp_path / "shared/x").write_bytes(b"shared")
    after = scan(tmp_path)
    report = OutputReport.model_validate(executor.observation_report(before, after))
    assert len(report.entries) == 5
    assert len({entry.identity() for entry in report.entries}) == 5
    assert {(entry.area, entry.raw()) for entry in report.entries} >= {
        ("session", b"shared/x"),
        ("shared", b"x"),
        ("session", b"\xff"),
    }


def test_stat_blind_spots_and_absence_stay_incomplete(tmp_path: Path) -> None:
    scan(tmp_path)
    same = tmp_path / "session/same"
    same.write_bytes(b"A")
    (tmp_path / "session/deleted").write_bytes(b"x")
    before = scan(tmp_path)
    original = same.stat()
    same.write_bytes(b"B")
    os.utime(same, ns=(original.st_atime_ns, original.st_mtime_ns))
    (tmp_path / "session/deleted").unlink()
    (tmp_path / "session/link").symlink_to("/etc/passwd")
    os.mkfifo(tmp_path / "session/fifo")
    report = OutputReport.model_validate(executor.observation_report(before, scan(tmp_path)))
    assert [(entry.kind, entry.raw()) for entry in report.entries] == [
        ("not_seen_after", b"deleted")
    ]
    assert report.coverage.mutation_coverage == "incomplete"
    assert report.coverage.links == "unavailable"


@pytest.mark.parametrize("mode", [stat.S_IFIFO, stat.S_IFLNK, stat.S_IFDIR])
def test_namespace_swap_does_not_label_special_metadata_as_regular(
    mode: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = SimpleNamespace(
        name="swapped",
        is_dir=lambda **_fields: False,
        is_file=lambda **_fields: True,
        stat=lambda **_fields: SimpleNamespace(st_mode=mode, st_mtime_ns=1, st_size=2, st_ino=3),
    )
    monkeypatch.setattr(os, "scandir", lambda _directory: nullcontext(iter([entry])))
    before, after = executor.SnapshotFiles(), executor.SnapshotFiles()
    assert executor._subdirectories(123, "", after) == []
    # Keep the ordinary written_files projection compatible, but the independently
    # typed report must not misrepresent the stat result as a regular file.
    assert after["swapped"] == (1, 2, 3)
    assert not after.raw
    report = OutputReport.model_validate(executor.observation_report(before, after))
    assert not report.entries
    assert report.coverage.after_error


def test_parallel_metadata_has_independent_entry_byte_and_wire_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = scan(tmp_path)
    for i in range(300):
        (tmp_path / "session" / f"file-{i:04}").touch()
    after = scan(tmp_path)
    assert len(after.raw) <= 256
    assert after.raw_bytes <= 65536
    assert after.limited
    report = OutputReport.model_validate(executor.observation_report(before, after))
    assert len(report.entries) == 64
    assert report.coverage.report_truncated
    monkeypatch.setattr(executor, "MAX_OBSERVATION_SCAN_BYTES", 150)
    tiny = scan(tmp_path)
    assert len(tiny.raw) <= 1
    assert tiny.limited


def test_source_error_and_path_omission_remain_explicit_without_new_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = object.__new__(executor.Executor)
    value.root = tmp_path

    def denied(_base: object) -> int:
        raise OSError("synthetic unavailable directory")

    monkeypatch.setattr(value, "open_base", denied)
    before, _ = value.snapshot("missing")
    assert isinstance(before, executor.SnapshotFiles)
    assert before.error
    assert before.finished_ns is not None
    after = executor.SnapshotFiles()
    after.observe("session", "x" * 4097, (1, 0, 1))
    after.observe("session", "visible", (1, 0, 1))
    report = OutputReport.model_validate(executor.observation_report(before, after))
    assert report.coverage.path_omitted
    assert report.coverage.before_error
    assert report.entries[0].kind == "regular_observed"


@pytest.mark.parametrize("raw", [{"x": 1 << 10000}, {"x": object()}, {"x": "x" * 20000}])
def test_wire_sidecar_failure_preserves_terminal_result(raw: object) -> None:
    message = {
        "type": "event",
        "event": "end",
        "state": "succeeded",
        "result": {"exit_code": 0},
        "output_observations": raw,
    }
    fitted = executor.fit_line(message)
    assert fitted is not None
    saved = json.loads(fitted)
    assert saved["state"] == "succeeded"
    assert saved["result"] == {"exit_code": 0}
    assert saved["output_observations"] is None


def test_atomic_terminal_result_observation_and_daemon_cursor(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    value = terminal(store)
    store.remember_request(value.execution_id, "{}")
    assert value.ended_at is not None
    event = ResultEvent(
        execution_id=value.execution_id, seq=1, time=value.ended_at, execution=value
    )
    store._db.execute(
        "CREATE TRIGGER fail_observation BEFORE UPDATE OF source_seq ON execution_recovery"
        " BEGIN SELECT RAISE(ABORT,'synthetic crash'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic"):
        store.finish(value, (event,), 9, observation=OutputReport(), observation_gap=None)
    saved = store.execution(value.execution_id)
    assert saved is not None
    assert saved.state == ExecutionState.QUEUED
    recovery = store.recovery(value.execution_id)
    assert recovery is not None
    assert recovery[2] == 0
    assert not store.observation_page(SCOPE).records
    store._db.execute("DROP TRIGGER fail_observation")
    store.finish(value, (event,), 9, observation=OutputReport(), observation_gap=None)
    store.finish(value, (event,), 9, observation=None, observation_gap="invalid")
    page = store.observation_page(SCOPE)
    assert page.next_after == 1
    assert len(page.records) == 1
    assert page.records[0].report is not None
    recovery = store.recovery(value.execution_id)
    assert recovery is not None
    assert recovery[2] == 9
    store.close()
    reopened = Store(tmp_path / "worker.db")
    assert reopened.observation_page(SCOPE).model_dump() == page.model_dump()
    reopened.close()


def test_original_dispatch_is_paired_and_never_rebound(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    value = terminal(store)
    store.remember_request(value.execution_id, "{}")
    inc = "inc-" + "1" * 24
    store.observation_dispatch(value, inc, ("original", "vm-original"), daemon=True)
    store.observation_dispatch(value, "inc-" + "2" * 24, ("new", "vm-new"), daemon=True)
    store.finish(value, (), observation=OutputReport(), observation_gap=None)
    pin = store.observation_page(SCOPE).records[0].marker.binding
    assert (pin.incarnation, pin.sandbox, pin.sandbox_id) == (inc, "original", "vm-original")
    recovery = store.recovery(value.execution_id)
    assert recovery is not None
    assert recovery[1] == inc
    store.close()


@pytest.mark.parametrize("damage", ["long-policy", "corrupt-binding", "missing-binding"])
def test_canonical_recovery_is_independent_of_derived_binding(tmp_path: Path, damage: str) -> None:
    store = Store(tmp_path / "worker.db")
    context = (
        CONTEXT.model_copy(update={"policy_revision": "x" * 257})
        if damage == "long-policy"
        else CONTEXT
    )
    value = terminal(store, context=context)
    store.remember_request(value.execution_id, "{}")
    if damage == "corrupt-binding":
        store._db.execute("UPDATE output_observation_attempts SET binding=?", (b"invalid",))
    elif damage == "missing-binding":
        store._db.execute("DELETE FROM output_observation_attempts")
    original = "inc-" + "1" * 24
    store.observation_dispatch(value, original, ("original", "vm-original"), daemon=True)
    store.observation_dispatch(value, "inc-" + "2" * 24, ("new", "vm-new"), daemon=True)
    recovery = store.recovery(value.execution_id)
    assert recovery is not None
    assert recovery[1] == original
    store.finish(value, (), observation=OutputReport(), observation_gap=None)
    saved = store.execution(value.execution_id)
    assert saved is not None
    assert saved.state == ExecutionState.SUCCEEDED
    store.close()


def test_receipt_time_is_not_a_legacy_naive_terminal_time(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    value = terminal(store).model_copy(update={"ended_at": datetime(2001, 1, 1)})
    earliest = datetime.now(UTC)
    store.finish(value, (), observation=OutputReport(), observation_gap=None)
    marker = store.observation_page(SCOPE).records[0].marker
    assert earliest <= marker.received_at <= datetime.now(UTC)
    saved = store.execution(value.execution_id)
    assert saved is not None
    assert saved.ended_at == value.ended_at
    store.close()


def test_pages_are_bounded_and_target_is_pinned(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    report = OutputReport(
        entries=tuple(
            OutputObservation(
                kind="regular_stat_difference",
                area="session",
                path_b64="eA=="
                if index == 0
                else base64.b64encode(f"{index}-".encode() + b"x" * 300).decode(),
                after=OutputStat(mtime_ns=1, size=1, inode=1),
            )
            for index in range(20)
        )
    )
    for index in range(10):
        store.finish(terminal(store, str(index)), (), observation=report, observation_gap=None)
    first = store.observation_page(SCOPE)
    assert len(first.model_dump_json().encode()) <= MAX_PAGE_BYTES
    assert 0 < first.next_after < first.target
    store.finish(terminal(store, "later"), (), observation=report, observation_gap=None)
    last, seen = first.next_after, list(first.records)
    while last < first.target:
        page = store.observation_page(
            SCOPE, after=last, target=first.target, stream_id=first.stream_id
        )
        assert len(page.model_dump_json().encode()) <= MAX_PAGE_BYTES
        assert page.next_after > last
        seen.extend(page.records)
        last = page.next_after
    assert [record.marker.seq for record in seen] == list(range(1, 11))
    assert store.observation_page(SCOPE, after=last).target == 11
    store.close()


def test_legacy_unfinished_attempt_has_no_backfilled_dispatch(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    value = terminal(store)
    store._db.execute("DELETE FROM output_observation_attempts")
    store.finish(value, (), observation=OutputReport(), observation_gap=None)
    binding = store.observation_page(SCOPE).records[0].marker.binding
    assert binding.binding_unavailable
    assert not binding.dispatched
    assert binding.incarnation is None
    assert binding.sandbox_id is None
    store.close()


def test_changed_derived_request_digest_cannot_claim_canonical_identity(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    value = terminal(store)
    binding = store.observations.binding(value).model_copy(update={"request_sha256": "b" * 64})
    store._db.execute(
        "UPDATE output_observation_attempts SET binding=?", (binding.model_dump_json().encode(),)
    )
    store.finish(value, (), observation=OutputReport(), observation_gap=None)
    saved = store.execution(value.execution_id)
    assert saved is not None
    assert saved.state == ExecutionState.SUCCEEDED
    with pytest.raises(WorkerError, match="history counters"):
        store.observation_page(SCOPE)
    store.close()


def test_scope_local_sequences_and_retention_floors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "worker.db")
    other = CONTEXT.model_copy(update={"session_id": "s2"})
    monkeypatch.setattr(output_observations, "MAX_DETAILS", 2)
    for name, context in (("a", CONTEXT), ("b", other), ("c", CONTEXT)):
        store.finish(
            terminal(store, name, context), (), observation=OutputReport(), observation_gap=None
        )
    page = store.observation_page(SCOPE)
    assert [record.marker.seq for record in page.records] == [1, 2]
    assert page.evicted_through == 1
    assert page.records[0].marker.gap == "evicted"
    assert page.records[0].report is None
    assert page.records[1].report is not None
    other_page = store.observation_page(
        OutputScope(**other.model_dump(include=set(OutputScope.model_fields)))
    )
    assert other_page.evicted_through == 0
    assert other_page.target == 1
    with pytest.raises(WorkerError, match="changed scope"):
        store.observation_page(SCOPE, stream_id=other_page.stream_id)
    assert not store.observation_page(SCOPE.model_copy(update={"project_id": "other"})).records
    store.close()


def test_quota_records_gap_without_changing_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "worker.db")
    monkeypatch.setattr(output_observations, "MAX_DETAILS", 0)
    value = terminal(store)
    store.finish(value, (), observation=OutputReport(), observation_gap=None)
    saved = store.execution(value.execution_id)
    assert saved is not None
    assert saved.state == ExecutionState.SUCCEEDED
    assert store.observation_page(SCOPE).records[0].marker.gap == "quota"
    store.close()


def test_orphan_detail_counts_against_global_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "worker.db")
    monkeypatch.setattr(output_observations, "MAX_DETAILS", 1)
    payload = OutputReport().model_dump_json().encode()
    store._db.execute(
        "INSERT INTO output_observation_details VALUES(?,?,?)", ("missing", payload, len(payload))
    )
    value = terminal(store)
    store.finish(value, (), observation=OutputReport(), observation_gap=None)
    assert store.observation_page(SCOPE).records[0].marker.gap == "quota"
    assert store._db.execute("SELECT count(*) FROM output_observation_details").fetchone()[0] == 1
    saved = store.execution(value.execution_id)
    assert saved is not None
    assert saved.state == ExecutionState.SUCCEEDED
    store.close()


@pytest.mark.parametrize(("after", "target"), [(2**64, None), (0, 2**64), (True, None), (0, 1.0)])
def test_cursor_bounds_precede_sql(tmp_path: Path, after: object, target: object) -> None:
    store = Store(tmp_path / "worker.db")
    with pytest.raises(WorkerError, match="invalid observation cursor"):
        store.observation_page(SCOPE, after=after, target=target)  # type: ignore[arg-type]
    store.close()


@pytest.mark.parametrize("field", ["marker", "binding", "payload"])
def test_corrupt_large_metadata_is_clipped_before_python(tmp_path: Path, field: str) -> None:
    store = Store(tmp_path / "worker.db")
    value = terminal(store)
    store.finish(value, (), observation=OutputReport(), observation_gap=None)
    table = "output_observation_details" if field == "payload" else "output_observation_attempts"
    store._db.execute(
        f"UPDATE {table} SET {field}=?",
        ("z" * (20 << 20),),
    )
    tracemalloc.start()
    try:
        with pytest.raises(WorkerError, match="invalid retained"):
            store.observation_page(SCOPE)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        store.close()
    assert peak < 200_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head", -1),
        ("head", 2**63 - 1),
        ("head", 1.5),
        ("head", "x" * 20000000),
        ("evicted_through", 2),
        ("unavailable", "unknown"),
    ],
)
def test_corrupt_scope_cannot_veto_terminal_durability(
    tmp_path: Path, field: str, value: object
) -> None:
    store = Store(tmp_path / "worker.db")
    store.finish(terminal(store, "first"), (), observation=OutputReport(), observation_gap=None)
    store._db.execute(
        f"UPDATE output_observation_scopes SET {field}=?",
        (value,),
    )
    current = terminal(store, "healthy")
    store.remember_request(current.execution_id, "{}")
    assert current.ended_at is not None
    event = ResultEvent(
        execution_id=current.execution_id, seq=1, time=current.ended_at, execution=current
    )
    store.finish(current, (event,), 7, observation=OutputReport(), observation_gap=None)
    saved = store.execution(current.execution_id)
    assert saved is not None
    assert saved.state == ExecutionState.SUCCEEDED
    recovery = store.recovery(current.execution_id)
    assert recovery is not None
    assert recovery[2] == 7
    raw = store._db.execute(
        "SELECT marker FROM output_observation_attempts WHERE execution=?", (current.execution_id,)
    ).fetchone()[0]
    marker = OutputMarker.model_validate_json(raw)
    assert marker.seq is None
    assert marker.gap == "invalid"
    with pytest.raises(WorkerError, match="history counters"):
        store.observation_page(SCOPE)
    store.close()


def test_corrupt_current_binding_holds_observation_only(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    value = terminal(store)
    store._db.execute("UPDATE output_observation_attempts SET binding=?", (b"not-json",))
    store.finish(value, (), observation=OutputReport(), observation_gap=None)
    saved = store.execution(value.execution_id)
    assert saved is not None
    assert saved.state == ExecutionState.SUCCEEDED
    with pytest.raises(WorkerError, match="history counters"):
        store.observation_page(SCOPE)
    store.close()


@pytest.mark.parametrize("field", ["size", "payload"])
def test_corrupt_detail_scalar_cannot_be_materialized_or_block_completion(
    tmp_path: Path, field: str
) -> None:
    store = Store(tmp_path / "worker.db")
    store.finish(terminal(store, "old"), (), observation=OutputReport(), observation_gap=None)
    store._db.execute(
        f"UPDATE output_observation_details SET {field}=?",
        ("z" * 20000000,),
    )
    value = terminal(store, "healthy")
    tracemalloc.start()
    try:
        store.finish(value, (), observation=OutputReport(), observation_gap=None)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 200_000
    saved = store.execution(value.execution_id)
    assert saved is not None
    assert saved.state == ExecutionState.SUCCEEDED
    assert store.observation_page(SCOPE, after=1).records[0].marker.gap == "quota"
    store.close()


def test_old_terminal_history_has_explicit_baseline_and_no_backfill(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    old = terminal(store, "old")
    store.update_execution(old)
    store._db.execute("DELETE FROM output_observation_attempts")
    store.close()
    reopened = Store(tmp_path / "worker.db")
    page = reopened.observation_page(SCOPE)
    assert page.history == "feature_baseline_only"
    assert page.target == 0
    reopened.finish(old, (), observation=OutputReport(), observation_gap=None)
    assert reopened.observation_page(SCOPE).target == 0
    reopened.close()


@pytest.mark.asyncio
async def test_derived_projection_exception_does_not_change_legacy_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = object.__new__(executor.Executor)
    value.root = tmp_path
    (tmp_path / "sessions/s1").mkdir(parents=True)
    (tmp_path / "shared").mkdir()
    before = value.snapshot("s1")
    (tmp_path / "sessions/s1/healthy").write_text("known")

    def broken(*args: object) -> None:
        raise ValueError("synthetic derived failure")

    monkeypatch.setattr(executor, "observation_report", broken)
    written = await value.written("s1", before)
    assert written["written_files"] == ["healthy"]
    assert written["output_observations"] is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "missing"),
        ({"protocol": True}, "invalid"),
        ({"x": "x" * 20000}, "oversized"),
        ({"entries": [{}] * 65}, "oversized"),
    ],
)
def test_independent_normalization_is_bounded(raw: object, expected: str) -> None:
    report, gap = output_observations.normalize(raw)
    assert report is None
    assert gap == expected


@pytest.mark.asyncio
async def test_daemon_compaction_preserves_independent_report() -> None:
    value = object.__new__(daemon.Daemon)
    value.history = OrderedDict({"exe-a": daemon.History()})
    value.bytes = 0
    report = OutputReport().model_dump(mode="json")
    await value.send(
        {
            "type": "event",
            "execution": "exe-a",
            "event": "end",
            "state": "succeeded",
            "output_observations": report,
            "result": {"stdout": {"text": "x" * 200000}},
        }
    )
    compact = value.history["exe-a"].compact_end
    assert compact is not None
    assert len(compact[1]) <= daemon.SNAPSHOT_BYTES
    assert json.loads(compact[1])["output_observations"] == report


@pytest.mark.asyncio
async def test_actual_http_terminal_reports_and_invalid_sidecar_do_not_change_outcome(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "worker.db")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(), tmp_path),
        Blobs(tmp_path / "blobs"),
        pool_id="test",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    await manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN))
    try:
        for op, kind, body, expected in (
            ("success", "shell", {"script": "printf known > out"}, "succeeded"),
            (
                "failed",
                "cell",
                {
                    "language": "python",
                    "code": "open('partial','w').write('known'); raise RuntimeError('expected')",
                },
                "failed",
            ),
            (
                "job",
                "job",
                {"script": "printf known > job", "purpose": "test", "expected_progress": "done"},
                "succeeded",
            ),
        ):
            response = await client.post(
                "/v1/executions",
                json={"operation_id": op, "kind": kind, "context": CONTEXT.model_dump(), **body},
                headers=AUTH,
            )
            assert response.status == 201
            accepted_value = await response.json()
            result = await asyncio.wait_for(manager.wait(accepted_value["execution_id"]), 30)
            assert result.state.value == expected
        assert store.observation_page(SCOPE).target == 3
        assert all(record.report is not None for record in store.observation_page(SCOPE).records)

        def corrupt(apply: Any, live: Any, env: str, message: dict[str, Any]) -> None:
            if message.get("event") == "end":
                message = {**message, "output_observations": {"protocol": True}}
            apply(live, env, message)

        view(manager).wrap_apply(monkeypatch, corrupt)
        value, _ = await manager.submit(
            ShellRequest(operation_id="invalid", context=CONTEXT, script="echo still-healthy")
        )
        result = await asyncio.wait_for(manager.wait(value.execution_id), 30)
        assert result.state == ExecutionState.SUCCEEDED
        assert result.result is not None
        assert result.result.stdout is not None
        assert result.result.stdout.text == "still-healthy\n"
        assert store.observation_page(SCOPE, after=3).records[0].marker.gap == "invalid"
        response = await client.get("/v1/output-observations", headers=AUTH)
        assert response.status == 404  # the prerequisite adds no public feed authority
    finally:
        await manager.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["none", "long-policy", "corrupt-binding"])
async def test_actual_daemon_restart_preserves_attempt_and_terminal_observation(
    tmp_path: Path, damage: str
) -> None:
    store = Store(tmp_path / "worker.db")
    backend = LocalBackend(LocalBackendSettings(daemon=True), tmp_path)

    def make_manager() -> ExecutionManager:
        value = ExecutionManager(
            store,
            backend,
            Blobs(tmp_path / "blobs"),
            pool_id="test",
            max_artifact_size=1024,
            state_dir=tmp_path,
        )
        value.daemon_identity = lambda _environment: ("synthetic-vm", "synthetic-id")
        return value

    manager = make_manager()
    await manager.start()
    try:
        context = (
            CONTEXT.model_copy(update={"policy_revision": "x" * 257})
            if damage == "long-policy"
            else CONTEXT
        )
        value, _ = await manager.submit(
            JobRequest(
                operation_id="restart",
                context=context,
                script="printf before >> runs; while [ ! -f release ]; do sleep .05; done; "
                "printf after >> runs",
                purpose="recovery fixture",
                expected_progress="one attempt",
            )
        )
        path = backend.root / "env1/project/sessions/s1/runs"
        async with asyncio.timeout(10):
            while not path.exists():
                await asyncio.sleep(0.01)
        channel = view(manager).channel("env1")
        incarnation = channel.incarnation
        if damage == "corrupt-binding":
            store._db.execute("UPDATE output_observation_attempts SET binding=?", (b"invalid",))
        await manager.close()
        saved = store.execution(value.execution_id)
        assert saved is not None
        assert saved.state in {ExecutionState.RUNNING, ExecutionState.QUEUED}
        store.close()
        store = Store(tmp_path / "worker.db")
        manager = make_manager()
        await manager.start()
        (path.parent / "release").touch()
        ended = await asyncio.wait_for(manager.wait(value.execution_id), 10)
        assert ended.state == ExecutionState.SUCCEEDED
        assert ended.attempt_id == value.attempt_id
        assert path.read_bytes() == b"beforeafter"
        recovery = store.recovery(value.execution_id)
        assert recovery is not None
        assert recovery[1] == incarnation
        if damage == "corrupt-binding":
            with pytest.raises(WorkerError, match="history counters"):
                store.observation_page(SCOPE)
        else:
            record = store.observation_page(SCOPE).records[0]
            assert record.report is not None
            assert record.report.before_started_ns is not None
            assert record.report.after_finished_ns is not None
            assert any(entry.raw() == b"runs" for entry in record.report.entries)
            if damage == "none":
                assert record.marker.binding.incarnation == incarnation
                assert record.marker.binding.sandbox_id == "synthetic-id"
            else:
                assert record.marker.gap == "binding_unavailable"
    finally:
        # close detaches by design; explicit shutdown owns the isolated test daemon.
        try:
            await shut_down_daemon(manager)
        finally:
            store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["shell", "cell", "job"])
async def test_actual_http_cancelled_partial_outputs_remain_observations(
    aiohttp_client: Any, tmp_path: Path, kind: str
) -> None:
    store = Store(tmp_path / "worker.db")
    backend = LocalBackend(LocalBackendSettings(), tmp_path)
    manager = ExecutionManager(
        store,
        backend,
        Blobs(tmp_path / "blobs"),
        pool_id="test",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    await manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN))
    body: dict[str, object] = {
        "kind": kind,
        "operation_id": f"cancel-{kind}",
        "context": CONTEXT.model_dump(),
    }
    if kind == "cell":
        body.update(
            language="python",
            code="import time; open('partial','w').write('known'); time.sleep(30)",
        )
    else:
        body.update(script="printf known > partial; sleep 30")
    if kind == "job":
        body.update(purpose="partial fixture", expected_progress="cancel")
    try:
        response = await client.post("/v1/executions", json=body, headers=AUTH)
        assert response.status == 201
        value = await response.json()
        async with asyncio.timeout(15):
            while not (backend.root / "env1/project/sessions/s1/partial").exists():
                await asyncio.sleep(0.01)
        response = await client.post(f"/v1/executions/{value['execution_id']}/cancel", headers=AUTH)
        assert response.status == 200
        ended = await asyncio.wait_for(manager.wait(value["execution_id"]), 15)
        assert ended.state == ExecutionState.CANCELLED
        record = store.observation_page(SCOPE).records[0]
        assert record.marker.potentially_partial
        assert record.marker.state == ExecutionState.CANCELLED
        assert record.report is not None
        assert any(entry.raw() == b"partial" for entry in record.report.entries)
    finally:
        await manager.close()
        store.close()


@pytest.mark.asyncio
async def test_marked_lab_recipe_composes_with_actual_local_http_and_readonly_journal(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(__file__).resolve().parents[3] / ".agents/labs/output_observations.py"
    spec = importlib.util.spec_from_file_location("observation_lab_fixture", source)
    assert spec is not None
    assert spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper, "LAB", tmp_path / "missing-lab")
    with pytest.raises(FileNotFoundError):
        helper.marked()
    monkeypatch.setattr(helper, "marked", lambda **_fields: None)
    monkeypatch.setattr(helper, "DATABASE", tmp_path / "worker.db")
    monkeypatch.setattr(helper, "EVIDENCE", tmp_path / "evidence/observations.json")
    store = Store(tmp_path / "worker.db")
    backend = LocalBackend(LocalBackendSettings(), tmp_path)
    manager = ExecutionManager(
        store,
        backend,
        Blobs(tmp_path / "blobs"),
        pool_id="test",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    manager.daemon_identity = lambda _environment: ("synthetic-vm", "synthetic-id")
    await manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN))
    monkeypatch.setattr(helper, "URL", str(client.make_url("")))
    import aiohttp

    try:
        async with aiohttp.ClientSession(
            headers=AUTH, timeout=aiohttp.ClientTimeout(total=90)
        ) as http:
            checks = helper.Checks(http)
            checks.evidence["environment"] = {
                "sandbox": "synthetic-vm",
                "sandbox_id": "synthetic-id",
            }
            await checks.verify()
            session = backend.root / helper.ENVIRONMENT / "project/sessions" / helper.SESSION
            assert all(not (session / f"many-{i:03}").exists() for i in range(300))
            outcomes = checks.evidence["terminal-outcomes"]
            page = checks.evidence["scoped-journal"]
            pdf = next((outcome for outcome in outcomes if outcome["kind"] == "pdf"), None)
            for defect in ("missing-cell-report", "missing-cancelled-path", "wrong-partial-state"):
                damaged = copy.deepcopy(page)
                by_operation = {
                    record["marker"]["binding"]["operation_id"]: record
                    for record in damaged["records"]
                }
                if defect == "missing-cell-report":
                    by_operation[f"lab24-cell-{helper.STAMP}"]["report"] = None
                elif defect == "missing-cancelled-path":
                    record = by_operation[f"lab24-cancel-cell-{helper.STAMP}"]
                    record["report"]["entries"] = []
                else:
                    record = by_operation[f"lab24-job-failed-{helper.STAMP}"]
                    record["marker"]["potentially_partial"] = False
                with pytest.raises(AssertionError):
                    checks.verify_reports(outcomes, damaged, pdf)
            await checks.checkpoint()
            await checks.running()
            await checks.resume()
            damaged = helper.journal()
            recovered_id = checks.evidence["running"]["execution"]["execution_id"]
            for record in damaged["records"]:
                if record["marker"]["binding"]["execution_id"] == recovered_id:
                    record["report"] = None
            with monkeypatch.context() as missing_recovery_report:
                missing_recovery_report.setattr(helper, "journal", lambda **_fields: damaged)
                with pytest.raises(AssertionError):
                    await checks.resume()
            await checks.restart_verify()
            assert checks.evidence["durable-prefix"]["target"] > 10
    finally:
        await manager.close()
        store.close()
