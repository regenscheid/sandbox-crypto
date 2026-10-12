# SPDX-License-Identifier: MIT
"""Internal persistence tests; synthetic proved pins do not validate worker transport."""

from __future__ import annotations

import sqlite3
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox_science.orchestrator.kernel_notices import (
    KernelBirthPin,
    KernelClaim,
    KernelNoticeError,
    KernelNoticeLimits,
    KernelNotices,
    MainNoticeRun,
    NoticeScope,
    TrustedKernelNotice,
)


def actor(**scope: str) -> MainNoticeRun:
    return MainNoticeRun(
        scope=NoticeScope(
            **{
                "project_id": "p",
                "environment_id": "e",
                "session_id": "s",
                "session_lifetime": "opened-one",
                "worker_binding": "worker-config-one",
                "sandbox": "vm-one",
                "sandbox_id": "immutable-one",
                "incarnation": "inc-" + "1" * 24,
                **scope,
            }
        ),
        run_id="run-one",
        role="main",
    )


def observation(
    run: MainNoticeRun | None = None,
    *,
    revision: int = 2,
    generation: int = 1,
    start: int = 1,
    language: str = "python",
) -> TrustedKernelNotice:
    run = run or actor()
    return TrustedKernelNotice.model_validate(
        {
            "scope": run.scope,
            "birth": {
                **{
                    k: getattr(run.scope, k)
                    for k in (
                        "project_id",
                        "environment_id",
                        "session_id",
                        "sandbox",
                        "sandbox_id",
                        "incarnation",
                    )
                },
                "language": language,
                "generation": generation,
                "kernel_id": f"kernel-{language}-{generation}",
                "birth_id": "birth-" + f"{generation:024x}",
                "start_revision": start,
            },
            "generation": generation,
            "current_revision": revision,
            "observed_at": datetime(2026, 10, 12, tzinfo=UTC),
        }
    )


@pytest.fixture
def inbox(tmp_path: Path) -> KernelNotices:
    return KernelNotices(tmp_path / "control.sqlite3", clock=lambda: 100.0)


def claimed(inbox: KernelNotices, run: MainNoticeRun | None = None) -> KernelClaim:
    result = inbox.claim(run or actor())
    assert result is not None
    return result


