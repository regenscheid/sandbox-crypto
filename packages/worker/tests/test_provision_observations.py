# SPDX-License-Identifier: MIT
"""Only retained SQLite and in-process HTTP fixtures; no provisioning or guest calls."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentRevision,
    EnvironmentState,
    ErrorKind,
    ExecutionContext,
    ProbeResult,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
    ResolvedFile,
)
from sandbox_science.contracts.provision_observations import (
    MAX_OBSERVATION_BYTES,
    RevisionProbePage,
)
from sandbox_science.contracts.provisioning import CatalogRef
from sandbox_science.contracts.revisions import LockfileRef
from sandbox_science.worker.app import create_app
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.provisioner import Provisioner
from sandbox_science.worker.settings import ClientSettings, token_digest
from sandbox_science.worker.store import RevisionRecord, Store

NOW = datetime(2026, 10, 13, tzinfo=UTC)
TOKEN = "test-secret-observation-pool-not-a-credential"  # noqa: S105
CLIENT = "test-secret-observation-project-not-a-credential"
BASE = "/v1/environments/env/provisioning/observations"


def operation(name: str = "prv-one", *, files: int = 0, seq: int = 0) -> ProvisionOperation:
    return ProvisionOperation(
        provision_id=name,
        request=ProvisionRequest(
            operation_id="request-" + name,
            context=ExecutionContext(project_id="p", session_id="s", environment_id="env"),
            purpose="Synthetic observation only",
            conda=("networkx",),
        ),
        state=ProvisionState.AWAITING_PERMISSION,
        permission_request=1,
        files=tuple(
            ResolvedFile(
                ecosystem="conda",
                name="pkg" + str(i),
                version="1",
                file="pkg" + str(i) + ".conda",
                license=None if i % 2 else "MIT",
                agpl=False,
                action="escalate",
            )
            for i in range(files)
        ),
        submitted_at=NOW,
        seq=seq,
    )


def revision(number: int, *, outputs: int = 2) -> EnvironmentRevision:
    return EnvironmentRevision(
        revision=number,
        fingerprint=f"{number:032x}",
        recorded_at=NOW,
        managed=None if number == 0 else True,
        probes=tuple(
            ProbeResult(
                probe="probe" + str(i),
                kind="functional" if i % 2 else "import_check",
                status="passed",
                revision=number,
                output="🧪" * outputs,
                recorded_at=NOW,
            )
            for i in range(9)
        ),
    )


@pytest.fixture
def store(tmp_path: Path):
    value = Store(tmp_path / "worker.sqlite3")
    try:
        yield value
    finally:
        value.close()


def insert(store: Store, value: ProvisionOperation) -> None:
    store.insert_provision(value, "0" * 64)


def entry(store: Store, number: int, *, outputs: int = 2) -> None:
    value = revision(number, outputs=outputs)
    store.add_revision("env", RevisionRecord(number, value.fingerprint, ""), value)


def test_large_resolve_summary_is_small_and_exact_files_page(store: Store):
    insert(store, operation(files=2000))
    entry(store, 0, outputs=2000)
    result = store.provision_observations.summary("p", "env", since=0)
    assert result.queue_total == 1
    assert result.operations[0].files_total == 2000
    assert result.operations[0].probes_total == 0
    assert len(result.model_dump_json().encode()) < 2048
    assert "🧪" not in result.model_dump_json()
    page = store.provision_observations.files(
        "p", "env", "prv-one", since=0, seq=0, offset=1992, limit=8
    )
    assert [f.name for f in page.files] == ["pkg" + str(i) for i in range(1992, 2000)]
    assert page.total == 2000
    assert page.next_offset is None
    assert page.files[1].license is None


def test_detail_preserves_request_but_does_not_load_error_or_notes(store: Store):
    insert(store, operation())
    result = store.provision_observations.detail("p", "env", "prv-one", since=0, seq=0)
    assert result.request.purpose == "Synthetic observation only"
    assert not result.error_present
    assert result.error_kind is None
    assert result.summary.permission_request == 1


@pytest.mark.parametrize("section", ["files", "probes"])
@pytest.mark.parametrize("corruption", ["string_object", "duplicate", "escaped_duplicate"])
def test_selected_elements_keep_original_object_type_and_unique_keys(
    store: Store, section: str, corruption: str
):
    if section == "files":
        value = operation(files=1)
        insert(store, value)
        raw = value.model_dump_json()
        table = "provisions"
        field, original, alternate = "action", "escalate", "allow"
    else:
        entry(store, 0)
        raw = revision(0).model_dump_json()
        table = "revision_entries"
        field, original, alternate = "status", "passed", "failed"
    if corruption == "string_object":
        record = json.loads(raw)
        record[section][0] = json.dumps(record[section][0])
        raw = json.dumps(record)
    else:
        key = field if corruption == "duplicate" else "\\u00" + hex(ord(field[0]))[2:] + field[1:]
        raw = raw.replace(
            f'"{field}":"{original}"',
            f'"{key}":"{alternate}","{field}":"{original}"',
            1,
        )
    store._db.execute(f"UPDATE {table} SET record=?", (raw,))  # noqa: S608
    if section == "files":
        with pytest.raises(WorkerError, match="unavailable"):
            store.provision_observations.files(
                "p", "env", "prv-one", since=0, seq=0, offset=0, limit=8
            )
    else:
        pin = (
            store.provision_observations.revisions(
                "p", "env", since=0, after=-1, limit=8, target=None
            )
            .revisions[0]
            .record_sha256
        )
        with pytest.raises(WorkerError, match="unavailable"):
            store.provision_observations.probes("p", "env", 0, since=0, pin=pin, offset=0, limit=8)
    assert not store._db.in_transaction


@pytest.mark.parametrize("field", ["catalog", "lockfile"])
def test_compact_nested_projection_rejects_duplicate_keys(store: Store, field: str):
    if field == "catalog":
        value = operation().model_copy(update={"catalog": CatalogRef(version=1, sha256="0" * 64)})
        insert(store, value)
        raw = value.model_dump_json().replace('"version":1', '"version":2,"version":1')
        store._db.execute("UPDATE provisions SET record=?", (raw,))
    else:
        value = revision(0).model_copy(update={"lockfile": LockfileRef(sha256="a" * 64, size=3)})
        store.add_revision("env", RevisionRecord(0, value.fingerprint, ""), value)
        raw = value.model_dump_json().replace('"size":3', '"size":4,"size":3')
        store._db.execute("UPDATE revision_entries SET record=?", (raw,))
    if field == "catalog":
        with pytest.raises(WorkerError, match="unavailable"):
            store.provision_observations.detail("p", "env", "prv-one", since=0, seq=None)
    else:
        with pytest.raises(WorkerError, match="unavailable"):
            store.provision_observations.revisions(
                "p", "env", since=0, after=-1, limit=8, target=None
            )
    assert not store._db.in_transaction


@pytest.mark.parametrize(
    "error",
    [
        '{"kind":"unavailable","kind":"internal","message":"Synthetic"}',
        '{"kind":"internal","\\u006bind":"internal","message":"Synthetic"}',
        '{"kind":null,"message":"Synthetic"}',
        '{"message":"Synthetic"}',
        '"internal"',
    ],
)
def test_selected_error_kind_must_be_unambiguous_original_text(store: Store, error: str):
    insert(store, operation())
    raw = operation().model_dump_json().replace('"error":null', '"error":' + error)
    store._db.execute("UPDATE provisions SET record=?", (raw,))
    with pytest.raises(WorkerError, match="unavailable"):
        store.provision_observations.detail("p", "env", "prv-one", since=0, seq=None)
    assert not store._db.in_transaction


def test_error_kind_projection_never_materializes_large_unselected_body(store: Store):
    insert(store, operation())
    raw = (
        operation()
        .model_dump_json()
        .replace(
            '"error":null',
            '"error":' + json.dumps({"kind": "internal", "message": "x" * (2 * 1024 * 1024)}),
        )
    )
    store._db.execute("UPDATE provisions SET record=?", (raw,))
    tracemalloc.start()
    try:
        detail = store.provision_observations.detail("p", "env", "prv-one", since=0, seq=None)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert detail.error_present
    assert detail.error_kind == ErrorKind.INTERNAL
    assert peak < 300000
    assert not store._db.in_transaction


def test_exact_file_pages_and_stale_seq(store: Store):
    value = operation(files=19)
    insert(store, value)
    names = []
    offset = 0
    while True:
        page = store.provision_observations.files(
            "p", "env", "prv-one", since=0, seq=0, offset=offset, limit=8
        )
        assert page.offset == offset
        assert page.limit == 8
        names.extend(f.name for f in page.files)
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert names == ["pkg" + str(i) for i in range(19)]
    store.update_provision(value.model_copy(update={"seq": 1}))
    with pytest.raises(WorkerError, match="sequence changed"):
        store.provision_observations.files("p", "env", "prv-one", since=0, seq=0, offset=8, limit=8)


def test_revision_target_pin_pages_and_mutable_probe_refusal(store: Store):
    for n in range(10):
        entry(store, n)
    page = store.provision_observations.revisions(
        "p", "env", since=0, after=-1, limit=8, target=None
    )
    assert page.retained_total == 10
    assert page.first_retained == 0
    assert page.target == 9
    assert page.next_after == 7
    entry(store, 10)
    last = store.provision_observations.revisions(
        "p", "env", since=0, after=page.next_after, limit=8, target=page.target
    )
    assert [v.revision for v in last.revisions] == [8, 9]
    assert last.next_after is None
    pin = page.revisions[0].record_sha256
    detail = store.provision_observations.probes("p", "env", 0, since=0, pin=pin, offset=0, limit=8)
    assert detail.total == 9
    assert detail.next_offset == 8
    assert detail.probes[0].kind == "import_check"
    assert detail.probes[1].kind == "functional"
    store.save_revision_entry("env", revision(0, outputs=3))
    with pytest.raises(WorkerError, match="record changed"):
        store.provision_observations.probes("p", "env", 0, since=0, pin=pin, offset=8, limit=8)


def test_selected_probe_bytes_bound_uses_honest_continuation(store: Store):
    entry(store, 0, outputs=2000)
    page = store.provision_observations.revisions(
        "p", "env", since=0, after=-1, limit=8, target=None
    )
    probes = store.provision_observations.probes(
        "p", "env", 0, since=0, pin=page.revisions[0].record_sha256, offset=0, limit=8
    )
    assert 0 < len(probes.probes) <= 8
    assert len(probes.model_dump_json().encode()) <= MAX_OBSERVATION_BYTES
    assert probes.next_offset == len(probes.probes)


@pytest.mark.parametrize(
    "kind",
    ["record", "scalar", "submitted", "finished", "request_duplicate", "parent_swap", "array"],
)
def test_corrupt_operation_refuses_without_full_python_allocation(store: Store, kind: str):
    insert(store, operation())
    record = json.loads(operation().model_dump_json())
    if kind == "record":
        store._db.execute("UPDATE provisions SET record=?", ("x" * (20 * 1024 * 1024),))
    elif kind == "scalar":
        record["state"] = "x" * (2 * 1024 * 1024)
        store._db.execute("UPDATE provisions SET record=?", (json.dumps(record),))
    elif kind in ("submitted", "finished"):
        store._db.execute(
            {
                "submitted": "UPDATE provisions SET submitted=?",
                "finished": "UPDATE provisions SET finished=?",
            }[kind],
            ("x" * (20 * 1024 * 1024),),
        )
    elif kind == "request_duplicate":
        raw = (
            operation()
            .model_dump_json()
            .replace('"project_id":"p"', '"project_id":"p","project_id":"q"')
        )
        store._db.execute("UPDATE provisions SET record=?", (raw,))
    else:
        if kind == "parent_swap":
            record["request"]["context"]["project_id"] = "q"
        else:
            record["files"] = "invalid"
        store._db.execute("UPDATE provisions SET record=?", (json.dumps(record),))
    tracemalloc.start()
    try:
        with pytest.raises(WorkerError, match="unavailable"):
            store.provision_observations.summary("p", "env", since=0)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 300000


def test_raw_size_guard_precedes_any_sql_json(store: Store):
    insert(store, operation())
    store._db.execute("UPDATE provisions SET record=?", ("x" * (20 * 1024 * 1024),))
    calls = []

    def forbidden(value):
        calls.append(True)
        raise AssertionError("must not parse oversized record")

    store._db.create_function("json_valid", 1, forbidden)
    with pytest.raises(WorkerError):
        store.provision_observations.summary("p", "env", since=0)
    assert calls == []


@pytest.mark.parametrize("limit", [-1, 0, 9, 10**8, True])
def test_direct_call_limit_refuses_before_materialization(store: Store, limit):
    insert(store, operation(files=10))
    with pytest.raises(WorkerError) as error:
        store.provision_observations.files(
            "p", "env", "prv-one", since=0, seq=0, offset=0, limit=limit
        )
    assert error.value.kind is ErrorKind.INVALID_REQUEST


def test_creation_boundary_unknown_current_and_empty_history(store: Store):
    insert(store, operation())
    entry(store, 0)
    result = store.provision_observations.summary("p", "env", since=NOW.timestamp() + 1)
    assert result.queue_total == 0
    assert result.current_revision is None
    assert result.gate.recorded is False
    assert result.gate.operation is None
    history = store.provision_observations.revisions(
        "p", "env", since=NOW.timestamp() + 1, after=-1, limit=8, target=None
    )
    assert history.retained_total == 0
    assert history.target is None
    assert history.first_retained is None
    with pytest.raises(WorkerError):
        store.provision_observations.detail("p", "env", "prv-one", since=NOW.timestamp() + 1, seq=0)


def test_read_snapshot_no_persistent_mutation_and_rollback_on_error(store: Store):
    insert(store, operation(files=10))
    entry(store, 0)
    before = store._db.total_changes
    store.provision_observations.summary("p", "env", since=0)
    with pytest.raises(WorkerError):
        store.provision_observations.files("p", "env", "prv-one", since=0, seq=9, offset=0, limit=8)
    assert store._db.total_changes == before
    assert not store._db.in_transaction


def test_unknown_probe_revision_stays_unknown(store: Store):
    entry(store, 0)
    value = revision(0)
    store.save_revision_entry(
        "env",
        value.model_copy(
            update={"probes": (value.probes[0].model_copy(update={"revision": None}),)}
        ),
    )
    page = store.provision_observations.revisions(
        "p", "env", since=0, after=-1, limit=8, target=None
    )
    result = store.provision_observations.probes(
        "p", "env", 0, since=0, pin=page.revisions[0].record_sha256, offset=0, limit=8
    )
    assert result.probes[0].revision is None
    assert RevisionProbePage.model_validate_json(result.model_dump_json(), strict=True) == result


@pytest.mark.asyncio
async def test_real_http_scope_queries_and_no_active_component_calls(store: Store):
    insert(store, operation(files=9))
    entry(store, 0)
    info = EnvironmentInfo(
        environment_id="env",
        project_id="p",
        profile="minimal",
        image="synthetic-image",
        policy="sha256:" + "0" * 64,
        sandbox="owned",
        state=EnvironmentState.STOPPED,
        created_at=NOW - timedelta(seconds=1),
        updated_at=NOW,
    )

    class Records:
        def get(self, environment):
            return info if environment == "env" else None

    class Forbidden:
        async def forget(self, *args):
            raise AssertionError("must not forget a read target")

        def __getattr__(self, name):
            raise AssertionError("no active call: " + name)

    environments = cast(
        Environments,
        SimpleNamespace(
            records=Records(),
            deleted=[],
            required_deleting=[],
            confirmed_deleted=[],
            reusing=[],
            required_deleted=[],
        ),
    )
    manager = cast(
        ExecutionManager,
        SimpleNamespace(
            store=store,
            forget_revisions=lambda _: None,
            guard_package_deletion=lambda _: None,
            forget_package_gate=lambda _: None,
            guard_package_reuse=lambda _: None,
        ),
    )
    clients = (ClientSettings(name="read", token_sha256=token_digest(CLIENT), projects=("p",)),)
    app = create_app(manager, TOKEN, environments, clients, cast(Provisioner, Forbidden()))
    async with TestClient(TestServer(app)) as client:
        for suffix in (
            "",
            "/operations/prv-one?seq=0",
            "/operations/prv-one",
            "/operations/prv-one/files?seq=0",
            "/revisions",
        ):
            join = "&" if "?" in suffix else "?"
            response = await client.get(
                BASE + suffix + join + "project_id=p", headers={"Authorization": "Bearer " + CLIENT}
            )
            assert response.status == 200, await response.text()
            assert response.headers["Cache-Control"] == "no-store"
            assert len(await response.read()) <= MAX_OBSERVATION_BYTES
        for query, status in (
            ("?project_id=q", 404),
            ("?project_id=p&limit=9", 400),
            ("?project_id=p&project_id=p", 400),
            ("", 400),
        ):
            response = await client.get(BASE + query, headers={"Authorization": "Bearer " + CLIENT})
            assert response.status == status
            assert response.headers["Cache-Control"] == "no-store"
        assert (await client.get(BASE + "?project_id=p")).status == 401
        auth = {"Authorization": "Bearer " + CLIENT}
        history = await (await client.get(BASE + "/revisions?project_id=p", headers=auth)).json()
        pin = history["revisions"][0]["record_sha256"]
        selected = await client.get(
            BASE + "/revisions/0/probes?project_id=p&pin=" + pin, headers=auth
        )
        assert selected.status == 200
        probes = await selected.json()
        assert probes["total"] == 9
        assert probes["probes"][0]["revision"] == 0
        store.update_provision(
            operation(files=9, seq=1).model_copy(update={"state": ProvisionState.FINISHED})
        )
        current = await client.get(BASE + "/operations/prv-one?project_id=p", headers=auth)
        assert current.status == 200
        assert (await current.json())["summary"]["seq"] == 1
        stale = await client.get(BASE + "/operations/prv-one?project_id=p&seq=0", headers=auth)
        assert stale.status == 409
        missing = await client.get(BASE + "/operations/prv-one/files?project_id=p", headers=auth)
        assert missing.status == 400
        unknown = await client.get(
            BASE.replace("/env/", "/foreign/") + "?project_id=p", headers=auth
        )
        assert unknown.status == 404
    unsupported = create_app(manager, TOKEN, environments, clients)
    async with TestClient(TestServer(unsupported)) as client:
        response = await client.get(
            BASE + "?project_id=p", headers={"Authorization": "Bearer " + CLIENT}
        )
        assert response.status == 422


def test_queue_order_is_request_insertion_not_wall_clock(store: Store):
    first = operation("prv-first")
    second = operation("prv-second").model_copy(update={"submitted_at": NOW - timedelta(seconds=1)})
    insert(store, first)
    insert(store, second)
    summary = store.provision_observations.summary("p", "env", since=0)
    assert [item.provision_id for item in summary.operations] == ["prv-first", "prv-second"]


def test_queue_count_limit_precedes_json_work(store: Store):
    for n in range(33):
        insert(store, operation("prv-" + str(n)))
    calls = []

    def forbidden(value):
        calls.append(True)
        raise AssertionError("must not parse outside row quota")

    store._db.create_function("json_valid", 1, forbidden)
    with pytest.raises(WorkerError):
        store.provision_observations.summary("p", "env", since=0)
    assert calls == []


@pytest.mark.parametrize("column", ["record", "revision", "recorded", "lockfile"])
def test_revision_corrupt_columns_refuse_bounded(store: Store, column: str):
    entry(store, 0)
    queries = {
        "record": "UPDATE revision_entries SET record=?",
        "revision": "UPDATE revision_entries SET revision=?",
        "recorded": "UPDATE revision_entries SET recorded=?",
        "lockfile": "UPDATE revision_entries SET lockfile=?",
    }
    store._db.execute(queries[column], ("x" * (20 * 1024 * 1024),))
    tracemalloc.start()
    try:
        with pytest.raises(WorkerError):
            store.provision_observations.revisions(
                "p", "env", since=0, after=-1, limit=8, target=None
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 300000


def test_history_eviction_is_not_empty_selected_window(store: Store):
    for n in range(3):
        entry(store, n)
    store._db.execute("DELETE FROM revision_entries WHERE revision<2")
    with pytest.raises(WorkerError) as error:
        store.provision_observations.revisions("p", "env", since=0, after=-1, limit=8, target=1)
    assert error.value.kind is ErrorKind.CONFLICT
    page = store.provision_observations.revisions(
        "p", "env", since=0, after=-1, limit=8, target=None
    )
    assert page.first_retained == 2
    assert page.retained_total == 1


def test_gate_literal_status_and_missing_owner_fail_closed(store: Store):
    insert(store, operation())
    store._db.execute(
        "INSERT INTO package_execution_gates VALUES ('env',1,'hold','prv-one',NULL,'')"
    )
    result = store.provision_observations.summary("p", "env", since=0)
    assert result.gate.recorded
    assert result.gate.mode == "hold"
    assert result.gate.operation == "prv-one"
    store._db.execute("UPDATE package_execution_gates SET operation='foreign'")
    with pytest.raises(WorkerError):
        store.provision_observations.summary("p", "env", since=0)


def test_revision_streaming_hash_does_not_materialize_all_output(store: Store):
    entry(store, 0, outputs=2000)
    before = store._db.execute("SELECT record FROM revision_entries").fetchone()[0].encode()
    import hashlib

    tracemalloc.start()
    try:
        page = store.provision_observations.revisions(
            "p", "env", since=0, after=-1, limit=8, target=None
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page.revisions[0].record_sha256 == hashlib.sha256(before).hexdigest()
    assert peak < 300000


def test_concurrent_revision_update_keeps_one_read_snapshot(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    entry(store, 0)
    import hashlib

    original = store._db.execute("SELECT record FROM revision_entries").fetchone()[0].encode()
    replacement = revision(0, outputs=7).model_dump_json()
    pin = store.provision_observations._pin
    with contextlib.closing(sqlite3.connect(store._path, isolation_level=None)) as writer:

        def changed(row_id):
            writer.execute("UPDATE revision_entries SET record=?", (replacement,))
            return pin(row_id)

        monkeypatch.setattr(store.provision_observations, "_pin", changed)
        page = store.provision_observations.revisions(
            "p", "env", since=0, after=-1, limit=8, target=None
        )
    monkeypatch.setattr(store.provision_observations, "_pin", pin)
    assert page.revisions[0].record_sha256 == hashlib.sha256(original).hexdigest()
    with pytest.raises(WorkerError, match="record changed"):
        store.provision_observations.probes(
            "p", "env", 0, since=0, pin=page.revisions[0].record_sha256, offset=0, limit=8
        )


@pytest.mark.parametrize("field", ["seq", "files_total"])
def test_retained_strict_bool_is_not_a_counter(store: Store, field: str):
    insert(store, operation())
    raw = json.loads(operation().model_dump_json())
    if field == "seq":
        raw["seq"] = True
    else:
        raw["files"] = False
    store._db.execute("UPDATE provisions SET record=?", (json.dumps(raw),))
    with pytest.raises(WorkerError):
        store.provision_observations.summary("p", "env", since=0)


def test_large_valid_selected_request_and_next_step_are_supported(store: Store):
    value = operation()
    request = ProvisionRequest(
        operation_id=value.request.operation_id,
        context=value.request.context,
        purpose="🧪" * 2000,
        pip=tuple("pkg" + str(i) + "==1" for i in range(32)),
        pip_reason="🧪" * 2000,
    )
    value = value.model_copy(update={"request": request, "next_step": "🧪" * 2000})
    insert(store, value)
    selected = store.provision_observations.detail("p", "env", "prv-one", since=0, seq=0)
    assert selected.request.purpose == request.purpose
    assert selected.next_step == value.next_step
    assert len(selected.model_dump_json().encode()) < MAX_OBSERVATION_BYTES


def test_selected_current_detail_is_explicit_and_pinned_reads_do_not_rebase(store: Store):
    value = operation(seq=7)
    insert(store, value)
    result = store.provision_observations.detail("p", "env", "prv-one", since=0, seq=None)
    assert result.summary.seq == 7
    store.update_provision(value.model_copy(update={"seq": 8, "state": ProvisionState.FINISHED}))
    with pytest.raises(WorkerError, match="sequence changed"):
        store.provision_observations.detail("p", "env", "prv-one", since=0, seq=7)
    current = store.provision_observations.detail("p", "env", "prv-one", since=0, seq=None)
    assert current.summary.seq == 8
    assert current.summary.state is ProvisionState.FINISHED
    assert store.provision_observations.summary("p", "env", since=0).queue_total == 0


def test_real_provision_hold_release_keeps_recorded_empty_ready_summary(store: Store) -> None:
    """Compose the real terminal producer with its observer, including a new current revision."""
    entry(store, 0)
    initial = store.provision_observations.summary("p", "env", since=0)
    assert initial.current_revision == 0
    assert initial.gate.mode == "ready"
    assert not initial.gate.recorded
    value = operation().model_copy(update={"revision_before": 0})
    insert(store, value)
    installing = value.model_copy(update={"state": ProvisionState.INSTALLING, "seq": 1})
    store.update_provision(installing)
    held = store.provision_observations.summary("p", "env", since=0)
    assert held.gate.mode == "hold"
    assert held.gate.generation == 1
    assert held.gate.operation == value.provision_id
    assert held.queue_total == 1
    entry(store, 1)
    store.update_provision(
        installing.model_copy(
            update={
                "state": ProvisionState.FINISHED,
                "outcome": ProvisionOutcome.COMPLETED,
                "revision_after": 1,
                "seq": 2,
            }
        )
    )
    assert store._db.execute(
        "SELECT typeof(operation),length(operation) FROM package_execution_gates "
        "WHERE environment_id='env'"
    ).fetchone() == ("text", 0)
    before = store._db.total_changes
    ready = store.provision_observations.summary("p", "env", since=0)
    assert ready.gate.mode == "ready"
    assert ready.gate.generation == 2
    assert ready.gate.recorded
    assert ready.gate.operation is None
    assert ready.gate.revision is None
    assert ready.current_revision == 1
    assert ready.queue_total == 0
    assert store._db.total_changes == before
    assert not store._db.in_transaction
    path = store._path
    # A new connection observes the same retained producer state; no backfill/repair.
    reopened = Store(path)
    try:
        observed = reopened.provision_observations.summary("p", "env", since=0)
        assert observed.gate == ready.gate
        assert observed.current_revision == ready.current_revision
        assert observed.queue_total == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("value", [None, b"", b"prv-one", 123, 1.5, "x" * 129, "🧪"])
def test_ready_operation_empty_text_does_not_admit_original_wrong_sql_types(
    store: Store, value: object
) -> None:
    # Deliberately relaxed isolated corrupt schema permits NULL/non-TEXT scalars.
    # Canonical production tables are NOT NULL/TEXT; do not change their schema.
    store._db.execute("ALTER TABLE package_execution_gates RENAME TO original_gate")
    store._db.execute(
        "CREATE TABLE package_execution_gates "
        "(environment_id, generation, mode, operation, revision, binding)"
    )
    store._db.execute(
        "INSERT INTO package_execution_gates VALUES ('env',2,'ready',?,NULL,'')", (value,)
    )
    before = store._db.total_changes
    with pytest.raises(WorkerError) as error:
        store.provision_observations.summary("p", "env", since=0)
    assert error.value.kind is ErrorKind.UNAVAILABLE
    assert store._db.total_changes == before
    assert not store._db.in_transaction


@pytest.mark.parametrize("blob", [False, True])
def test_ready_operation_large_corruption_refuses_without_materializing_value(
    store: Store, blob: bool
) -> None:
    value = b"x" * (20 * 1024 * 1024) if blob else "x" * (20 * 1024 * 1024)
    store._db.execute(
        "INSERT INTO package_execution_gates VALUES ('env',2,'ready',?,NULL,'')", (value,)
    )
    tracemalloc.start()
    try:
        with pytest.raises(WorkerError) as error:
            store.provision_observations.summary("p", "env", since=0)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert error.value.kind is ErrorKind.UNAVAILABLE
    assert peak < 128 * 1024
    assert not store._db.in_transaction
