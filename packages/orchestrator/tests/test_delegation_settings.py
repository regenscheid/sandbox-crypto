# SPDX-License-Identifier: MIT
"""A project's delegation setting and the model delegated runs call (plans-and-delegation
spec, PD-15, PD-27 to PD-29 and PD-38): the cap and a check's limits, as the project
keeps them; the delegated role's alias; and what a run's record says it ran under."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import (
    DelegationRecords,
    DelegationSettings,
)
from sandbox_science.orchestrator.projects import ProjectStore

from .control_versions import older_store
from .loop_harness import PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import (
    CHECK,
    REPORT,
    children,
    configure,
    delegation,
    results,
    started,
    wait,
)
from .test_delegation import delegating as delegating

OBJECT_LIMIT = 4096
"""The bytes a project's limits may take, as the store keeps them."""


def _store(path: Path) -> ProjectStore:
    store = ProjectStore(path)
    store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    return store


def test_a_setting_keeps_its_defaults_until_changed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        records = DelegationRecords(store.control)
        assert records.settings(PROJECT) == DelegationSettings(
            depth=0, cap=10, check_tokens=150_000, check_turns=12
        )
        assert records.configure(PROJECT, cap=3) == DelegationSettings(cap=3)
        changed = records.configure(PROJECT, depth=1, check_turns=5)
        assert changed == DelegationSettings(depth=1, cap=3, check_turns=5)
        assert records.settings(PROJECT) == changed
    finally:
        store.close()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"depth": 2}, "depth"),  # depth 2 comes later (PD-43)
        ({"cap": 0}, "cap"),
        ({"cap": 21}, "cap"),
        ({"check_tokens": 49_999}, "check_tokens"),
        ({"check_tokens": 400_001}, "check_tokens"),
        ({"check_turns": 0}, "check_turns"),
        ({"turns": 3}, "turns"),
    ],
)
def test_a_setting_out_of_range_is_refused_and_the_old_one_kept(
    tmp_path: Path, changes: dict[str, int], message: str
) -> None:
    store = _store(tmp_path)
    try:
        records = DelegationRecords(store.control)
        records.configure(PROJECT, cap=4)
        with pytest.raises(ControlError, match=f"^{message}: "):
            records.configure(PROJECT, **changes)
        assert records.settings(PROJECT) == DelegationSettings(cap=4)
        with pytest.raises(ControlError, match="no project"):
            records.configure("other", cap=4)
    finally:
        store.close()


@pytest.mark.parametrize(
    "limits",
    ["[1]", "not json", json.dumps({"note": "x" * OBJECT_LIMIT})],
)
def test_the_store_keeps_only_a_small_object_of_limits(tmp_path: Path, limits: str) -> None:
    store = _store(tmp_path)
    try:
        insert = (
            "INSERT INTO project_delegation (project_id, depth, updated_at, limits) "
            "VALUES (?, 0, '2026-10-15T12:00:00+00:00', ?)"
        )
        with (
            pytest.raises(sqlite3.IntegrityError, match="project_delegation"),
            store.control.write() as tx,
        ):
            tx.sql.execute(insert, (PROJECT, limits))
        DelegationRecords(store.control).configure(PROJECT, cap=2)
        with (
            pytest.raises(sqlite3.IntegrityError, match="project_delegation"),
            store.control.write() as tx,
        ):
            tx.sql.execute("UPDATE project_delegation SET limits = ?", (limits,))
    finally:
        store.close()


def test_the_cli_shows_and_changes_the_whole_setting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(tmp_path).close()
    argv = ["project", "delegation", "--state-dir", str(tmp_path), "--project", PROJECT]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == (
        f"project {PROJECT}: delegation depth 0 (off); at most 10 delegated runs at once; "
        "a check gets 150000 tokens and 12 turns\n"
    )
    assert cli.main([*argv, "--depth", "1", "--cap", "3", "--check-turns", "4"]) == 0
    assert capsys.readouterr().out == (
        f"project {PROJECT}: delegation depth 1 (main runs delegate checks); at most 3 "
        "delegated runs at once; a check gets 150000 tokens and 4 turns\n"
    )
    assert cli.main([*argv, "--check-tokens", "10"]) == 1
    assert capsys.readouterr().err.startswith("error: check_tokens: ")
    store = ProjectStore(tmp_path)
    try:
        found = DelegationRecords(store.control).settings(PROJECT)
    finally:
        store.close()
    assert found == DelegationSettings(depth=1, cap=3, check_turns=4)


