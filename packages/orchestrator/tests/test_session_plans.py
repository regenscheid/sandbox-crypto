# SPDX-License-Identifier: MIT
"""Internal plan persistence/authority probes; no runtime planner or approval gate."""

from __future__ import annotations

import json
import sqlite3
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.store import DocumentStore
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import (
    MAX_COUNTER,
    GoalOutcome,
    PlanAuthority,
    PlanBody,
    PlanEvidence,
    PlanLimits,
    PlanOutput,
    PlanPhase,
    PlanScope,
    PlanStep,
    PlanTrack,
    SessionPlans,
)


def actor(role: Literal["user", "main", "delegated"] = "main", project="p", session="s", **fields):
    return PlanAuthority(
        scope=PlanScope(project_id=project, session_id=session),
        role=role,
        **{**({"user_id": "user-one"} if role == "user" else {"run_id": "run-one"}), **fields},
    )


def body(*steps, **changes):
    return PlanBody(
        **{
            "goal": "Establish the claim",
            "feasibility": "Inspect assumptions before deriving the result",
            "outputs": (
                PlanOutput(
                    title="Derivation",
                    description="A reproducible result",
                    evidence_standard="Check",
                ),
            ),
            "tracks": (
                PlanTrack(track_id="a", title="Analysis"),
                PlanTrack(track_id="b", title="Check"),
            ),
            "phases": (PlanPhase(phase_id="phase-one", title="Work", steps=steps or (step(),)),),
            **changes,
        }
    )


def step(identifier="one", *, title=None, description="Analyze", track="a"):
    return PlanStep(
        step_id=identifier,
        title=title or identifier.title(),
        description=description,
        evidence_standard="Retain reproducible evidence",
        track_id=track,
    )


def outcome(status: Literal["achieved", "partially_achieved", "not_achieved"] = "achieved"):
    return GoalOutcome(
        status=status,
        summary="Attributed assessment",
        evidence=(),
        remaining_work=() if status == "achieved" else ("Resolve the remaining limitation",),
    )


def open_sessions(state: Path) -> None:
    """The projects and sessions the tests plan for: a plan change needs its session
    recorded and open."""
    projects = ProjectStore(state)
    for project in ("p", "other", "third"):
        projects.create(project, "minimal", "sha256:" + "a" * 64)
        for session in ("s", "other"):
            projects.open_session(project, session, dedicated=False)
    projects.close()


@pytest.fixture
def store(tmp_path):
    open_sessions(tmp_path)
    documents = DocumentStore(tmp_path / "control.sqlite3")
    yield documents
    documents.close()


@pytest.fixture
def plans(store):
    return SessionPlans(store)


def made(plans, authority=None, value=None, operation="publish"):
    return plans.publish(authority or actor(), operation, value or body(), "Initial plan")


def update(
    plans,
    ref,
    *,
    identifier="one",
    cursor=0,
    status="completed",
    operation="progress",
    authority=None,
    **kwargs,
):
    return plans.progress(
        authority or actor(),
        operation,
        ref,
        identifier,
        cursor,
        status,
        "Attribution, not proof",
        **kwargs,
    )


def status(plans, ref, value, *, operation="status", authority=None, expected=None, result=None):
    current = plans.current(authority or actor())
    assert current
    return plans.set_status(
        authority or actor(),
        operation,
        ref,
        expected if expected is not None else current.head.status_revision,
        current.head.progress_cursor,
        value,
        "Workflow bookkeeping",
        outcome=result,
    )


def rows(store):
    with store.control.read() as tx:
        db = tx.sql
        return tuple(
            db.execute("SELECT kind,count(*) FROM session_plan_records GROUP BY kind")
        ), tuple(db.execute("SELECT kind,count(*) FROM session_plan_projections GROUP BY kind"))


def damage(store, table, kind, transform, *, identifier=None):
    """Corrupt only the disposable record under test, bypassing its immutable trigger."""
    with store.control.write() as tx:
        db = tx.sql
        if table == "session_plan_records":
            db.execute("DROP TRIGGER session_plan_records_no_update")
        assert table in {"session_plan_records", "session_plan_projections"}
        query = f"SELECT rowid,record FROM {table} WHERE kind=?"
        params = (kind,)
        if identifier is not None:
            query += " AND id=?"
            params += (identifier,)
        rowid, record = db.execute(query + " LIMIT 1", params).fetchone()
        value = transform(json.loads(record))
        changed = json.dumps(value, separators=(",", ":"))
        db.execute(
            f"UPDATE {table} SET record=?,bytes=? WHERE rowid=?",
            (changed, len(changed) + 512, rowid),
        )


