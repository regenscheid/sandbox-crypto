# SPDX-License-Identifier: MIT
"""The run service's records in the control store (run-service spec RS-4, RS-5, RS-13)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.control import ControlError, migrations, store_path
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.runstate import ChatMessage, RunState
from sandbox_science.testing import FakeClock

from .control_versions import at_version, before


def store(tmp_path: Path, clock: FakeClock | None = None) -> tuple[ProjectStore, RunState]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    if clock is None:
        return projects, RunState(projects.control)
    return projects, RunState(projects.control, clock=clock)


def test_a_message_is_queued_once_and_marked_delivered(tmp_path: Path) -> None:
    projects, state = store(tmp_path)
    first = state.post("p", "s", "m-1", "What is $x$?", author="user")
    assert isinstance(first, ChatMessage)  # no question waits for an answer
    assert state.post("p", "s", "m-1", "What is $x$?", author="user") == first
    with pytest.raises(ControlError) as conflict:
        state.post("p", "s", "m-1", "Something else", author="user")
    assert conflict.value.kind == "conflict"
    second = state.post("p", "s", "m-2", "And $y$?", author="user")
    assert [m.message_id for m in state.messages("p", "s", state="queued")] == ["m-1", "m-2"]
    assert state.messages("p", "s", after=first.number) == [second]
    state.deliver("p", "s", ["m-1"], "20261014T000000000000Z-run-1")
    (delivered, queued) = state.messages("p", "s")
    assert (delivered.state, delivered.run) == ("delivered", "20261014T000000000000Z-run-1")
    assert queued.state == "queued"
    projects.close()


@pytest.mark.parametrize("text", ["", "   ", "x" * 65537])
def test_a_blank_or_long_message_is_refused(tmp_path: Path, text: str) -> None:
    projects, state = store(tmp_path)
    with pytest.raises(ControlError) as refused:
        state.post("p", "s", "m-1", text, author="user")
    assert refused.value.kind == "invalid_request"
    projects.close()


def test_a_message_to_an_unknown_session_is_not_found(tmp_path: Path) -> None:
    projects, state = store(tmp_path)
    with pytest.raises(ControlError) as missing:
        state.post("p", "other", "m-1", "Hello", author="user")
    assert missing.value.kind == "not_found"
    projects.close()


def test_one_run_holds_a_session_until_its_lease_expires(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 14, tzinfo=UTC))
    projects, state = store(tmp_path, clock)
    state.acquire("p", "s", "run-a", "holder-a")
    with pytest.raises(ControlError) as busy:
        state.acquire("p", "s", "run-b", "holder-b")
    assert (busy.value.kind, busy.value.details) == ("busy", {"run": "run-a"})
    lease = state.lease("p", "s")
    assert lease is not None
    assert lease.run == "run-a"
    clock.advance(59)
    state.renew("p", "s", "holder-a")
    clock.advance(59)
    with pytest.raises(ControlError):
        state.acquire("p", "s", "run-b", "holder-b")
    clock.advance(1)  # 60 s without a renewal: the holder died
    assert state.lease("p", "s") is None
    state.acquire("p", "s", "run-b", "holder-b")
    with pytest.raises(ControlError) as lost:
        state.renew("p", "s", "holder-a")
    assert lost.value.kind == "conflict"
    state.release("p", "s", "holder-a")  # not its lease any more: nothing happens
    assert state.lease("p", "s") is not None
    state.release("p", "s", "holder-b")
    assert state.lease("p", "s") is None
    projects.close()


def test_events_are_numbered_and_never_changed(tmp_path: Path) -> None:
    projects, state = store(tmp_path)
    assert state.append("p", "s", "run-1", "started", {"run_id": "run-1"}) == 1
    assert state.append("p", "s", "run-1", "model", {"turn": 1}) == 2
    assert state.append("p", "s", "run-2", "model", {"turn": 1}) == 1
    assert [e.seq for e in state.events("p", "s", "run-1", after=1)] == [2]
    assert not state.ended("p", "s", "run-1")
    state.append("p", "s", "run-1", "stopped", {"stopped": "answered"})
    assert state.ended("p", "s", "run-1")
    with pytest.raises(ControlError):
        state.append("p", "s", "run-1", "model", {"text": "x" * 70000})
    projects.close()
    with sqlite3.connect(store_path(tmp_path)) as db:
        for statement in ("UPDATE run_events SET data = '{}'", "DELETE FROM run_events"):
            with pytest.raises(sqlite3.IntegrityError, match="written once"):
                db.execute(statement)


def test_a_run_offers_delivers_and_writes_only_while_it_holds_the_session(
    tmp_path: Path,
) -> None:
    projects, state = store(tmp_path)
    state.post("p", "s", "m-1", "Hello.", author="user")
    for refused in (
        lambda: state.offer("p", "s", ["m-1"], "run-a"),
        lambda: state.deliver("p", "s", ["m-1"], "run-a", holding=True),
        lambda: state.append("p", "s", "run-a", "model", {"turn": 1}, holding=True),
    ):
        with pytest.raises(ControlError) as lost:
            refused()
        assert lost.value.kind == "conflict"
    assert not state.known("p", "s", "run-a")
    state.acquire("p", "s", "run-a", "holder-a")
    assert state.known("p", "s", "run-a")
    assert state.messages("p", "s")[0].offered is None
    state.offer("p", "s", ["m-1"], "run-a")
    # The run that saw it, named even while it is still queued, or once it is held.
    assert state.messages("p", "s")[0].offered == "run-a"
    assert state.append("p", "s", "run-a", "model", {"turn": 1}, holding=True) == 1
    state.deliver("p", "s", ["m-1"], "run-a", holding=True)
    assert [m.state for m in state.messages("p", "s")] == ["delivered"]
    state.release("p", "s", "holder-a")
    assert state.known("p", "s", "run-a")  # its events remain
    projects.close()


def test_held_and_refused_messages(tmp_path: Path) -> None:
    projects, state = store(tmp_path)
    for number in (1, 2, 3):
        state.post("p", "s", f"m-{number}", f"Message {number}.", author="user")
    state.hold("p", "s", ["m-1"])
    state.refuse("p", "s", ["m-2"], "budget")
    assert state.waiting() == [("p", "s")]
    assert [(m.state, m.reason) for m in state.messages("p", "s")] == [
        ("held", None),
        ("refused", "budget"),
        ("queued", None),
    ]
    assert state.resume("p", "s") == 1
    assert [m.message_id for m in state.messages("p", "s", state="queued")] == ["m-1", "m-3"]
    projects.close()


def test_a_projects_daily_cost_limit_is_an_optional_setting(tmp_path: Path) -> None:
    projects, state = store(tmp_path)
    now = datetime.now(UTC)
    assert state.cost_limit("p") is None  # none by default: nothing is kept
    assert not state.over_cost_limit("p", now)
    state.set_cost_limit("p", 2.5)
    assert state.cost_limit("p") == 2.5
    assert not state.over_cost_limit("p", now)  # the gateway priced nothing yet
    run = run_name(now, "priced")
    state.append("p", "s", run, "assistant", {"usage": {"input_tokens": 9, "cost": 2.5}})
    assert state.over_cost_limit("p", now)
    assert not state.over_cost_limit("p", now + timedelta(hours=25))  # out of the window
    state.set_cost_limit("p", None)
    assert (state.cost_limit("p"), state.over_cost_limit("p", now)) == (None, False)
    for wrong in (-1.0, 2e9):
        with pytest.raises(ControlError):
            state.set_cost_limit("p", wrong)
    with pytest.raises(ControlError):
        state.set_cost_limit("nope", 1)
    projects.close()


def test_dead_leases_and_the_services_heartbeat(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 14, tzinfo=UTC))
    projects, state = store(tmp_path, clock)
    state.acquire("p", "s", "run-a", "holder-a")
    state.beat("service")
    assert state.dead() == []
    assert state.service_running()
    clock.advance(60)
    assert [lease.run for lease in state.dead()] == ["run-a"]
    assert not state.service_running()
    projects.close()


def test_the_cli_shows_sets_and_removes_the_cost_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store(tmp_path)[0].close()
    argv = ["project", "budget", "--state-dir", str(tmp_path), "--project", "p"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == "project p: no daily cost limit, in the gateway's currency\n"
    assert cli.main([*argv, "--daily-cost-limit", "12.5"]) == 0
    assert capsys.readouterr().out == (
        "project p: a daily cost limit of 12.5, in the gateway's currency\n"
    )
    assert cli.main([*argv, "--no-cost-limit"]) == 0
    assert capsys.readouterr().out.startswith("project p: no daily cost limit")
    assert cli.main([*argv, "--daily-cost-limit", "-1"]) == 1
    assert "a daily cost limit is 0 to" in capsys.readouterr().err


def test_a_version_35_store_drops_its_token_budgets(tmp_path: Path) -> None:
    """The token budgets go with their table; the cost limits start empty: none is kept."""
    path = store_path(tmp_path)
    with at_version(35):
        projects = ProjectStore(tmp_path)
        projects.create("p", "minimal", "sha256:" + "a" * 64)
        projects.close()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO project_budgets VALUES ('p', 5000, '2026-10-15T00:00:00+00:00')")
    migrated = ProjectStore(tmp_path)
    try:
        assert RunState(migrated.control).cost_limit("p") is None
    finally:
        migrated.close()
    with sqlite3.connect(path) as db:
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master")}
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
    assert "project_budgets" not in tables
    assert "project_cost_limits" in tables


def test_a_version_39_stores_reviewer_and_watcher_calls_get_no_cost(tmp_path: Path) -> None:
    """Version 40 keeps each reviewer and watcher call's cost (lab 40, B5); the calls made
    before it have none, and the project's cost counts nothing for them."""
    path = store_path(tmp_path)
    with at_version(39):
        projects = ProjectStore(tmp_path)
        projects.create("p", "minimal", "sha256:" + "a" * 64)
        projects.close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO review_calls(project,review_id,created,estimated_tokens) "
            "VALUES ('p','rev-1',1000,100)"
        )
        db.execute("INSERT INTO job_watcher_calls(project,requested_at) VALUES ('p',1000)")
    migrated = ProjectStore(tmp_path)
    try:
        assert RunState(migrated).model_cost("p") is None
    finally:
        migrated.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
        assert db.execute("SELECT cost FROM review_calls").fetchall() == [(None,)]
        assert db.execute("SELECT cost FROM job_watcher_calls").fetchall() == [(None,)]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE job_watcher_calls SET cost=-1")