def test_immutable_scope_and_observation_survive_restart_and_ack_compaction(
    inbox: KernelNotices,
) -> None:
    run, source = actor(), observation()
    first = inbox.record(run, source)
    assert first.state == "recorded"
    assert first.sequence == 1
    reopened = KernelNotices(inbox.path, clock=inbox.clock)
    claim = claimed(reopened)
    assert claim.notices[0].observation == source
    with (
        sqlite3.connect(inbox.path) as db,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        db.execute("UPDATE kernel_notice_items SET payload=x'00'")
    # Caller save-before-ACK is intentionally not wired/proven by this store.
    assert reopened.acknowledge(run, claim) is False
    assert reopened.acknowledge(run, claim) is True
    assert inbox.status(run).pending == 0
    assert inbox.status(run).acknowledged_through == first.sequence
    assert inbox.record(run, source).state == "duplicate"
    with sqlite3.connect(inbox.path) as db:
        assert db.execute("SELECT count(*) FROM kernel_notice_items").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM kernel_notice_watermarks").fetchone() == (1,)


def test_monotonic_prefix_and_historical_notice_after_new_generation(inbox: KernelNotices) -> None:
    run = actor()
    assert inbox.record(run, observation(revision=2)).sequence == 1
    assert inbox.record(run, observation(revision=3)).sequence == 2
    # A new current kernel is proved; old input cannot revive stale notices.
    assert inbox.record(run, observation(generation=2, revision=3, start=3)).state == "not_stale"
    claim = claimed(inbox)
    assert [n.sequence for n in claim.notices] == [1, 2]
    assert all(n.observation.birth.generation == 1 for n in claim.notices)
    inbox.acknowledge(run, claim)
    assert inbox.record(run, observation(revision=4)).state == "duplicate"
    assert inbox.record(run, observation(generation=2, revision=4, start=3)).sequence == 3


@pytest.mark.parametrize("reason", ["unknown_lineage", "unknown_revision"])
def test_unknown_holds_preserve_history_and_same_pin_recovery_does_not_revive_or_renew(
    inbox: KernelNotices, reason: str
) -> None:
    run, source = actor(), observation()
    inbox.record(run, source)
    claim = claimed(inbox)
    inbox.release(run, claim)
    inbox.hold(run, reason)  # type: ignore[arg-type]
    assert inbox.claim(run) is None
    assert inbox.status(run).pending == 1
    assert inbox.record(run, source).state == "duplicate"
    assert inbox.status(run).held is None
    recovered = claimed(inbox)
    assert recovered.pins == claim.pins
    assert recovered.token != claim.token
    inbox.record(run, source)
    assert inbox.renew(run, recovered).token == recovered.token
    inbox.acknowledge(run, recovered)
    inbox.hold(run, reason)  # type: ignore[arg-type]
    assert inbox.record(run, source).state == "duplicate"
    assert inbox.claim(run) is None
    assert inbox.status(run).pending == 0


@pytest.mark.parametrize("field", ["worker_binding", "sandbox", "sandbox_id", "incarnation"])
def test_binding_replacement_refuses_and_preserves_original(
    inbox: KernelNotices, field: str
) -> None:
    run = actor()
    inbox.record(run, observation())
    replacement = actor(**{field: "inc-" + "2" * 24 if field == "incarnation" else "replacement"})
    for operation in (
        lambda: inbox.record(replacement, observation(replacement)),
        lambda: inbox.hold(replacement, "unknown_lineage"),
        lambda: inbox.claim(replacement),
        lambda: inbox.status(replacement),
    ):
        with pytest.raises(KernelNoticeError, match="binding changed"):
            operation()
    assert inbox.status(run).pending == 1


@pytest.mark.parametrize(
    "field", ["project_id", "environment_id", "session_id", "session_lifetime"]
)
def test_foreign_scope_cannot_claim_ack_or_renew(inbox: KernelNotices, field: str) -> None:
    run = actor()
    inbox.record(run, observation())
    claim = claimed(inbox)
    foreign = actor(**{field: "foreign"})
    assert inbox.claim(foreign) is None
    for operation in (inbox.acknowledge, inbox.renew, inbox.release):
        with pytest.raises(KernelNoticeError, match="outside"):
            operation(foreign, claim)
    assert inbox.status(run).pending == 1


@pytest.mark.parametrize("role", ["user", "delegated", "reviewer", "", "MAIN"])
def test_positive_main_authority_even_after_model_copy(inbox: KernelNotices, role: str) -> None:
    forbidden = actor().model_copy(update={"role": role})
    for operation in (
        lambda: inbox.record(forbidden, observation()),
        lambda: inbox.hold(forbidden, "unknown_lineage"),
        lambda: inbox.claim(forbidden),
        lambda: inbox.status(forbidden),
    ):
        with pytest.raises(KernelNoticeError, match="trusted"):
            operation()


def test_unknown_or_incoherent_birth_cannot_be_constructed_or_admitted(
    inbox: KernelNotices,
) -> None:
    source = observation()
    for change in (
        {"birth": None},
        {"current_revision": None},
        {"generation": 0},
        {"current_revision": True},
        {"observed_at": "2026-10-12T00:00:00"},
    ):
        with pytest.raises(KernelNoticeError):
            inbox.record(actor(), source.model_copy(update=change))
    for change in ({"sandbox_id": "foreign"}, {"start_revision": 3}, {"generation": 2}):
        bad = source.model_copy(update={"birth": source.birth.model_copy(update=change)})
        with pytest.raises(KernelNoticeError):
            inbox.record(actor(), bad)
    with pytest.raises(ValidationError):
        KernelBirthPin.model_validate({**source.birth.model_dump(), "birth_id": "guessed"})


def test_competing_process_style_claims_have_exactly_one_owner(inbox: KernelNotices) -> None:
    inbox.record(actor(), observation())
    stores = [KernelNotices(inbox.path, clock=inbox.clock) for _ in range(2)]
    runs = [actor().model_copy(update={"run_id": f"run-{i}"}) for i in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda pair: pair[0].claim(pair[1]), zip(stores, runs, strict=True))
        )
    assert sum(result is not None for result in results) == 1
    claim = next(result for result in results if result is not None)
    owner = next(run for run in runs if run.run_id == claim.run_id)
    stores[0].release(owner, claim)
    assert stores[1].claim(runs[1]) is not None