def test_user_edit_stale_main_cas_and_attributed_history(plans):
    main = actor(alias="configured-model", resolved_model=None)
    first = made(plans, main)
    user = actor("user")
    second = plans.revise(user, "edit", first.ref, body(goal="USER refined the claim"), "USER edit")
    with pytest.raises(DocumentError, match="version changed"):
        plans.revise(main, "main-edit", first.ref, body(), "stale")
    third = plans.revise(main, "main-edit", second.ref, body(goal="Follow USER change"), "reread")
    history = plans.history(main, first.ref.plan_id, "version")
    assert [v.ref.version for v in history.items] == [1, 2, 3]
    assert [v.author.role for v in history.items] == ["main", "user", "main"]
    assert history.items[0].author.resolved_model is None
    assert plans.version(main, first.ref).body.goal == "Establish the claim"
    assert plans.summary(main).last_event.ref == third.ref


def test_progress_matches_stable_intent_but_changed_step_needs_new_assertion(plans):
    first = made(plans)
    update(plans, first.ref)
    same = plans.revise(actor(), "same", first.ref, body(goal="New explanation"), "unchanged step")
    assert plans.current(actor()).steps[0].matches_current_step
    changed = plans.revise(
        actor(), "change", same.ref, body(step(description="Altered work")), "changed intent"
    )
    snapshot = plans.current(actor())
    assert snapshot.steps[0].latest.ref == first.ref
    assert not snapshot.steps[0].matches_current_step
    with pytest.raises(DocumentError, match="open, changed"):
        status(plans, changed.ref, "completed", result=outcome())
    update(plans, changed.ref, cursor=1, operation="reaffirm")
    status(plans, changed.ref, "completed", result=outcome())


def test_removed_step_history_not_current_projection_and_track_moves(plans):
    first = made(plans, value=body(step(), step("two")))
    update(plans, first.ref)
    second = plans.revise(
        actor(), "remove", first.ref, body(step("two", track="b")), "remove old intent"
    )
    assert [v.step_id for v in plans.current(actor()).steps] == ["two"]
    assert (
        plans.history(actor(), first.ref.plan_id, "progress", step_id="one").items[0].step_id
        == "one"
    )
    delegated = actor("delegated", grant=second.ref, track_id="a")
    with pytest.raises(DocumentError, match="absent"):
        update(plans, second.ref, authority=delegated)
    with pytest.raises(DocumentError, match="grant"):
        update(plans, second.ref, identifier="two", authority=delegated)


def test_delegated_exact_track_version_and_no_body_status_authority(plans):
    first = made(plans)
    delegated = actor("delegated", grant=first.ref, track_id="a")
    update(plans, first.ref, authority=delegated)
    with pytest.raises(DocumentError, match="only update"):
        plans.revise(delegated, "edit", first.ref, body(), "denied")
    with pytest.raises(DocumentError, match="only update"):
        status(plans, first.ref, "active", authority=delegated)
    second = plans.revise(actor(), "move", first.ref, body(step(track="b")), "move track")
    with pytest.raises(DocumentError, match="version changed"):
        update(plans, first.ref, cursor=1, authority=delegated, operation="old")
    with pytest.raises(DocumentError, match="grant"):
        update(plans, second.ref, cursor=1, authority=delegated, operation="wrong-grant")


@pytest.mark.parametrize(
    "fields",
    [
        {"role": "user", "user_id": "u", "run_id": None, "alias": "model"},
        {"run_id": "USER"},
        {"user_id": "forged"},
        {"role": "delegated", "track_id": "a", "grant": None},
        {
            "scope": PlanScope(project_id="p", session_id="s").model_copy(
                update={"project_id": "bad/id"}
            )
        },
    ],
)
def test_copied_authority_cannot_bypass_positive_validation(plans, fields):
    invalid = actor().model_copy(update=fields)
    with pytest.raises(DocumentError, match="invalid plan input"):
        made(plans, invalid)
    assert plans.current(actor()) is None


def test_progress_status_revision_race_and_step_cas(plans):
    held_status = "draft"
    first = made(plans)
    status(plans, first.ref, "active", operation="active")
    status(plans, first.ref, held_status, operation="hold")
    with pytest.raises(DocumentError, match="status changed"):
        update(plans, first.ref, expected_status_revision=2)
    update(plans, first.ref, expected_status_revision=3)
    with pytest.raises(DocumentError, match="progress changed"):
        update(plans, first.ref, operation="stale")
    with pytest.raises(DocumentError, match="another plan mutation"):
        update(plans, first.ref, expected_status_revision=2)