def test_older_stores_role_calls_get_no_cost_source(tmp_path: Path) -> None:
    """Version 53 keeps where each reviewer, job watcher and import pass call's cost came
    from (lab 43, B2); the calls made before it have none, and their costs still count."""
    path = store_path(tmp_path)
    with at_version(before(migrations._cost_sources)):
        projects = ProjectStore(tmp_path)
        projects.create("p", "minimal", "sha256:" + "a" * 64)
        projects.close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO review_calls(project,review_id,created,estimated_tokens,cost) "
            "VALUES ('p','rev-1',1000,100,0.25)"
        )
        db.execute("INSERT INTO job_watcher_calls(project,requested_at) VALUES ('p',1000)")
        db.execute(
            "INSERT INTO skill_metadata_calls(project,created,snapshot,model,cost) "
            "VALUES ('p',1000,'s','m',0.5)"
        )
    migrated = ProjectStore(tmp_path)
    try:
        state = RunState(migrated)
        assert state.model_costs("p") == {"reviewer": 0.25, "metadata": 0.5}
        assert state.model_info_cost("p") is None
    finally:
        migrated.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
        for table in ROLE_TABLES:
            assert db.execute(f"SELECT cost_source FROM {table}").fetchall() == [(None,)]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE job_watcher_calls SET cost_source='estimated'")


