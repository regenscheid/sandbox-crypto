# SPDX-License-Identifier: MIT
"""A run's place in its session's tree of runs (plans-and-delegation spec, PD-32)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
STARTED = datetime(2026, 10, 14, 12, tzinfo=UTC)


def record(run_id: str, **place: Any) -> RunRecord:
    return RunRecord.model_validate(
        {
            "run_id": run_id,
            "context": CONTEXT.model_dump(mode="json"),
            "task": "Work.",
            "started_at": STARTED,
            "ended_at": STARTED,
            "stopped": "answered",
            "final_text": "Done.",
            "messages": [],
            "tools": [],
            **place,
        }
    )


def test_a_delegated_run_is_listed_under_its_parent(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    store.create("p", "minimal", "sha256:" + "a" * 64)
    store.open_session("p", "s", dedicated=False)
    root = store.save_run(record("root"))
    child = record("child", role="delegated", parent=root.name, delegation="dlg-1")
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
    place: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="parent and its delegation"):
        record("r", **place)


def test_an_older_record_is_a_main_run() -> None:
    older = record("r")
    assert (older.role, older.parent, older.delegation) == ("main", None, None)
