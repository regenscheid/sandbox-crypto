# SPDX-License-Identifier: MIT
"""A run's place in its session's tree of runs (plans-and-delegation spec, PD-32)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.harness import RunRequest
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore, RunRecord, run_name

from .control_versions import older_store

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
STARTED = datetime(2026, 10, 14, 12, tzinfo=UTC)


LATER = STARTED + timedelta(seconds=1)


def record(
    run_id: str, *, started: datetime = STARTED, session: str = "s", **place: Any
) -> RunRecord:
    context = CONTEXT.model_copy(update={"session_id": session})
    return RunRecord.model_validate(
        {
            "run_id": run_id,
            "context": context.model_dump(mode="json"),
            "task": "Work.",
            "started_at": started,
            "ended_at": started,
            "stopped": "answered",
            "final_text": "Done.",
            "messages": [],
            "tools": [],
            **place,
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ProjectStore]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.open_session("p", "other", dedicated=False)
    yield projects
    projects.close()


def delegated(run_id: str, parent: str, *, started: datetime = LATER) -> RunRecord:
    return record(
        run_id, started=started, role="delegated", parent=parent, delegation=f"dlg-{run_id}"
    )


def test_a_delegated_run_is_listed_under_its_parent(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    store.create("p", "minimal", "sha256:" + "a" * 64)
    store.open_session("p", "s", dedicated=False)
    root = store.save_run(record("root"))
    child = record("child", started=LATER, role="delegated", parent=root.name, delegation="dlg-1")
    store.save_run(child)
    listed = {entry.run_id: (entry.role, entry.parent) for entry in store.runs("p", "s")}
    assert listed == {"root": ("main", None), "child": ("delegated", root.name)}
    kept = store.run("p", "s", next(e.name for e in store.runs("p", "s") if e.run_id == "child"))
    assert (kept.role, kept.parent, kept.delegation) == ("delegated", root.name, "dlg-1")
    store.close()


@pytest.mark.parametrize(
    "place",
    [
        {"role": "delegated"},
        {"role": "delegated", "parent": "20261014T120000000000Z-root"},
        {"role": "main", "parent": "20261014T120000000000Z-root", "delegation": "dlg-1"},
        {"delegation": "dlg-1"},
    ],
)
def test_a_run_is_main_or_delegated_with_both_its_parent_and_delegation(
    place: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="parent and its delegation"):
        record("r", **place)


def test_an_older_record_is_a_main_run() -> None:
    older = record("r")
    assert (older.role, older.parent, older.delegation) == ("main", None, None)


@pytest.mark.parametrize(
    "parent",
    [
        run_name(LATER, "child"),  # itself
        run_name(LATER + timedelta(seconds=1), "later"),  # a run that started after it
        "../../etc/passwd",
        "",
    ],
)
def test_a_parent_is_a_run_name_from_before_the_child(parent: str) -> None:
    with pytest.raises(ValidationError):
        delegated("child", parent)


def test_a_parent_is_a_run_of_the_same_session(store: ProjectStore) -> None:
    elsewhere = store.save_run(record("root", session="other"))
    with pytest.raises(ProjectError, match="no run of session") as refused:
        store.save_run(delegated("child", elsewhere.name))
    assert refused.value.kind is ErrorKind.INVALID_REQUEST
    with pytest.raises(ProjectError, match="no run of session"):
        store.save_run(delegated("child", run_name(STARTED, "never")))
    assert store.runs("p", "s") == []


def test_a_parent_still_running_is_known_by_its_events(store: ProjectStore) -> None:
    """A parent's record is kept after its children's: its events name it meanwhile."""
    parent = run_name(STARTED, "root")
    with store.control.write() as tx:
        tx.sql.execute(
            "INSERT INTO run_events VALUES ('p', 's', ?, 1, ?, 'started', '{}')",
            (parent, STARTED.isoformat()),
        )
    store.save_run(delegated("child", parent))
    (entry,) = store.runs("p", "s")
    assert entry.parent == parent


def test_only_a_name_kept_already_is_a_conflict(store: ProjectStore) -> None:
    store.save_run(record("root"))
    with pytest.raises(ProjectError, match="already") as refused:
        store.save_run(record("root"))
    assert refused.value.kind is ErrorKind.CONFLICT


@pytest.mark.parametrize(
    ("role", "parent", "delegation"),
    [("main", "20261014T120000000000Z-root", None), ("delegated", None, None),
     ("delegated", "20261014T120000000000Z-root", None)],
)  # fmt: skip
def test_the_store_ties_a_runs_role_to_its_parent(
    store: ProjectStore, role: str, parent: str | None, delegation: str | None
) -> None:
    with (
        pytest.raises(sqlite3.IntegrityError, match="parent and its delegation"),
        store.control.write() as tx,
    ):
        tx.sql.execute(
            "INSERT INTO runs (project_id, session_id, name, run_id, started_at, record, "
            "role, parent, delegation) VALUES ('p', 's', ?, 'x', ?, '{}', ?, ?, ?)",
            (run_name(LATER, "x"), LATER.isoformat(), role, parent, delegation),
        )


@pytest.mark.parametrize(("parent", "delegation"), [("p", None), (None, "dlg-1")])
def test_a_run_request_names_its_parent_and_delegation_together(
    parent: str | None, delegation: str | None
) -> None:
    with pytest.raises(ValueError, match="together"):
        RunRequest("p", "s", "Go", parent=parent, delegation=delegation)


def test_a_version_10_store_keeps_its_runs_as_roots(tmp_path: Path) -> None:
    """A store from before the run tree (version 10) migrates, its runs main runs."""
    current = ProjectStore(tmp_path / "current")
    current.create("p", "minimal", "sha256:" + "a" * 64)
    current.open_session("p", "s", dedicated=False)
    current.save_run(record("old"))
    current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 10)
    with sqlite3.connect(target / "control.sqlite3") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
    assert not columns & {"role", "parent", "delegation"}
    migrated = ProjectStore(target)
    try:
        (entry,) = migrated.runs("p", "s")
        assert (entry.run_id, entry.role, entry.parent) == ("old", "main", None)
        migrated.save_run(delegated("child", entry.name))
        assert [e.role for e in migrated.runs("p", "s")] == ["main", "delegated"]
    finally:
        migrated.close()
    with sqlite3.connect(target / "control.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