def test_expired_claim_late_ack_and_replay_cannot_touch_later_owner(inbox: KernelNotices) -> None:
    now = [100.0]
    inbox.clock = lambda: now[0]
    run = actor()
    inbox.record(run, observation())
    old = claimed(inbox)
    now[0] = old.expires_at
    new_run = run.model_copy(update={"run_id": "next-run"})
    new = claimed(inbox, new_run)
    for operation in (inbox.renew, inbox.release, inbox.acknowledge):
        with pytest.raises(KernelNoticeError, match="displaced"):
            operation(run, old)
    inbox.acknowledge(new_run, new)
    assert inbox.acknowledge(new_run, new) is True
    inbox.record(new_run, observation(revision=3))
    later = claimed(inbox, new_run)
    assert inbox.acknowledge(new_run, new) is True
    assert inbox.status(run).pending == 1
    for operation in (inbox.renew, inbox.release):
        with pytest.raises(KernelNoticeError):
            operation(new_run, new)
    inbox.acknowledge(new_run, later)
    with pytest.raises(KernelNoticeError):
        inbox.acknowledge(new_run, new)  # displaced last receipt is explicitly unsupported


def test_ack_is_exact_whole_set_and_payload_not_a_subset(inbox: KernelNotices) -> None:
    run = actor()
    inbox.record(run, observation())
    inbox.record(run, observation(revision=3))
    claim = claimed(inbox)
    partial = claim.model_copy(update={"pins": claim.pins[:1], "notices": claim.notices[:1]})
    with pytest.raises(KernelNoticeError):
        inbox.acknowledge(run, partial)
    forged_notice = claim.notices[0].model_copy(update={"observation": observation(revision=4)})
    forged = claim.model_copy(update={"notices": (forged_notice, claim.notices[1])})
    with pytest.raises(KernelNoticeError):
        inbox.acknowledge(run, forged)
    assert inbox.status(run).pending == 2
    inbox.acknowledge(run, claim)


def test_quota_holds_do_not_advance_watermark_or_evict_and_can_drain(tmp_path: Path) -> None:
    inbox = KernelNotices(
        tmp_path / "bounded.sqlite3", limits=KernelNoticeLimits(notices=1, per_scope=1)
    )
    run = actor()
    first = inbox.record(run, observation())
    assert inbox.record(run, observation(revision=3)).reason == "capacity"
    assert inbox.status(run).pending == 1
    claim = claimed(inbox)
    assert claim.notices[0].sequence == first.sequence
    inbox.acknowledge(run, claim)
    assert inbox.record(run, observation(revision=3)).state == "recorded"
    assert inbox.status(run).held is None


def test_scope_capacity_has_one_bounded_global_gap_not_an_unbounded_queue(tmp_path: Path) -> None:
    inbox = KernelNotices(tmp_path / "scopes.sqlite3", limits=KernelNoticeLimits(scopes=1))
    run = actor()
    inbox.hold(run, "unknown_lineage")
    for i in range(100):
        foreign = actor(session_id=f"session-{i}")
        assert inbox.hold(foreign, "unknown_lineage").reason == "capacity"
    assert inbox.status(run).global_capacity_held
    with sqlite3.connect(inbox.path) as db:
        assert db.execute("SELECT count(*) FROM kernel_notice_scopes").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM kernel_notice_meta").fetchone() == (1,)


def test_byte_reservation_includes_holds_receipts_and_pins(tmp_path: Path) -> None:
    inbox = KernelNotices(tmp_path / "bytes.sqlite3")
    run = actor()
    inbox.record(run, observation())
    with inbox._db() as db:  # type: ignore[reportPrivateUsage]
        used = inbox._budget(db)  # type: ignore[reportPrivateUsage]
    inbox.limits = KernelNoticeLimits(bytes=max(4096, used))
    assert inbox.hold(run, "unknown_revision").state == "held"
    assert inbox.record(run, observation()).state == "duplicate"
    # Claim receipt bytes are admitted too, not allocated on top of a full budget.
    assert inbox.claim(run) is not None
    inbox.record(run, observation(revision=3))
    assert inbox.status(run).pending <= 2


