# SPDX-License-Identifier: MIT
"""Passive lineage proves only exact original retained births and never wakes a sandbox."""

from __future__ import annotations

import asyncio
import time
import tracemalloc
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import (
    CellRequest,
    KernelLineageIdentity,
    KernelRestartRequest,
    KernelStatusRequest,
    SessionCloseRequest,
    kernel_lineage_sha256,
)
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import eventually
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.errors import WorkerError

from .harness import AUTH, CONTEXT, TOKEN, Harness, view
from .test_kernel_observation import REQUEST

pytestmark = pytest.mark.asyncio
BINDING = ("ss-test-original", "vm-test-original")


@pytest_asyncio.fixture
async def lineage(tmp_path: Path) -> AsyncIterator[Harness]:
    # Synthetic trusted managed identity; this fixture is not VM attestation.
    worker = await Harness.start(tmp_path, daemon=True, identity=BINDING)
    try:
        yield worker
    finally:
        # Restore the exact synthetic owner after deliberate binding damage.
        worker.manager.daemon_identity = lambda _: BINDING
        await worker.close()


async def test_exact_birth_and_stale_pin_are_readonly(lineage: Harness) -> None:
    manager = lineage.manager
    async with WorkerClient(
        str(lineage.client.make_url("")), TOKEN, session=lineage.client.session
    ) as client:
        with pytest.raises(WorkerError) as error:
            await manager.observe_kernel_lineage(REQUEST)
        assert error.value.kind == "unavailable"
        assert view(manager).connected() == set()
        await lineage.run("lineage-seed", kind="cell", code="lineage_value = 1729")
        manager.revisions.observe("env1", "b" * 32)
        manager.store.set_package_status("env1", "needs_recovery", "prv-synthetic", 0)
        environment = view(manager).connection("env1")
        environment.last_used = before = time.monotonic() - 60
        rows = list(manager.store._db.iterdump())
        observed = await manager.observe_kernel_lineage(REQUEST)
        item = observed.kernels[0]
        assert item.lineage_status == "proved"
        assert item.birth is not None
        assert (item.birth.sandbox, item.birth.sandbox_id) == BINDING
        assert (
            item.birth.start_revision,
            item.kernel.start_revision,
            observed.current_revision,
            item.kernel.revision_status,
        ) == (0, 0, 1, "predates")
        assert environment.last_used == before
        assert environment.requests == 0
        assert list(manager.store._db.iterdump()) == rows
        other = await manager.observe_kernel_lineage(
            KernelStatusRequest(context=REQUEST.context.model_copy(update={"session_id": "other"}))
        )
        assert other.kernels == ()
        # Existing GUI transport remains unchanged.
        plain = await client.observe_kernels(REQUEST.context)
        assert plain.kernels[0] == item.kernel
        assert "birth" not in plain.model_dump()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("missing", "missing_birth"),
        ("corrupt", "corrupt_birth"),
        ("project", "identity_mismatch"),
        ("birth", "identity_mismatch"),
        ("raw_generation", "identity_mismatch"),
        ("incarnation", "identity_mismatch"),
        ("binding", "binding_unavailable"),
        ("endpoint_incarnation", "binding_unavailable"),
        ("revision", "unknown_start_revision"),
        ("generation", "generation_unavailable"),
        ("managed_missing", "binding_unavailable"),
        ("managed_changed", "binding_unavailable"),
        ("unmanaged", "binding_unavailable"),
    ],
)
async def test_missing_or_changed_original_proof_stays_unknown(
    lineage: Harness,
    change: str,
    reason: str,
) -> None:
    await lineage.run("lineage-seed", kind="cell", code="lineage_value = 1729")
    manager = lineage.manager
    # Initial lockfile retention is unrelated owned background work.
    await eventually(lambda: not view(manager).fetching_lockfiles(), timeout=5)
    db = manager.store._db
    birth, _ = manager.store.kernel_starts.retained("env1", "s1", "python")
    assert birth is not None
    if change == "missing":
        db.execute("DELETE FROM kernel_start_revisions")
    elif change == "corrupt":
        db.execute("UPDATE kernel_start_revisions SET record='invalid'")
    elif change in {"binding", "endpoint_incarnation"}:
        manager.store.remember_endpoint(
            "env1",
            birth.incarnation if change == "binding" else "inc-" + "f" * 24,
            ("ss-other", "vm-other") if change == "binding" else BINDING,
        )
    elif change == "generation":
        db.execute("DELETE FROM kernels")
    elif change == "managed_missing":
        manager.daemon_identity = lambda _: None
    elif change == "managed_changed":
        manager.daemon_identity = lambda _: ("replacement", "replacement-id")
    elif change == "unmanaged":
        manager.daemon_identity = None
    else:
        field, value = {
            "project": ("project_id", "other"),
            "birth": ("birth_id", "birth-" + "f" * 24),
            "raw_generation": ("generation", 2),
            "incarnation": ("incarnation", "inc-" + "f" * 24),
            "revision": ("start_revision", None),
        }[change]
        changed = birth.model_copy(update={field: value})
        db.execute("UPDATE kernel_start_revisions SET record=?", (changed.model_dump_json(),))
    before = list(db.iterdump())
    observed = await manager.observe_kernel_lineage(REQUEST)
    assert observed.kernels[0].lineage_status == "unknown"
    assert observed.kernels[0].birth is None
    assert observed.kernels[0].unknown_reason == reason
    assert list(db.iterdump()) == before


