# SPDX-License-Identifier: MIT
"""Birth pins are observations, never a later-current-package or cross-scope inference."""

from __future__ import annotations

import sqlite3
import tracemalloc
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import KernelInfo, KernelListing
from sandbox_science.worker.kernel_revisions import KernelBirth, KernelStarts
from sandbox_science.worker.schema import prepare


def birth(**fields: object) -> KernelBirth:
    return KernelBirth.model_validate(
        {
            "project_id": "p1",
            "environment_id": "e1",
            "session_id": "s1",
            "language": "python",
            "incarnation": "inc-" + "a" * 24,
            "kernel_id": "krn-s1-python",
            "generation": 1,
            "birth_id": "birth-" + "a" * 24,
            "start_revision": 0,
            **fields,
        }
    )


def info(generation: int = 1) -> KernelInfo:
    return KernelInfo(kernel_id="krn-s1-python", language="python", generation=generation)


def test_birth_pin_survives_restart_and_cannot_be_rebased(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = sqlite3.connect(path)
    prepare(Path("kernels.sqlite3"), db)  # the store makes its tables
    starts = KernelStarts(db)
    assert starts.observe(birth(), info(), 0).revision_status == "current"
    db.commit()
    db.close()
    db = sqlite3.connect(path)
    prepare(Path("kernels.sqlite3"), db)  # the store makes its tables
    starts = KernelStarts(db)
    stale = starts.observe(birth(start_revision=5), info(), 5)
    assert (stale.start_revision, stale.current_revision, stale.revision_status) == (
        0,
        5,
        "predates",
    )
    db.close()


@pytest.mark.parametrize("change", [{"project_id": "other"}, {"birth_id": "birth-" + "b" * 24}])
def test_colliding_scope_or_birth_cannot_inherit_or_replace_a_pin(
    change: dict[str, object],
) -> None:
    db = sqlite3.connect(":memory:")
    prepare(Path("kernels.sqlite3"), db)  # the store makes its tables
    starts = KernelStarts(db)
    starts.observe(birth(), info(), 0)
    assert starts.observe(birth(**change), info(), 0).start_revision is None
    assert starts.observe(birth(), info(), 1).start_revision == 0
    db.close()


def test_first_reattached_observation_is_permanently_unknown_until_new_birth() -> None:
    db = sqlite3.connect(":memory:")
    prepare(Path("kernels.sqlite3"), db)  # the store makes its tables
    starts = KernelStarts(db)
    assert starts.observe(birth(start_revision=None), info(), 7).revision_status == "unknown"
    assert starts.observe(birth(start_revision=7), info(), 7).start_revision is None
    new = birth(generation=2, birth_id="birth-" + "b" * 24, start_revision=7)
    assert starts.observe(new, info(2), 7).start_revision == 7
    assert starts.observe(birth(), info(), 7).start_revision is None
    assert starts.observe(new, info(2), 8).start_revision == 7
    db.close()


def test_status_observation_does_not_freeze_a_provisional_unknown_birth() -> None:
    db = sqlite3.connect(":memory:")
    prepare(Path("kernels.sqlite3"), db)  # the store makes its tables
    starts = KernelStarts(db)
    assert (
        starts.observe(birth(start_revision=None), info(), 0, remember=False).start_revision is None
    )
    assert starts.observe(birth(), info(), 0).start_revision == 0
    db.close()


@pytest.mark.parametrize(
    "kernels",
    [
        [info(), info()],
        [KernelInfo(kernel_id="k", language="python", generation=1, current_revision=1)],
    ],
)
def test_kernel_listing_rejects_duplicate_or_incoherent_entries(kernels: list[KernelInfo]) -> None:
    with pytest.raises(ValidationError):
        KernelListing(environment_id="e1", session_id="s1", kernels=tuple(kernels))


@pytest.mark.parametrize("column", ["incarnation", "base", "last"])
def test_corrupt_legacy_generation_is_bounded_and_never_becomes_absent(
    tmp_path: Path, column: str
) -> None:
    from sandbox_science.worker.store import KernelRecord, Store

    store = Store(tmp_path / "worker.sqlite")
    try:
        store.save_kernel("e1", "s1", "python", KernelRecord("inc-" + "a" * 24, 0, 1))
        query = {
            "incarnation": "UPDATE kernels SET incarnation=?",
            "base": "UPDATE kernels SET base=?",
            "last": "UPDATE kernels SET last=?",
        }[column]
        store._db.execute(query, ("x" * 20_000_000,))
        tracemalloc.start()
        with pytest.raises(ValueError, match="retained kernel generation"):
            store.kernel("e1", "s1", "python")
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 100_000
    finally:
        store.close()


def test_corrupt_large_birth_record_is_bounded_and_unknown() -> None:
    db = sqlite3.connect(":memory:")
    prepare(Path("kernels.sqlite3"), db)  # the store makes its tables
    starts = KernelStarts(db)
    db.execute(
        "INSERT INTO kernel_start_revisions VALUES('e1','s1','python',?)", ("x" * 20_000_000,)
    )
    tracemalloc.start()
    assert starts.observe(birth(), info(), 0).revision_status == "unknown"
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 100_000
    db.close()


@pytest.mark.parametrize(
    "fields",
    [{"start_revision": True}, {"start_revision": 0.0}, {"birth_id": "x"}, {"generation": -1}],
)
def test_birth_metadata_is_strict(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        birth(**fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"start_revision": 1, "current_revision": 2},
        {"start_revision": 3, "current_revision": 2, "revision_status": "predates"},
        {"start_revision": True},
    ],
)
def test_public_kernel_comparison_refuses_incoherent_or_loose_fields(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        KernelInfo.model_validate(
            {"kernel_id": "k", "language": "python", "generation": 1, **fields}
        )


def test_epoch_refuses_delayed_birth_after_return_to_the_same_fingerprint(tmp_path: Path) -> None:
    from sandbox_science.worker.store import RevisionRecord, Store

    store = Store(tmp_path / "worker.sqlite")
    starts = store.kernel_starts
    incarnation = "inc-" + "a" * 24
    try:
        store.save_revision("e1", RevisionRecord(0, "a" * 32, ""))
        assert starts.epoch("e1", incarnation, 0, "a" * 32, remember=True) == 0
        store.save_revision("e1", RevisionRecord(1, "b" * 32, ""))
        assert starts.epoch("e1", incarnation, 1, "b" * 32, remember=True) == 1
        store.save_revision("e1", RevisionRecord(2, "a" * 32, ""))
        assert starts.epoch("e1", incarnation, 2, "a" * 32, remember=True) == 2
        assert starts.epoch("e1", incarnation, 0, "a" * 32) is None
        assert starts.epoch("e1", incarnation, 0, "a" * 32, remember=True) is None
        assert starts.epoch("e1", incarnation, 2, "a" * 32) == 2
        assert starts.epoch("e1", "inc-" + "b" * 24, 2, "a" * 32) is None
        assert starts.epoch("e1", incarnation, True, "a" * 32) is None
        assert starts.epoch("e1", incarnation, 2.0, "a" * 32) is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_managed_read_lock_and_owned_startup_survive_repeated_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import os

    from sandbox_science.worker import executor, provisioning

    async def send(_message: object) -> None:
        pass

    ex = executor.Executor(tmp_path, send)
    ex.package_mode = "ready"
    ex.reported = "a" * 32
    monkeypatch.setattr(
        provisioning,
        "observe",
        lambda _s, _p, _k, held: (os.close(held), provisioning.Observation(fingerprint="a" * 32))[
            1
        ],
    )
    running = executor.Running("exe-birth", "cell")
    kernel = ex.kernel("s1", "python")
    entered, release = asyncio.Event(), asyncio.Event()

    async def work() -> None:
        entered.set()
        await release.wait()

    start = asyncio.create_task(ex.start_kernel(running, kernel, 30, work))
    await entered.wait()
    assert ex.packages.try_lock(exclusive=True) is None
    start.cancel()
    await asyncio.sleep(0)
    start.cancel()
    await asyncio.sleep(0)
    assert not start.done()
    assert ex.packages.try_lock(exclusive=True) is None
    release.set()
    await start
    assert running.cancel_requested  # existing launch owner records cancel after actual work
    descriptor = ex.packages.try_lock(exclusive=True)
    assert descriptor is not None
    os.close(descriptor)
    assert kernel.start_environment == "a" * 32
    assert kernel.start_package_epoch == 0


@pytest.mark.parametrize("value", ["x" * 20_000_000, -1, 2**63 - 1])
def test_corrupt_current_revision_projection_is_bounded(tmp_path: Path, value: object) -> None:
    from sandbox_science.worker.store import Store

    store = Store(tmp_path / "worker.sqlite")
    try:
        store._db.execute(
            "INSERT INTO environment_revisions VALUES('e1',?,?, '')",
            (value, "a" * 32),
        )
        tracemalloc.start()
        assert store.kernel_starts.current("e1") is None
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 100_000
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ignored_interrupt_does_not_launch_replacement_through_recovery_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from queue import Empty
    from typing import Any

    from sandbox_science.worker import executor

    async def send(_message: object) -> None:
        pass

    class Manager:
        async def interrupt_kernel(self) -> None:
            pass

        async def is_alive(self) -> bool:
            return True

    class Client:
        async def get_iopub_msg(self, **_fields: Any) -> object:
            await asyncio.sleep(0.005)
            raise Empty

    ex = executor.Executor(tmp_path, send)
    ex.package_mode = "needs_recovery"
    kernel = ex.kernel("s1", "python")
    kernel.manager, kernel.client, kernel.generation = Manager(), Client(), 1
    stopped = False

    async def stop(_kernel: executor.Kernel) -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(executor.Kernel, "stop", stop)
    monkeypatch.setattr(executor, "INTERRUPT_GRACE", 0)
    state, result = await ex.execute(
        executor.Running("exe-ignored", "cell"), kernel, "", 0.001, 1024, {}, [], msg_id="synthetic"
    )
    assert state == "timed_out"
    assert stopped
    assert result["state_lost"] is True
    assert kernel.generation == 1
