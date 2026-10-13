# SPDX-License-Identifier: MIT
"""Temporary canonical stores and scripted host observations; no models/workers."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sandbox_science.orchestrator import child_attempts as module
from sandbox_science.orchestrator.child_attempts import (
    AppliedBudget,
    AttemptAuthority,
    Checkpoint,
    ChildAttempts,
    EffectPin,
    Observe,
    RecordedCall,
    RecordedMessage,
    RequestedProfile,
    Reserve,
    Seal,
    ToolSchema,
)
from sandbox_science.orchestrator.delegation_records import (
    DelegationBrief,
    DelegationRecords,
    DelegationScope,
    HandbackAuthority,
    HostObservation,
    ParentAuthority,
    ProfileIntent,
    RequestedLimits,
)
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.mathjobs import threaded
from sandbox_science.orchestrator.documents.store import DocumentStore
from sandbox_science.orchestrator.projects import ProjectStore


@pytest.fixture
def f(tmp_path):
    projects = ProjectStore(tmp_path / "state")
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("p", "s", dedicated=False)
    documents = DocumentStore(tmp_path / "state/control.sqlite3")
    records = DelegationRecords(documents, initialize=True)
    parent = ParentAuthority(
        scope=DelegationScope(
            project_id="p", session_id="s", environment_id=session.environment_id
        ),
        parent_run_id="parent",
    )
    prepared = records.prepare(
        parent,
        "prepare",
        DelegationBrief(
            task="Analyze the synthetic claim",
            context_summary="No model/worker effects",
            track=None,
            inputs=(),
            profile=ProfileIntent(
                profile_id="child-profile", profile_sha256="b" * 64, model_alias="explicit-child"
            ),
            limits=RequestedLimits(turns=2, wall_seconds=30, output_tokens=128),
        ),
    )
    authority = AttemptAuthority(
        scope=parent.scope,
        parent_run_id="parent",
        delegation_id=prepared.delegation_id,
        child_run_id=prepared.child_run_id,
        controller_id="owner",
    )
    now = {"value": datetime(2026, 10, 13, 12, tzinfo=UTC)}
    journal = ChildAttempts(
        documents, records, projects, initialize=True, clock=lambda: now["value"]
    )
    original = projects.session_snapshot("p", "s")
    messages = (
        RecordedMessage(role="system", content="Original synthetic child framing."),
        RecordedMessage(role="user", content="Inspect only the supplied claim."),
    )
    args = {
        "prepared": prepared,
        "original": original,
        "session": journal.pin_session(authority, original),
        "profile": RequestedProfile(
            profile_id="child-profile",
            profile_sha256="b" * 64,
            alias="explicit-child",
        ),
        "budget": AppliedBudget(
            max_calls=2,
            per_call_output_tokens=64,
            total_output_tokens=128,
            cumulative_input_bytes=65536,
            wall_seconds=30,
        ),
        "system": messages[0].content,
        "tools": (
            ToolSchema(
                name="plan_progress",
                description="Synthetic offered schema",
                parameters_json='{"type":"object"}',
            ),
        ),
        "messages": messages,
        "admitted": lambda: True,
    }
    value = SimpleNamespace(
        projects=projects,
        documents=documents,
        records=records,
        parent=parent,
        prepared=prepared,
        authority=authority,
        journal=journal,
        original=original,
        args=args,
        messages=messages,
        now=now,
    )
    yield value
    original.close()
    documents.close()


def begin(f, operation="begin", **updates):
    return f.journal.begin_attempt(f.authority, operation, **(f.args | updates))


def reserve(f, event, operation="call", **updates):
    body = Reserve(
        messages=f.journal.get(f.authority, event.attempt_id).messages, output_tokens=64
    ).model_copy(update=updates)
    return f.journal.reserve_turn(
        f.authority,
        operation,
        event.attempt_id,
        event.sequence,
        body,
        original=f.original,
        admitted=lambda: True,
    )


def returned(reservation, **updates):
    return Observe(
        reservation=reservation.sequence,
        outcome="returned",
        response=RecordedMessage(role="assistant", content="Partial synthetic observation"),
        resolved_model=None,
        finish_reason=None,
        input_tokens=None,
        output_tokens=None,
        reason=None,
    ).model_copy(update=updates)


def test_scripted_turn_exact_partial_reopen_and_separate_handback(f):
    started = begin(f)
    call = reserve(f, started)
    response = returned(
        call,
        response=RecordedMessage(
            role="assistant",
            content="Partial text",
            tool_calls=(
                RecordedCall(call_id="call-1", name="plan_progress", arguments="{malformed"),
            ),
        ),
    )
    observed = f.journal.observe_turn(
        f.authority, "observed", call.attempt_id, call.sequence, response
    )
    state = f.journal.get(f.authority, call.attempt_id)
    checkpoint = f.journal.checkpoint(
        f.authority,
        "partial",
        call.attempt_id,
        observed.sequence,
        Checkpoint(
            messages=(
                *state.messages,
                RecordedMessage(
                    role="tool", tool_call_id="call-1", content="Refused malformed input"
                ),
            ),
            held_effects=(),
        ),
    )
    ended = f.journal.seal_local_attempt(
        f.authority, "ended", call.attempt_id, checkpoint.sequence, Seal(ended="failed")
    )
    reader = DocumentStore(f.documents.path, readonly=True)
    try:
        journal = ChildAttempts(reader, DelegationRecords(reader), f.projects)
        final = journal.get(f.authority, call.attempt_id)
        assert final.sealed == ended
        assert final.messages[-2].tool_calls[0].arguments == "{malformed"
        assert final.begin.body == started.body
        assert final.reserved_output_tokens == 64
    finally:
        reader.close()
    assert f.records.get(f.parent, f.prepared.delegation_id).outcome is None
    assert not hasattr(f.journal, "dispatch")


def test_begin_replay_and_new_attempt_cannot_bypass_pending(f):
    started = begin(f)
    pending = reserve(f, started)
    assert begin(f) == started
    with pytest.raises(DocumentError, match="already has an attempt"):
        begin(f, operation="new-attempt")
    with pytest.raises(DocumentError, match="held or sealed"):
        reserve(f, pending, operation="retry")
    other = f.authority.model_copy(update={"controller_id": "another-owner"})
    with pytest.raises(DocumentError):
        f.journal.begin_attempt(other, "takeover", **f.args)


def test_unknown_charged_until_exact_original_return_and_no_refund(f):
    started = begin(f)
    call = reserve(f, started)
    unknown = f.journal.observe_turn(
        f.authority,
        "unknown",
        call.attempt_id,
        2,
        Observe(
            reservation=2,
            outcome="unknown",
            response=None,
            resolved_model=None,
            finish_reason=None,
            input_tokens=None,
            output_tokens=None,
            reason="timeout",
        ),
    )
    state = f.journal.get(f.authority, call.attempt_id)
    assert state.pending == call
    assert state.calls == 1
    assert state.reserved_output_tokens == 64
    with pytest.raises(DocumentError, match="held or sealed"):
        reserve(f, unknown, operation="implicit-retry")
    observation = f.journal.observe_turn(
        f.authority,
        "late-return",
        call.attempt_id,
        3,
        returned(call, resolved_model="actual-response-name", output_tokens=1),
    )
    reserve(f, observation, operation="new-turn")
    state = f.journal.get(f.authority, call.attempt_id)
    assert state.calls == 2
    assert state.reserved_output_tokens == 128  # reported usage does not refund reservations


def test_late_facts_after_close_and_handback_are_historical(f):
    started = begin(f)
    call = reserve(f, started)
    f.projects.close_session("p", "s")
    f.records.finish(
        HandbackAuthority(
            scope=f.parent.scope,
            parent_run_id="parent",
            delegation_id=f.prepared.delegation_id,
            child_run_id=f.prepared.child_run_id,
        ),
        "handback",
        1,
        HostObservation(ended="interrupted"),
        None,
    )
    ending = f.journal.seal_local_attempt(
        f.authority, "seal", call.attempt_id, 2, Seal(ended="interrupted")
    )
    late = f.journal.observe_turn(f.authority, "late", call.attempt_id, 3, returned(call))
    assert f.journal.get(f.authority, call.attempt_id).sealed == ending
    assert begin(f, admitted=lambda: False) == started
    assert f.journal.observe_turn(f.authority, "late", call.attempt_id, 3, returned(call)) == late
    with pytest.raises(DocumentError):
        reserve(f, late, operation="revive")
    assert f.records.get(f.parent, f.prepared.delegation_id).outcome is not None


def test_exact_payload_model_owner_and_prefix_replay(f):
    started = begin(f)
    call = reserve(f, started)
    observed = f.journal.observe_turn(f.authority, "response", call.attempt_id, 2, returned(call))
    assert (
        f.journal.observe_turn(f.authority, "response", call.attempt_id, 2, returned(call))
        == observed
    )
    with pytest.raises(DocumentError, match="another child attempt mutation"):
        f.journal.observe_turn(
            f.authority, "response", call.attempt_id, 2, returned(call, resolved_model="invented")
        )
    with pytest.raises(DocumentError, match="prefix changed"):
        reserve(f, observed, operation="rewritten", messages=f.messages)
    with pytest.raises(DocumentError):
        f.journal.get(f.authority.model_copy(update={"child_run_id": "wrong"}), call.attempt_id)


@pytest.mark.parametrize(("field", "value"), [("per_call_output_tokens", True), ("max_calls", 1.0)])
def test_copied_budget_literals_refuse(f, field, value):
    with pytest.raises(DocumentError):
        begin(f, budget=f.args["budget"].model_copy(update={field: value}))


def test_copied_extra_subclass_and_nested_expansion_before_encoder(f, monkeypatch):
    with pytest.raises(DocumentError):
        begin(
            f, profile=f.args["profile"].model_copy(update={f"extra-{i}": "x" for i in range(2048)})
        )
    calls = []
    original = json.dumps

    def encoding(*args, **kwargs):
        calls.append(type(args[0]) is dict and "system" in args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(json, "dumps", encoding)
    with pytest.raises(DocumentError):
        begin(f, system="\0" * module.MAX_RECORD)
    assert not any(calls)


@pytest.mark.parametrize("change", ["alias", "profile_sha256"])
def test_original_profile_intent_not_changed(f, change):
    with pytest.raises(DocumentError, match="profile intent"):
        begin(
            f,
            profile=f.args["profile"].model_copy(
                update={change: "changed" if change == "alias" else "c" * 64}
            ),
        )


@pytest.mark.parametrize("text", ['{"x":1,"x":2}', "[]"])
def test_tool_schema_duplicates_and_wrong_shape_refuse(f, text):
    with pytest.raises(DocumentError):
        begin(f, tools=(f.args["tools"][0].model_copy(update={"parameters_json": text}),))


def test_budget_and_wall_admission_are_explicit_no_call_effect(f):
    started = begin(f, budget=f.args["budget"].model_copy(update={"max_calls": 1}))
    call = reserve(f, started)
    observed = f.journal.observe_turn(f.authority, "returned", call.attempt_id, 2, returned(call))
    with pytest.raises(DocumentError, match="budget exhausted"):
        reserve(f, observed, operation="over-calls")
    assert f.journal.get(f.authority, call.attempt_id).cursor == 3


@pytest.mark.parametrize("delta", [-1, 31])
def test_wall_clock_backwards_or_expired_refuses(f, delta):
    started = begin(f)
    f.now["value"] += timedelta(seconds=delta)
    with pytest.raises(DocumentError, match="budget exhausted"):
        reserve(f, started)


def test_sticky_unknown_effects_cannot_be_removed_or_bypassed(f):
    started = begin(f)
    effect = EffectPin(
        operation_id="op", execution_id="ex", attempt_id="att", binding_sha256="d" * 64
    )
    checkpoint = f.journal.checkpoint(
        f.authority,
        "effect",
        started.attempt_id,
        1,
        Checkpoint(messages=f.messages, held_effects=(effect,)),
    )
    with pytest.raises(DocumentError, match="cannot be removed"):
        f.journal.checkpoint(
            f.authority,
            "drop-effect",
            started.attempt_id,
            2,
            Checkpoint(messages=f.messages, held_effects=()),
        )
    with pytest.raises(DocumentError, match="held or sealed"):
        reserve(f, checkpoint)
    with pytest.raises(DocumentError):
        begin(f, operation="new-attempt")


def test_stale_scope_closed_reopened_and_false_admission(f):
    started = begin(f)
    with pytest.raises(DocumentError, match="admission"):
        f.journal.reserve_turn(
            f.authority,
            "bad-admission",
            started.attempt_id,
            1,
            Reserve(messages=f.messages, output_tokens=64),
            original=f.original,
            admitted=lambda: 1,
        )
    f.projects.close_session("p", "s")
    f.projects.open_session("p", "s", dedicated=False)
    with pytest.raises(DocumentError, match="session"):
        reserve(f, started)
    assert f.journal.get(f.authority, started.attempt_id).cursor == 1


def test_inside_writer_admission_revocation_rolls_back(f, monkeypatch):
    started = begin(f)
    original = f.journal._budget

    def budget(db, project):
        original(db, project)
        allowed["value"] = False

    allowed = {"value": True}
    monkeypatch.setattr(f.journal, "_budget", budget)
    with pytest.raises(DocumentError, match="admission"):
        f.journal.reserve_turn(
            f.authority,
            "revoked",
            started.attempt_id,
            1,
            Reserve(messages=f.messages, output_tokens=64),
            original=f.original,
            admitted=lambda: allowed["value"],
        )
    assert f.journal.get(f.authority, started.attempt_id).cursor == 1


def test_actual_postcommit_unknown_replays_original(f, monkeypatch):
    started = begin(f)
    original = f.documents._write
    from contextlib import contextmanager

    @contextmanager
    def lost():
        with original() as db:
            yield db
        raise OSError("synthetic lost acknowledgement")

    monkeypatch.setattr(f.documents, "_write", lost)
    with pytest.raises(OSError, match="lost acknowledgement"):
        reserve(f, started)
    monkeypatch.setattr(f.documents, "_write", original)
    replay = reserve(f, started)
    assert replay.sequence == 2
    assert f.journal.get(f.authority, started.attempt_id).calls == 1


def test_attempt_quota_refusal_is_atomic(f, monkeypatch):
    started = begin(f)
    monkeypatch.setattr(module, "MAX_ATTEMPT", 1)
    with pytest.raises(DocumentError):
        reserve(f, started)
    monkeypatch.setattr(module, "MAX_ATTEMPT", 4 * 1024 * 1024)
    assert f.journal.get(f.authority, started.attempt_id).cursor == 1


def test_concurrent_reservations_one_winner(f):
    started = begin(f)

    def run(index):
        owner = DocumentStore(f.documents.path)
        try:
            journal = ChildAttempts(
                owner, DelegationRecords(owner), f.projects, clock=lambda: f.now["value"]
            )
            return journal.reserve_turn(
                f.authority,
                f"concurrent-{index}",
                started.attempt_id,
                1,
                Reserve(messages=f.messages, output_tokens=64),
                original=f.original,
                admitted=lambda: True,
            )
        except DocumentError:
            return None
        finally:
            owner.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(run, [1, 2]))
    assert sum(value is not None for value in values) == 1
    assert f.journal.get(f.authority, started.attempt_id).calls == 1


@pytest.mark.parametrize("column", ["operation", "request_sha", "record"])
def test_corrupt_large_sql_fields_refuse_before_materialization(f, column):
    started = begin(f)
    db = f.documents._db
    assert db is not None
    db.execute("DROP TRIGGER child_attempt_events_no_update")
    db.execute(f"UPDATE child_attempt_events SET {column}=?", ("x" * (20 * 1024 * 1024),))  # noqa: S608 - fixed parametrize columns
    import tracemalloc

    tracemalloc.start()
    try:
        with pytest.raises(DocumentError):
            f.journal.get(f.authority, started.attempt_id)
        assert tracemalloc.get_traced_memory()[1] < 2 * 1024 * 1024
    finally:
        tracemalloc.stop()


def test_retained_scalar_nul_duplicate_and_event_binding_refuse(f):
    started = begin(f)
    db = f.documents._db
    assert db is not None
    db.execute("DROP TRIGGER child_attempt_events_no_update")
    db.execute("UPDATE child_attempt_events SET operation=?", ("begin\0corrupt",))
    with pytest.raises(DocumentError):
        f.journal.get(f.authority, started.attempt_id)


def test_absent_partial_readonly_namespace_no_initialization(f, tmp_path):
    empty = DocumentStore(tmp_path / "other.sqlite3")
    try:
        records = DelegationRecords(empty, initialize=True)
        with pytest.raises(DocumentError, match="not initialized"):
            ChildAttempts(empty, records, f.projects)
        db = empty._db
        assert db is not None
        db.execute("CREATE TABLE child_attempt_meta(version INTEGER)")
        with pytest.raises(DocumentError, match="corrupt"):
            ChildAttempts(empty, records, f.projects, initialize=True)
    finally:
        empty.close()


@pytest.mark.asyncio
async def test_actual_thread_owner_close_waits_through_repeated_cancel(f, monkeypatch):
    started = begin(f)
    entered, release = threading.Event(), threading.Event()
    original = f.journal._budget

    def blocked(db, project):
        original(db, project)
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(f.journal, "_budget", blocked)
    task = asyncio.create_task(threaded(reserve, f, started))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel("first")
    await asyncio.sleep(0)
    task.cancel("second")
    close = asyncio.create_task(asyncio.to_thread(f.documents.close))
    await asyncio.sleep(0.02)
    assert not task.done()
    assert not close.done()
    os.fstat(f.original._descriptor)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await close
    assert f.documents._db is None


def test_precommit_canonical_root_replacement_refuses(f, monkeypatch, tmp_path):
    started = begin(f)
    original = f.projects.session_snapshot
    old = tmp_path / "old-projects"
    count = 0

    def changed(*args):
        nonlocal count
        snapshot = original(*args)
        count += 1
        if count == 2:
            f.projects.root.rename(old)
            f.projects.root.mkdir()
        return snapshot

    monkeypatch.setattr(f.projects, "session_snapshot", changed)
    try:
        with pytest.raises(DocumentError, match="session"):
            reserve(f, started)
    finally:
        if old.exists():
            f.projects.root.rmdir()
            old.rename(f.projects.root)
    assert f.journal.get(f.authority, started.attempt_id).cursor == 1


def test_late_facts_need_no_live_registry_path(f):
    started = begin(f)
    call = reserve(f, started)
    root = f.projects.root
    old = root.with_name("old-projects")
    root.rename(old)
    try:
        observed = f.journal.observe_turn(f.authority, "late", call.attempt_id, 2, returned(call))
        assert observed.sequence == 3
        assert begin(f, admitted=lambda: False) == started
    finally:
        old.rename(root)


def test_invalid_response_retention_stays_unknown_not_no_call(f):
    started = begin(f)
    call = reserve(f, started)
    invalid = returned(
        call,
        response=RecordedMessage(role="assistant", content="valid").model_copy(
            update={"content": "\ud800"}
        ),
    )
    with pytest.raises(DocumentError):
        f.journal.observe_turn(f.authority, "invalid-response", call.attempt_id, 2, invalid)
    assert f.journal.get(f.authority, call.attempt_id).pending == call
    unknown = f.journal.observe_turn(
        f.authority,
        "retention-failed",
        call.attempt_id,
        2,
        Observe(
            reservation=2,
            outcome="unknown",
            response=None,
            resolved_model=None,
            finish_reason=None,
            input_tokens=None,
            output_tokens=None,
            reason="retention_failed",
        ),
    )
    with pytest.raises(DocumentError, match="held or sealed"):
        reserve(f, unknown, operation="retry")


@pytest.mark.parametrize("change", ["duplicate", "missing-role", "changed-response"])
def test_retained_exact_bytes_request_binding_refuses(f, change):
    started = begin(f)
    db = f.documents._db
    assert db is not None
    db.execute("DROP TRIGGER child_attempt_events_no_update")
    row = db.execute("SELECT record FROM child_attempt_events").fetchone()
    assert row is not None
    raw = row[0]
    if change == "duplicate":
        raw = raw.replace('"version":1', '"version":1,"version":1')
    elif change == "missing-role":
        raw = raw.replace('"role":"delegated",', "")
    else:
        raw = raw.replace("Original synthetic child framing.", "Changed synthetic child framing.")
    db.execute("UPDATE child_attempt_events SET record=?", (raw,))
    db.execute("UPDATE child_attempt_events SET bytes=" + module._CHARGE)  # noqa: S608 - static charge expression
    with pytest.raises(DocumentError):
        f.journal.get(f.authority, started.attempt_id)


def test_nested_json_decoder_recursion_is_fixed_refusal(f):
    schema = '{"x":' * 1100 + "0" + "}" * 1100
    with pytest.raises(DocumentError, match="invalid child settings/tool schema"):
        begin(f, tools=(f.args["tools"][0].model_copy(update={"parameters_json": schema}),))


def test_extra_system_input_cannot_rewrite_original_role(f):
    started = begin(f)
    with pytest.raises(DocumentError, match="prefix changed"):
        reserve(
            f,
            started,
            messages=(*f.messages, RecordedMessage(role="system", content="Different role")),
        )


def test_retained_nested_decoder_recursion_is_fixed_refusal(f):
    started = begin(f)
    db = f.documents._db
    assert db is not None
    db.execute("DROP TRIGGER child_attempt_events_no_update")
    raw = '{"nested":' * 2000 + "0" + "}" * 2000
    db.execute("UPDATE child_attempt_events SET record=?", (raw,))
    db.execute("UPDATE child_attempt_events SET bytes=" + module._CHARGE)  # noqa: S608 - static expression
    with pytest.raises(DocumentError, match="identity is corrupt"):
        f.journal.get(f.authority, started.attempt_id)


@pytest.mark.parametrize("loss", ["suffix", "whole-child", "head", "all-child"])
def test_committed_loss_never_reseeds_or_repeats_unknown(f, loss):
    started = begin(f)
    reserve(f, started)
    db = f.documents._db
    assert db is not None
    if loss in {"suffix", "whole-child", "all-child"}:
        db.execute("DROP TRIGGER child_attempt_events_no_delete")
        if loss == "suffix":
            db.execute("DELETE FROM child_attempt_events WHERE sequence=2")
        else:
            db.execute("DELETE FROM child_attempt_events")
    if loss in {"head", "all-child"}:
        db.execute("DROP TRIGGER child_attempt_heads_no_delete")
        db.execute("DELETE FROM child_attempt_heads")
    with pytest.raises(DocumentError, match="missing or corrupt"):
        f.journal.get(f.authority, started.attempt_id)
    with pytest.raises(DocumentError):
        begin(f, operation="new-after-loss")
    with pytest.raises(DocumentError):
        reserve(f, started)


def test_committed_tail_anchor_catches_loss_even_if_count_repaired(f):
    started = begin(f)
    reserve(f, started)
    db = f.documents._db
    assert db is not None
    db.execute("DROP TRIGGER child_attempt_events_no_delete")
    db.execute("DELETE FROM child_attempt_events WHERE sequence=2")
    db.execute("DROP TRIGGER child_attempt_meta_step")
    db.execute("UPDATE child_attempt_meta SET event_count=1")
    with pytest.raises(DocumentError, match="committed tail"):
        f.journal.get(f.authority, started.attempt_id)