def test_sql_failure_rolls_back_source_watermark_and_notice(tmp_path: Path) -> None:
    inbox = KernelNotices(tmp_path / "fault.sqlite3")
    with sqlite3.connect(inbox.path) as db:
        db.execute(
            "CREATE TRIGGER injected BEFORE INSERT ON kernel_notice_items"
            " BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        inbox.record(actor(), observation())
    with sqlite3.connect(inbox.path) as db:
        assert db.execute("SELECT count(*) FROM kernel_notice_scopes").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM kernel_notice_watermarks").fetchone() == (0,)
        db.execute("DROP TRIGGER injected")
    assert inbox.record(actor(), observation()).sequence == 1


def test_ack_failure_rolls_back_cursor_receipt_and_every_item(inbox: KernelNotices) -> None:
    run = actor()
    inbox.record(run, observation())
    inbox.record(run, observation(revision=3))
    claim = claimed(inbox)
    with sqlite3.connect(inbox.path) as db:
        db.execute(
            "CREATE TRIGGER injected BEFORE DELETE ON kernel_notice_items WHEN OLD.sequence=2"
            " BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        inbox.acknowledge(run, claim)
    assert inbox.status(run).pending == 2
    assert inbox.status(run).acknowledged_through == 0
    with sqlite3.connect(inbox.path) as db:
        db.execute("DROP TRIGGER injected")
    inbox.acknowledge(run, claim)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, 1e308])