ROLE_TABLES = ("review_calls", "job_watcher_calls", "skill_metadata_calls")
UNPRICED_CALL = {
    "review_calls": "INSERT INTO review_calls(project,review_id,created,estimated_tokens,"
    "input_tokens,output_tokens) VALUES ('p','rev-2',?,100,10,2)",
    "job_watcher_calls": "INSERT INTO job_watcher_calls(project,requested_at,input_tokens,"
    "output_tokens) VALUES ('p',?,10,2)",
    "skill_metadata_calls": "INSERT INTO skill_metadata_calls(project,created,snapshot,model,"
    "input_tokens,output_tokens) VALUES ('p',?,'s','m',10,2)",
}


def test_each_roles_unpriced_calls_count_and_model_info_costs_are_told_apart(
    tmp_path: Path,
) -> None:
    """Unpriced auxiliary calls remain visible without claiming the local cap was hit;
    the part priced from gateway model information remains distinct."""
    projects, state = store(tmp_path)
    now = datetime.now(UTC)
    hour_ago = now - timedelta(hours=1)
    try:
        name = run_name(now, "r-main")
        state.append("p", "s", name, "started", {"role": "main"})
        usage = {"input_tokens": 5, "output_tokens": 1, "cost": 0.5, "cost_source": "model_info"}
        state.append("p", "s", name, "assistant", {"turn": 1, "usage": usage})
        with state.control.write() as tx:
            tx.sql.execute(
                "INSERT INTO review_calls(project,review_id,created,estimated_tokens,"
                "input_tokens,output_tokens,cost,cost_source) "
                "VALUES ('p','rev-1',?,100,10,2,0.25,'model_info')",
                (now.timestamp(),),
            )
            tx.sql.execute(
                "INSERT INTO job_watcher_calls(project,requested_at,input_tokens,output_tokens,"
                "cost,cost_source) VALUES ('p',?,10,2,0.125,'gateway')",
                (now.timestamp(),),
            )
        assert state.model_cost("p") == 0.875
        assert state.model_info_cost("p") == 0.75
        assert state.model_info_cost("p", now + timedelta(hours=1)) is None
        state.set_cost_limit("p", 100)
        assert (state.unpriced_calls("p", hour_ago), state.over_cost_limit("p", now)) == (0, False)
        for table in ROLE_TABLES:
            with state.control.write() as tx:
                tx.sql.execute(UNPRICED_CALL[table], (now.timestamp(),))
            assert (state.unpriced_calls("p", hour_ago), state.over_cost_limit("p", now)) == (
                1,
                False,
            ), table
            # Out of the window, it no longer counts.
            assert state.unpriced_calls("p", now + timedelta(hours=1)) == 0
            with state.control.write() as tx:
                tx.sql.execute(f"DELETE FROM {table} WHERE cost IS NULL")
            assert not state.over_cost_limit("p", now)
    finally:
        projects.close()