def test_separate_connections_concurrent_cas_one_wins(store):
    plans = SessionPlans(store)
    first = made(plans)
    other = DocumentStore(store.path)
    second = SessionPlans(other)
    try:

        def change(pair):
            instance, operation = pair
            try:
                return instance.revise(
                    actor(), operation, first.ref, body(goal=operation), "concurrent"
                )
            except DocumentError as error:
                return error

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(change, ((plans, "one"), (second, "two"))))
        assert sum(isinstance(value, DocumentError) for value in results) == 1
        current = plans.current(actor())
        assert current is not None
        assert current.head.ref.version == 2
        assert len(plans.events(actor()).items) == 2
    finally:
        other.close()


@pytest.mark.parametrize(
    ("step_status", "goal_status", "allowed"),
    [
        ("pending", "not_achieved", False),
        ("in_progress", "partially_achieved", False),
        ("blocked", "achieved", False),
        ("skipped", "achieved", False),
        ("blocked", "not_achieved", True),
        ("skipped", "partially_achieved", True),
        ("completed", "achieved", True),
    ],
)
def test_terminal_workflow_accounting_is_distinct_from_goal_success(
    plans, step_status, goal_status, allowed
):
    first = made(plans)
    update(plans, first.ref, status=step_status)
    if not allowed:
        with pytest.raises(DocumentError, match="open, changed"):
            status(plans, first.ref, "completed", result=outcome(goal_status))
        assert plans.current(actor()).head.status == "draft"
    else:
        status(plans, first.ref, "completed", result=outcome(goal_status))
        assert plans.summary(actor()).goal_outcome == goal_status
        assert (
            plans.history(actor(), first.ref.plan_id, "status").items[-1].outcome.status
            == goal_status
        )


def test_completed_requires_explicit_outcome_and_abandonment_is_separate(plans):
    first = made(plans)
    with pytest.raises(ValidationError, match="explicit goal outcome"):
        status(plans, first.ref, "completed")
    status(plans, first.ref, "abandoned")
    assert plans.summary(actor()).goal_outcome is None
    with pytest.raises(DocumentError, match="terminal plans"):
        update(plans, first.ref)
    with pytest.raises(DocumentError, match="current plan changed"):
        made(plans, operation="next")
    second = plans.publish(actor(), "next", body(), "new attempt", expected_current=first.ref)
    assert second.ref.plan_id != first.ref.plan_id
    assert plans.history(actor(), first.ref.plan_id, "version").items[0].ref == first.ref


@pytest.mark.parametrize(
    ("status_tag", "remaining"), [("achieved", ("open",)), ("not_achieved", ())]
)
def test_outcome_remaining_work_is_not_optional_success_inference(status_tag, remaining):
    with pytest.raises(ValidationError):
        GoalOutcome(status=status_tag, summary="Assessment", evidence=(), remaining_work=remaining)


def test_active_only_stores_bookkeeping(plans):
    first = made(plans)
    status(plans, first.ref, "active", operation="record")
    assert plans.summary(actor()).status == "active"
    assert not hasattr(plans, "execution_allowed")


def test_replay_restart_and_historical_receipt_after_newer_head(store):
    plans = SessionPlans(store)
    first = made(plans)
    update(plans, first.ref)
    second = plans.revise(actor("user"), "edit", first.ref, body(goal="Updated"), "USER edit")
    before = rows(store)
    store.close()
    plans = SessionPlans(store)
    assert made(plans) == first
    current = plans.current(actor())
    assert current is not None
    assert current.head.ref == second.ref
    assert rows(store) == before
    with pytest.raises(DocumentError, match="another plan mutation"):
        made(plans, value=body(goal="Different"))


def test_operation_namespace_separates_roles_runs_and_scopes(plans):
    first = made(plans, operation="same")
    second = plans.revise(actor("user"), "same", first.ref, body(goal="Edit"), "USER")
    update(plans, second.ref, operation="same", authority=actor(run_id="run-two"))
    other = made(plans, actor(project="other"), operation="same")
    assert other.ref.plan_id != first.ref.plan_id
    assert plans.summary(actor()).event_cursor == 3
    assert plans.summary(actor(project="other")).event_cursor == 1


def test_scope_reads_and_session_local_cursor_have_no_foreign_holes(plans):
    first = made(plans)
    made(plans, actor(project="other"))
    made(plans, actor(session="other"))
    update(plans, first.ref)
    assert [e.sequence for e in plans.events(actor()).items] == [1, 2]
    for foreign in (actor(project="other"), actor(session="other")):
        with pytest.raises(DocumentError, match="not found"):
            plans.version(foreign, first.ref)
        with pytest.raises(DocumentError, match="not found"):
            plans.history(foreign, first.ref.plan_id, "progress")