async def test_same_daemon_restart_retains_birth_key_and_explicit_restart_changes_it(
    tmp_path: Path,
) -> None:
    worker = await Harness.start(tmp_path, daemon=True)
    try:
        execution, _ = await worker.manager.submit(
            CellRequest(operation_id="lineage-start", context=REQUEST.context, code="value = 1729")
        )
        await worker.manager.wait(execution.execution_id)
        first = (await worker.manager.observe_kernel_lineage(REQUEST)).kernels[0]
        assert first.lineage_status == "proved"
        assert first.birth is not None
        key = kernel_lineage_sha256(
            KernelLineageIdentity(birth=first.birth, generation=first.kernel.generation)
        )
        await worker.restart()
        reattached = (await worker.manager.observe_kernel_lineage(REQUEST)).kernels[0]
        assert reattached == first
        assert reattached.birth is not None
        assert (
            kernel_lineage_sha256(
                KernelLineageIdentity(
                    birth=reattached.birth, generation=reattached.kernel.generation
                )
            )
            == key
        )
        execution, _ = await worker.manager.submit(
            CellRequest(
                operation_id="lineage-retained", context=REQUEST.context, code="print(value)"
            )
        )
        ended = await worker.manager.wait(execution.execution_id)
        assert ended.result is not None
        assert ended.result.stdout is not None
        assert ended.result.stdout.text == "1729\n"
        await worker.manager.restart_kernel(KernelRestartRequest(context=REQUEST.context))
        fresh = (await worker.manager.observe_kernel_lineage(REQUEST)).kernels[0]
        assert fresh.lineage_status == "proved"
        assert fresh.birth is not None
        assert fresh.birth.start_revision == fresh.kernel.current_revision
        assert fresh.birth.generation > first.birth.generation
        assert (
            kernel_lineage_sha256(
                KernelLineageIdentity(birth=fresh.birth, generation=fresh.kernel.generation)
            )
            != key
        )
    finally:
        await worker.close()


async def test_known_original_birth_does_not_imply_a_current_revision(lineage: Harness) -> None:
    await lineage.run("lineage-start", kind="cell", code="value = 1729")
    lineage.manager.store._db.execute("DELETE FROM environment_revisions")
    before = list(lineage.manager.store._db.iterdump())
    observed = await lineage.manager.observe_kernel_lineage(REQUEST)
    assert observed.current_revision is None
    item = observed.kernels[0]
    assert item.lineage_status == "proved"
    assert item.birth is not None
    assert item.birth.start_revision == 0
    assert item.kernel.revision_status == "unknown"
    assert list(lineage.manager.store._db.iterdump()) == before