def test_repeated_unknown_cost_warning_is_coalesced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    projects, state = store(tmp_path)
    now = datetime.now(UTC)
    try:
        state.set_cost_limit("p", 100)
        with state.control.write() as tx:
            tx.sql.execute(UNPRICED_CALL["review_calls"], (now.timestamp(),))
        with caplog.at_level(logging.WARNING):
            for _ in range(120):
                assert state.cost_gate("p", now) == (100, False)
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
        assert "after 100 cost-gate checks" in warnings[1]
    finally:
        projects.close()


def test_an_unsaved_runs_cost_is_its_started_roles(tmp_path: Path) -> None:
    """A run without a record counts by the role its ``started`` event names (lab 40,
    B5): a delegated run that died is no main run's spending."""
    projects, state = store(tmp_path)
    try:
        now = datetime.now(UTC)
        for run, role, cost in (("r-main", "main", 0.5), ("r-child", "delegated", 0.25)):
            name = run_name(now, run)
            state.append("p", "s", name, "started", {"role": role})
            usage = {"model": "m", "input_tokens": 1, "output_tokens": 1, "cost": cost}
            state.append("p", "s", name, "assistant", {"turn": 1, "usage": usage})
        assert state.model_costs("p") == {"main": 0.5, "delegated": 0.25}
        assert list(state.model_costs("p")) == ["main", "delegated"]
        assert state.model_cost("p") == 0.75
        assert state.model_costs("p", now + timedelta(hours=1)) == {}
    finally:
        projects.close()