def test_pinned_pages_ignore_later_mutations_and_keep_removed_history(plans):
    first = made(plans)
    for index in range(7):
        update(plans, first.ref, cursor=index, operation=f"progress-{index}")
    page = plans.history(actor(), first.ref.plan_id, "progress", limit=3)
    assert page.more
    assert page.after == 3
    assert page.target == 7
    update(plans, first.ref, cursor=7, operation="later")
    second = plans.history(
        actor(), first.ref.plan_id, "progress", after=page.after, target=page.target, limit=3
    )
    third = plans.history(
        actor(), first.ref.plan_id, "progress", after=second.after, target=page.target, limit=3
    )
    assert [v.sequence for v in page.items + second.items + third.items] == list(range(1, 8))
    assert not third.more
    assert third.after == 7


@pytest.mark.parametrize(
    "kwargs",
    [
        {"after": True},
        {"after": 1.0},
        {"after": MAX_COUNTER + 1},
        {"after": -1},
        {"after": 2, "target": 1},
        {"target": 2},
        {"limit": 0},
        {"limit": True},
        {"limit": 17},
    ],
)
def test_page_counters_and_bounds_are_exact(plans, kwargs):
    made(plans)
    with pytest.raises(DocumentError, match="invalid"):
        plans.events(actor(), **kwargs)


def test_auxiliary_rows_are_counted_and_failed_quota_mutation_rolls_back(store):
    plans = SessionPlans(store, limits=PlanLimits(project_records=6))
    first = made(plans)
    assert sum(n for _, n in rows(store)[0]) == 4
    assert sum(n for _, n in rows(store)[1]) == 2
    before = rows(store)
    with pytest.raises(DocumentError, match="quota"):
        update(plans, first.ref)
    assert rows(store) == before
    summary = plans.summary(actor())
    assert summary is not None
    assert summary.progress_cursor == 0
    # The quota belongs to plans; ordinary document writes still succeed.
    result = store.create(
        "p", name="notes", title="Notes", text="Safe", author=Author(kind="user", user="test-user")
    )
    assert result.revision == 1


def test_global_and_project_version_quotas_and_tightening(store):
    plans = SessionPlans(store, limits=PlanLimits(project_versions=1, total_versions=2))
    first = made(plans)
    made(plans, actor(project="other"))
    with pytest.raises(DocumentError, match="quota"):
        plans.revise(actor(), "next", first.ref, body(), "same project")
    with pytest.raises(DocumentError, match="quota"):
        made(plans, actor(project="third"))
    with pytest.raises(DocumentError, match="quota"):
        SessionPlans(store, limits=PlanLimits(total_versions=1))
    current = plans.current(actor())
    assert current is not None
    assert current.head.ref == first.ref


def test_progress_and_metadata_quotas_leave_prior_state(store):
    plans = SessionPlans(store, limits=PlanLimits(project_progress=1, metadata_bytes=100_000))
    first = made(plans)
    update(plans, first.ref)
    with pytest.raises(DocumentError, match="quota"):
        update(plans, first.ref, cursor=1, operation="next")
    summary = plans.summary(actor())
    assert summary is not None
    assert summary.progress_cursor == 1
    open_sessions(store.path.parent / "tiny")
    tiny = DocumentStore(store.path.parent / "tiny" / "control.sqlite3")
    try:
        capped = SessionPlans(tiny, limits=PlanLimits(metadata_bytes=1024))
        with pytest.raises(DocumentError, match="quota"):
            made(capped)
        assert capped.current(actor()) is None
    finally:
        tiny.close()