async def test_changed_trusted_vm_during_rpc_cannot_prove_original_birth(
    lineage: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await lineage.run("lineage-start", kind="cell", code="value = 1729")
    manager = lineage.manager
    request = ExecutorChannel.request

    async def rebound(channel: ExecutorChannel, op: str, **fields: Any) -> Message:
        reply = await request(channel, op, **fields)
        if op == "kernels":
            manager.daemon_identity = lambda _: ("replacement", "replacement-id")
        return reply

    monkeypatch.setattr(ExecutorChannel, "request", rebound)
    observed = await manager.observe_kernel_lineage(REQUEST)
    assert observed.kernels[0].lineage_status == "unknown"
    assert observed.kernels[0].unknown_reason == "binding_unavailable"


@pytest.mark.parametrize("column", ["incarnation", "sandbox", "sandbox_id", "birth"])
async def test_corrupt_huge_proof_has_bounded_reads(lineage: Harness, column: str) -> None:
    await lineage.run("lineage-seed", kind="cell", code="lineage_value = 1729")
    manager = lineage.manager
    channel = view(manager).channel("env1")
    reply = await channel.request("kernels", session="s1")
    db = manager.store._db
    query = (
        "UPDATE kernel_start_revisions SET record=?"
        if column == "birth"
        else {
            "incarnation": "UPDATE executor_endpoints SET incarnation=?",
            "sandbox": "UPDATE executor_endpoints SET sandbox=?",
            "sandbox_id": "UPDATE executor_endpoints SET sandbox_id=?",
        }[column]
    )
    db.execute(query, ("x" * 20_000_000,))
    tracemalloc.start()
    try:
        # Isolate the synchronous SQL/mapping boundary from asyncio pipe buffers.
        # HTTP composition and the shared transport budget are exercised separately.
        observed = view(manager).kernel_lineage(REQUEST.context, channel, reply["kernels"])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert observed.kernels[0].lineage_status == "unknown"
    assert peak < 150_000


async def test_lineage_shares_observer_admission_and_completed_close_token(
    lineage: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await lineage.run("lineage-seed", kind="cell", code="lineage_value = 1729")
    manager = lineage.manager
    started, release = asyncio.Event(), asyncio.Event()
    request = ExecutorChannel.request

    async def delayed(channel: ExecutorChannel, op: str, **fields: Any) -> Message:
        if op == "kernels":
            reply = await request(channel, op, **fields)
            started.set()
            await release.wait()
            return reply
        return await request(channel, op, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(ExecutorChannel, "request", delayed)
        pending = asyncio.create_task(manager.observe_kernel_lineage(REQUEST))
        try:
            await started.wait()
            with pytest.raises(WorkerError, match="capacity"):
                await manager.observe_kernels(REQUEST)
            await manager.close_session(SessionCloseRequest(context=REQUEST.context))
            release.set()
            with pytest.raises(WorkerError, match="connection changed"):
                await pending
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
    assert view(manager).observations() == {}
    assert (await manager.observe_kernel_lineage(REQUEST)).kernels == ()


async def test_lineage_route_auth_and_project_scope_do_not_dispatch(lineage: Harness) -> None:
    response = await lineage.client.post("/v1/kernels/lineage", json={"context": CONTEXT})
    assert response.status == 401
    # The ordinary fixture client has a project-scoped token as well as the pool token.
    response = await lineage.client.post(
        "/v1/kernels/lineage", json={"context": {**CONTEXT, "project_id": "other"}}, headers=AUTH
    )
    assert response.status in {404, 503}  # pool token may inspect only an existing connection
    assert lineage.view.connected() == set()


async def test_lineage_refuses_duplicate_live_kernel_ids_as_unavailable(
    lineage: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await lineage.run("lineage-open", kind="shell", script="true")
    original = ExecutorChannel.request

    async def duplicate(channel: ExecutorChannel, op: str, **fields: Any) -> Message:
        if op == "kernels":
            return {
                "kernels": [
                    {"kernel_id": "same", "language": language, "generation": 1}
                    for language in ("python", "sage")
                ]
            }
        return await original(channel, op, **fields)

    monkeypatch.setattr(ExecutorChannel, "request", duplicate)
    with pytest.raises(WorkerError) as error:
        await lineage.manager.observe_kernel_lineage(REQUEST)
    assert error.value.kind == "unavailable"
    assert lineage.view.observations() == {}