def test_nonfinite_or_inexact_clock_refuses(inbox: KernelNotices, value: float) -> None:
    inbox.record(actor(), observation())
    inbox.clock = lambda: value
    with pytest.raises(KernelNoticeError, match="clock"):
        inbox.claim(actor())


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("kernel_notice_scopes", "payload"),
        ("kernel_notice_scopes", "lease"),
        ("kernel_notice_scopes", "last_ack"),
        ("kernel_notice_watermarks", "pin"),
        ("kernel_notice_items", "payload"),
        ("kernel_notice_items", "digest"),
    ],
)
def test_corrupt_twenty_mib_rows_refuse_before_python_materialization(
    inbox: KernelNotices, table: str, column: str
) -> None:
    run = actor()
    inbox.record(run, observation())
    inbox.limits = KernelNoticeLimits(bytes=64 * 1024 * 1024)
    with sqlite3.connect(inbox.path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("DROP TRIGGER kernel_notice_scope_pin_no_update")
        db.execute("DROP TRIGGER kernel_notice_items_no_update")
        data = b"x" * (20 * 1024 * 1024)
        db.execute(
            f"UPDATE {table} SET {column}=?",  # noqa: S608
            (data.decode() if column == "digest" else data,),
        )
        if column == "lease":
            db.execute("UPDATE kernel_notice_scopes SET until=200")
    tracemalloc.start()
    try:
        with pytest.raises(KernelNoticeError):
            inbox.record(
                run, observation(revision=3)
            ) if table == "kernel_notice_watermarks" else inbox.claim(run)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024


def test_infinite_retained_lease_does_not_silently_block_forever(inbox: KernelNotices) -> None:
    inbox.record(actor(), observation())
    claimed(inbox)
    with sqlite3.connect(inbox.path) as db:
        db.execute("UPDATE kernel_notice_scopes SET until=?", (float("inf"),))
    with pytest.raises(KernelNoticeError, match="lease"):
        inbox.claim(actor())


def test_sequences_are_scope_local_and_do_not_disclose_foreign_counts(inbox: KernelNotices) -> None:
    first, other = actor(), actor(session_id="other")
    assert inbox.record(first, observation()).sequence == 1
    assert inbox.record(other, observation(other)).sequence == 1
    assert inbox.record(other, observation(other, revision=3)).sequence == 2
    assert inbox.record(first, observation(revision=3)).sequence == 2
    assert [n.sequence for n in claimed(inbox).notices] == [1, 2]
    assert [n.sequence for n in claimed(inbox, other).notices] == [1, 2]


def test_complete_claim_bound_includes_pins_ids_and_large_counters(inbox: KernelNotices) -> None:
    run = actor().model_copy(update={"run_id": "r" * 128})
    with sqlite3.connect(inbox.path) as db:
        inbox.hold(run, "unknown_lineage")
        db.execute("UPDATE kernel_notice_scopes SET next_sequence=?", (2**61,))
    for revision in range(2, 18):
        inbox.record(run, observation(revision=revision))
    claim = claimed(inbox, run)
    assert 1 <= len(claim.notices) < 16
    assert len(claim.model_dump_json().encode()) <= inbox.limits.batch_bytes
    inbox.clock = lambda: 2**52 + 0.5
    # Large lease deadlines must remain bounded too; no wider envelope is silently emitted.
    with pytest.raises(KernelNoticeError, match="expired"):
        inbox.renew(run, claim)
    claim = claimed(inbox, run)
    assert len(claim.model_dump_json().encode()) <= inbox.limits.batch_bytes
    inbox.acknowledge(run, claim)
    assert inbox.status(run).pending > 0


def test_reserved_hold_and_actual_receipt_bytes_at_exact_full_budget(tmp_path: Path) -> None:
    inbox = KernelNotices(tmp_path / "exact.sqlite3")
    run = actor()
    for revision in range(2, 12):
        inbox.record(run, observation(revision=revision))
    with inbox._db() as db:  # type: ignore[reportPrivateUsage]
        used = inbox._budget(db)  # type: ignore[reportPrivateUsage]
    assert used > 4096
    inbox.limits = KernelNoticeLimits(bytes=used)
    inbox.hold(run, "unknown_revision")
    inbox.record(run, observation(revision=11))
    assert inbox.claim(run) is None  # no space for even an exact claim receipt
    assert inbox.status(run).pending == 10
    with inbox._db() as db:  # type: ignore[reportPrivateUsage]
        assert inbox._budget(db) == used  # type: ignore[reportPrivateUsage]
    inbox.limits = KernelNoticeLimits(bytes=used + 4096)
    claim = claimed(inbox)
    inbox.acknowledge(run, claim)
    with inbox._db() as db:  # type: ignore[reportPrivateUsage]
        assert inbox._budget(db) < used  # type: ignore[reportPrivateUsage]


def test_corrupt_foreign_hold_is_bounded_and_cannot_bypass_global_quota(
    inbox: KernelNotices,
) -> None:
    run, other = actor(), actor(session_id="other")
    inbox.record(run, observation())
    inbox.hold(other, "unknown_lineage")
    with sqlite3.connect(inbox.path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE kernel_notice_scopes SET held=? WHERE held IS NOT NULL", ("x" * 2**21,))
    tracemalloc.start()
    try:
        with pytest.raises(KernelNoticeError, match="hold"):
            inbox.claim(run)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024


def test_strict_limits_and_corrupt_per_scope_rows_refuse(inbox: KernelNotices) -> None:
    for change in ({"per_scope": True}, {"lease_seconds": float("inf")}, {"bytes": -1}):
        with pytest.raises(KernelNoticeError):
            KernelNotices(inbox.path, limits=KernelNoticeLimits().model_copy(update=change))
    inbox.record(actor(), observation())
    inbox.record(actor(), observation(revision=3))
    inbox.limits = KernelNoticeLimits(per_scope=1)
    with pytest.raises(KernelNoticeError, match="per-scope"):
        inbox.claim(actor())


@pytest.mark.parametrize("sequence", [0, 2])
def test_pending_sequence_must_bind_retained_scope_counter(
    inbox: KernelNotices,
    sequence: int,
) -> None:
    inbox.record(actor(), observation())
    with sqlite3.connect(inbox.path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("DROP TRIGGER kernel_notice_items_no_update")
        db.execute("UPDATE kernel_notice_items SET sequence=?", (sequence,))
    with pytest.raises(KernelNoticeError, match="sequence"):
        inbox.claim(actor())