async def test_a_check_gets_the_projects_turns_and_tokens(delegating: Harness) -> None:
    """PD-29, PD-38: the child's record keeps the limits it ran under; its parent's
    keeps its own."""
    configure(delegating, check_turns=2, check_tokens=60_000)
    root = await delegating.run(
        "root", [Reply(calls=[CHECK]), wait(), Reply("Done.")], delegated=[Reply(REPORT)]
    )
    assert json.loads(results(root)[0])["limits"] == {"turns": 2, "tokens": 60_000}
    (child,) = children(delegating, root)
    assert child.limits is not None
    assert (child.limits["max_turns"], child.limits["tokens"]) == (2, 60_000)
    assert root.limits is not None
    assert "tokens" not in root.limits
    assert {"max_turns", "max_input_chars"} <= set(root.limits)


async def test_delegated_runs_call_the_delegated_model(delegating: Harness) -> None:
    """PD-27, PD-29: with its own alias set, a child calls it and its record and its
    delegation's outcome name it; the root keeps MAIN's."""
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Done.")],
        delegated=[Reply(REPORT)],
        options=["--delegated-model", "delegated"],
    )
    (child,) = children(delegating, root)
    assert (root.alias, child.alias) == ("main", "delegated")
    assert {usage.model for usage in child.usage} == {"scripted-delegated"}
    assert {usage.model for usage in root.usage} == {"scripted-main"}
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert (outcome.alias, outcome.models) == ("delegated", ("scripted-delegated",))


async def test_without_its_own_alias_a_child_calls_mains(delegating: Harness) -> None:
    root = await delegating.run(
        "root", [Reply(calls=[CHECK]), wait(), Reply("Done.")], delegated=[Reply(REPORT)]
    )
    (child,) = children(delegating, root)
    assert child.alias == "main"
    assert {usage.model for usage in child.usage} == {"scripted-main"}


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (["--delegated-script", "replies.json"], "--delegated-script needs --delegated-model"),
        (["--delegated-model", "delegated"], "--delegated-model needs --model-url"),
    ],
)
def test_the_delegated_model_needs_a_gateway_or_a_script(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], options: list[str], error: str
) -> None:
    argv = [
        "run", "--worker", "http://127.0.0.1:9", "--token-file", str(tmp_path / "token"),
        "--project", PROJECT, "--session", SESSION, "--state-dir", str(tmp_path),
        "--allow-unisolated", "--model", "main", "--script", str(tmp_path / "main.json"),
    ]  # fmt: skip
    with pytest.raises(SystemExit):
        cli.main([*argv, *options, "Go."])
    assert error in capsys.readouterr().err


def test_a_version_32_store_keeps_its_depth_and_takes_the_default_limits(
    tmp_path: Path,
) -> None:
    current = _store(tmp_path / "current")
    try:
        DelegationRecords(current.control).configure(PROJECT, depth=1)
    finally:
        current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 32)
    with sqlite3.connect(target / "control.sqlite3") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(project_delegation)")}
    assert "limits" not in columns
    migrated = ProjectStore(target)
    try:
        records = DelegationRecords(migrated.control)
        assert records.settings(PROJECT) == DelegationSettings(depth=1)
        assert records.configure(PROJECT, cap=5) == DelegationSettings(depth=1, cap=5)
    finally:
        migrated.close()


async def test_a_run_record_without_alias_or_limits_still_reads(delegating: Harness) -> None:
    """Records written before PD-29 have neither."""
    root = await delegating.run("root", [Reply("Done.")])
    older = root.model_dump(mode="json", exclude={"alias", "limits"})
    read = type(root).model_validate(older)
    assert (read.alias, read.limits) == (None, None)