def test_immutable_records_and_commit_failure_leave_no_partial_plan(store):
    plans = SessionPlans(store)
    with store.control.write() as tx:
        db = tx.sql
        db.execute(
            "CREATE TRIGGER fail_plan_receipt BEFORE INSERT ON session_plan_records "
            "WHEN NEW.kind='request' BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="receipt failure"):
        made(plans)
    assert rows(store) == ((), ())
    with store.control.write() as tx:
        db = tx.sql
        db.execute("DROP TRIGGER fail_plan_receipt")
    made(plans)
    for verb in ("UPDATE session_plan_records SET key='x'", "DELETE FROM session_plan_records"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"), store.control.write() as tx:
            tx.sql.execute(verb)


@pytest.mark.parametrize(
    ("field", "value"), [("event_sequence", 999), ("progress_cursor", 1), ("status_revision", 2)]
)
def test_corrupt_replay_receipt_cannot_ack_another_event(store, plans, field, value):
    made(plans)
    damage(
        store,
        "session_plan_records",
        "request",
        lambda v: {**v, "receipt": {**v["receipt"], field: value}},
    )
    with pytest.raises(DocumentError, match=r"not found|receipt"):
        made(plans)


def test_corrupt_progress_fingerprint_cannot_certify_current_completion(store, plans):
    first = made(plans)
    update(plans, first.ref)
    damage(store, "session_plan_records", "progress", lambda v: {**v, "step_sha256": "a" * 64})
    with pytest.raises(DocumentError, match="does not bind"):
        status(plans, first.ref, "completed", result=outcome())


@pytest.mark.parametrize("value", ["x" * 20_000_000, "not json"])
def test_large_or_malformed_projection_is_refused_when_written(store, plans, value):
    made(plans)
    refused = pytest.raises(sqlite3.IntegrityError, match="CHECK")
    with refused, store.control.write() as tx:
        query = "UPDATE session_plan_projections SET record=? WHERE kind='current'"
        tx.sql.execute(query, (value,))
    assert plans.summary(actor()) is not None


@pytest.mark.parametrize(
    "transform",
    [
        lambda v: {**v, "progress_cursor": 1},
        lambda v: {**v, "status_revision": MAX_COUNTER},
        lambda v: {**v, "scope": {"project_id": "other", "session_id": "s"}},
    ],
)
def test_corrupt_head_refuses_without_mutation(store, plans, transform):
    first = made(plans)
    damage(store, "session_plan_projections", "head", transform)
    before = rows(store)
    with pytest.raises(DocumentError, match=r"incoherent|invalid|not found"):
        update(plans, first.ref)
    assert rows(store) == before


def test_evidence_is_same_project_claim_without_resource_lookup(plans):
    first = made(plans)
    claim = PlanEvidence(
        project_id="p", kind="artifact", reference_id="not-looked-up", revision=99, sha256="a" * 64
    )
    update(plans, first.ref, evidence=(claim,))
    assert plans.current(actor()).steps[0].latest.evidence == (claim,)
    forged = claim.model_copy(update={"project_id": "other"})
    with pytest.raises(DocumentError, match="belong to the project"):
        update(plans, first.ref, cursor=1, operation="foreign", evidence=(forged,))


@pytest.mark.parametrize(
    "change",
    [
        {"phases": (PlanPhase(phase_id="p", title="Work", steps=(step(), step(title="ONE"))),)},
        {"phases": (PlanPhase(phase_id="p", title="Work", steps=(step(track="missing"),)),)},
        {"goal": "bad\udcff"},
        {"goal": "\0"},
    ],
)
def test_copied_body_structure_and_invalid_encoding_are_revalidated(plans, change):
    value = body().model_copy(update=change)
    with pytest.raises(DocumentError, match="invalid plan input"):
        made(plans, value=value)
    assert plans.current(actor()) is None


def test_readonly_and_old_namespace_are_honest(tmp_path):
    path = tmp_path / "control.sqlite3"
    absent = DocumentStore(path, readonly=True)
    try:
        empty = SessionPlans(absent)
        assert empty.summary(actor()) is None
        assert empty.events(actor()).items == ()
        with pytest.raises(DocumentError, match="invalid"):
            empty.events(actor(), after=1)
    finally:
        absent.close()
    open_sessions(tmp_path)
    writer = DocumentStore(path)
    reader = DocumentStore(path, readonly=True)
    try:
        assert SessionPlans(reader).summary(actor()) is None
        plans = SessionPlans(writer)
        first = made(plans)
        read = SessionPlans(reader)
        assert read.version(actor(), first.ref).ref == first.ref
        with pytest.raises(DocumentError, match="read-only"):
            update(read, first.ref)
    finally:
        reader.close()
        writer.close()


def test_complete_page_byte_cap_and_full_current_bound(plans):
    first = made(plans)
    note = "😀" * 4096
    for index in range(7):
        plans.progress(actor(), f"long-{index}", first.ref, "one", index, "in_progress", note)
    page = plans.history(actor(), first.ref.plan_id, "progress")
    assert page.more
    assert 1 <= len(page.items) < 7
    assert sum(len(item.model_dump_json().encode()) for item in page.items) <= 262_144
    next_page = plans.history(
        actor(), first.ref.plan_id, "progress", after=page.after, target=page.target
    )
    assert [item.sequence for item in page.items + next_page.items] == list(range(1, 8))
    assert plans.current(actor()).steps[0].latest.note == note


def test_summary_checks_one_canonical_mutation_without_building_current(store, plans):
    first = made(plans)
    update(plans, first.ref)
    with store.control.read() as tx:
        db = tx.sql
        statements = []
        db.set_trace_callback(statements.append)
    try:
        summary = plans.summary(actor())
    finally:
        with store.control.read() as tx:
            db = tx.sql
            db.set_trace_callback(None)
    assert summary.progress_cursor == 1
    assert len(summary.model_dump_json().encode()) < 8192
    # Validate the latest immutable assertion's actor, but never walk current steps.
    payload_reads = [
        query
        for query in statements
        if query.startswith("SELECT record") and "kind='progress'" in query
    ]
    assert len(payload_reads) == 1
    assert not any(
        query.startswith("SELECT record") for query in statements if "kind='version'" in query
    )


def test_exact_text_and_reason_preserved_without_rendering(plans):
    raw = '# Goal\n"quote" \\command $x$ <script>data</script>'
    receipt = plans.publish(actor(), "raw", body(goal=raw), "  Exact reason  ")
    version = plans.version(actor(), receipt.ref)
    assert version.body.goal == raw
    assert version.reason == "  Exact reason  "


def test_the_user_keeps_any_text_but_an_agent_writes_none_hidden(plans):
    """Threat model A3: what an agent plans, the user reads and adopts by saving, and
    agents read again, so an agent's plan text holds no character the user may not see;
    the user's own text is kept as written."""
    raw = "# Goal\r\nthe user's own text, with a carriage return"
    receipt = plans.publish(actor("user"), "user-raw", body(goal=raw), "Mine")
    assert plans.version(actor("user"), receipt.ref).body.goal == raw
    for field in (
        {"goal": "run" + chr(0x200B) + "it"},
        {"goal": "a" + chr(0x0D) + "b"},
        {"feasibility": "evil &#x202E;gnp.exe"},
    ):
        with pytest.raises(DocumentError) as refused:
            plans.revise(actor(), "agent-hidden", receipt.ref, body(**field), "Revised")
        assert "may not see" in refused.value.message
        assert "the plan.body." in refused.value.message
    with pytest.raises(DocumentError) as reason:
        plans.revise(actor(), "agent-reason", receipt.ref, body(), "why" + chr(0x2066))
    assert "the plan.reason" in reason.value.message


@pytest.mark.parametrize("reason", [" ", "x" * 4097, "bad\0reason", "bad\udcff"])
def test_bad_mutation_reason_leaves_no_records(plans, reason):
    with pytest.raises(DocumentError, match=r"invalid|encoding"):
        plans.publish(actor(), "bad", body(), reason)
    assert plans.current(actor()) is None


def test_body_serialized_budget_applies_after_unicode_expansion(plans):
    value = body(*[step(f"step-{index}", description="😀" * 4096) for index in range(2)])
    with pytest.raises(DocumentError, match="byte limit"):
        made(plans, value=value)
    assert plans.current(actor()) is None


@pytest.mark.parametrize("operation", ["bad/id", "bad\udcff", "x" * 129, ""])
def test_operation_ids_cannot_escape_namespace(plans, operation):
    with pytest.raises(DocumentError, match="identifier"):
        made(plans, operation=operation)
    assert plans.current(actor()) is None


def test_same_scope_progress_other_step_cursor_and_status_cas(plans):
    first = made(plans, value=body(step(), step("two")))
    update(plans, first.ref)
    second = update(plans, first.ref, identifier="two", operation="two", cursor=0)
    assert second.progress_cursor == 2
    with pytest.raises(DocumentError, match="status/progress changed"):
        plans.set_status(
            actor(), "old-finish", first.ref, 1, 1, "completed", "stale", outcome=outcome()
        )
    status(plans, first.ref, "completed", result=outcome())
    assert plans.current(actor()).head.status == "completed"


def test_replay_cannot_substitute_valid_other_record(store, plans):
    first = made(plans)
    second = plans.revise(actor(), "edit", first.ref, body(goal="second"), "new version")
    damage(
        store,
        "session_plan_records",
        "event",
        lambda v: {**v, "number": 2, "ref": second.ref.model_dump(mode="json")},
    )
    with pytest.raises(DocumentError, match="receipt"):
        made(plans)


def test_wrong_accounting_is_refused_and_document_saves_continue(store, plans):
    made(plans)
    refused = pytest.raises(sqlite3.IntegrityError, match="CHECK")
    with refused, store.control.write() as tx:
        tx.sql.execute("UPDATE session_plan_projections SET bytes=0 WHERE kind='head'")
    SessionPlans(store)
    result = store.create(
        "p",
        name="still-works",
        title="Still works",
        text="Preserved",
        author=Author(kind="user", user="test-user"),
    )
    assert result.revision == 1


def test_valid_same_actor_receipt_substitution_cannot_rebind_request(store, plans):
    first = made(plans)
    original = update(plans, first.ref, status="in_progress", operation="first")
    replacement = update(plans, first.ref, cursor=1, operation="second")
    with store.control.write() as tx:
        db = tx.sql
        db.execute("DROP TRIGGER session_plan_records_no_update")
        for rowid, raw in db.execute(
            "SELECT rowid,record FROM session_plan_records WHERE kind='request'"
        ):
            value = json.loads(raw)
            if value["operation_id"] == "first":
                value["receipt"] = {**replacement.model_dump(mode="json"), "operation_id": "first"}
                changed = json.dumps(value)
                db.execute(
                    "UPDATE session_plan_records SET record=?,bytes=? WHERE rowid=?",
                    (changed, len(changed) + 512, rowid),
                )
                break
    with pytest.raises(DocumentError, match="does not bind"):
        update(plans, first.ref, status="in_progress", operation="first")
    assert original.event_sequence != replacement.event_sequence
    assert plans.current(actor()).head.progress_cursor == 2


def test_current_aggregate_refuses_before_materializing_all_large_progress(plans):
    first = made(plans, value=body(*[step(f"step-{index}") for index in range(44)]))
    note = "😀" * 4096
    for index in range(44):
        plans.progress(actor(), f"large-{index}", first.ref, f"step-{index}", 0, "completed", note)
    tracemalloc.start()
    try:
        with pytest.raises(DocumentError, match="current plan read limit"):
            plans.current(actor())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4_000_000
    assert plans.summary(actor()).progress_cursor == 44
    assert plans.history(actor(), first.ref.plan_id, "progress", limit=2).more


def test_summary_and_event_page_cannot_hide_user_edit_author(store, plans):
    made(plans, actor("user"))
    damage(
        store,
        "session_plan_records",
        "event",
        lambda v: {**v, "author": actor().model_dump(mode="json")},
    )
    for read in (lambda: plans.summary(actor()), lambda: plans.events(actor())):
        with pytest.raises(DocumentError, match="does not bind"):
            read()


def test_missing_scoped_event_is_unavailable_not_silent_covered_prefix(store, plans):
    first = made(plans)
    update(plans, first.ref)
    with store.control.write() as tx:
        db = tx.sql
        db.execute("DROP TRIGGER session_plan_records_no_delete")
        db.execute("DELETE FROM session_plan_records WHERE kind='event' AND number=1")
    with pytest.raises(DocumentError, match="history has a gap"):
        plans.events(actor())


def test_compact_current_keeps_all_steps_when_full_notes_exceed_current_limit(plans):
    first = made(plans, value=body(*[step(f"step-{index}") for index in range(44)]))
    claim = PlanEvidence(
        project_id="p", kind="execution", reference_id="exec-one", attempt_id="attempt-one"
    )
    for index in range(44):
        plans.progress(
            actor(),
            f"compact-{index}",
            first.ref,
            f"step-{index}",
            0,
            "completed",
            "😀" * 4096,
            evidence=(claim,),
        )
    with pytest.raises(DocumentError, match="current plan read limit"):
        plans.current(actor())
    compact = plans.compact_current(actor())
    assert compact.version.ref == first.ref
    assert len(compact.steps) == 44
    assert all(item.latest.status == "completed" for item in compact.steps)
    assert all(item.latest.evidence_count == 1 for item in compact.steps)
    assert all(item.matches_current_step for item in compact.steps)
    encoded = compact.model_dump_json()
    assert len(encoded.encode()) <= 262_144
    assert "note" not in compact.steps[0].latest.model_dump()
    assert "evidence" not in compact.steps[0].latest.model_dump()
    assert plans.compact_current(actor(project="other")) is None


def test_compact_metadata_cap_refuses_without_dropping_steps(plans):
    first = made(plans, value=body(*[step(f"step-{index}") for index in range(44)]))
    wide = actor(alias="😀" * 256, resolved_model="😀" * 256)
    for index in range(44):
        plans.progress(wide, f"wide-{index}", first.ref, f"step-{index}", 0, "completed", "Done")
    with pytest.raises(DocumentError, match="compact plan read limit"):
        plans.compact_current(actor())
    assert len(plans.current(actor()).steps) == 44
    assert plans.summary(actor()).progress_cursor == 44


@pytest.mark.parametrize("kind", ["version", "progress", "status"])
def test_unfiltered_history_cannot_silently_skip_missing_record(store, plans, kind):
    first = made(plans)
    if kind == "version":
        plans.revise(actor(), "edit", first.ref, body(goal="Changed"), "Edit")
    elif kind == "progress":
        update(plans, first.ref)
        update(plans, first.ref, cursor=1, operation="second")
    else:
        status(plans, first.ref, "active")
    with store.control.write() as tx:
        db = tx.sql
        db.execute("DROP TRIGGER session_plan_records_no_delete")
        db.execute("DELETE FROM session_plan_records WHERE kind=? AND number=1", (kind,))
    with pytest.raises(DocumentError, match="history has a gap"):
        plans.history(actor(), first.ref.plan_id, kind)


def test_a_plan_stored_with_the_removed_approval_status_reads_as_a_draft(store, plans):
    first = made(plans)
    status(plans, first.ref, "active", operation="active")
    # An older writer could record awaiting_approval, which no longer exists.
    old, new = '"status":"active"', '"status":"awaiting_approval"'
    with store.control.write() as tx:
        # History is immutable; an older writer's rows are made by lifting that briefly.
        triggers = tx.sql.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            ("session_plan_records",),
        ).fetchall()
        for name, _ in triggers:
            tx.sql.execute(f"DROP TRIGGER {name}")
        for table in ("session_plan_records", "session_plan_projections"):
            # One statement, so the row's byte accounting (a CHECK) holds throughout.
            tx.sql.execute(
                f"UPDATE {table} SET record=replace(record,?1,?2),"
                "bytes=length(CAST(replace(record,?1,?2) AS BLOB))+512",
                (old, new),
            )
        for _, sql in triggers:
            tx.sql.execute(sql)
        changed = tx.sql.execute(
            "SELECT count(*) FROM session_plan_projections WHERE record LIKE ?", (f"%{new}%",)
        ).fetchone()[0]
    assert changed
    # The plan stays readable, and as a draft it fences effects.
    assert plans.summary(actor()).status == "draft"
    assert plans.current(actor()).head.status == "draft"


def test_a_change_needs_its_session_open_and_a_pinned_change_its_opening(store, plans):
    projects = ProjectStore(store.path.parent)
    try:
        opening = projects.get("p").sessions["s"].kernel_notice_lifetime
        assert opening is not None
        pinned = plans.pinned(opening)
        first = made(pinned)
        projects.close_session("p", "s")
        for refused in (plans, pinned):
            with pytest.raises(DocumentError, match="session is closed") as closed:
                update(refused, first.ref, operation="closed")
            assert closed.value.kind == "conflict"
        projects.open_session("p", "s", dedicated=False)
        update(plans, first.ref, operation="reopened")  # the session is open again
        with pytest.raises(DocumentError, match="opened again"):
            update(pinned, first.ref, cursor=1, operation="old-opening")
        with pytest.raises(DocumentError, match="session is closed"):
            made(plans, actor(project="p", session="never"), operation="unrecorded")
    finally:
        projects.close()


def test_has_records_tells_a_damaged_plan_from_an_absent_one(store, plans):
    assert not plans.has_records(actor().scope)
    made(plans)
    with store.control.write() as tx:
        tx.sql.execute("DELETE FROM session_plan_projections WHERE kind='current'")
    assert plans.summary(actor()) is None
    assert plans.has_records(actor().scope)
    assert not plans.has_records(actor(session="other").scope)


def test_the_evidence_checks_keep_plans_and_their_accounting(tmp_path, monkeypatch):
    from sandbox_science.orchestrator.control import migrations

    with monkeypatch.context() as old:  # a store from before version 7
        old.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:5])
        old.setattr(migrations, "VERSION", migrations.BASELINE + 4)
        open_sessions(tmp_path)
        documents = DocumentStore(tmp_path / "control.sqlite3")
        plans = SessionPlans(documents)
        first = made(plans)
        update(plans, first.ref)
        before = (plans.current(actor()), plans.summary(actor()), rows(documents))
        documents.close()
    documents = DocumentStore(tmp_path / "control.sqlite3")
    documents.initialize()  # the writer brings the store to the current version
    try:
        plans = SessionPlans(documents)
        assert (plans.current(actor()), plans.summary(actor()), rows(documents)) == before
        update(plans, first.ref, cursor=1, operation="after")
        summary = plans.summary(actor())
        assert summary is not None
        assert summary.progress_cursor == 2
    finally:
        documents.close()
