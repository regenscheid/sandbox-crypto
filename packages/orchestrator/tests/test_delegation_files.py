# SPDX-License-Identifier: MIT
"""A delegated run's files (research artifacts, RA-36; plans-and-delegation spec,
PD-17): the artifact revisions and tombstones its calls recorded, which its outcome
keeps apart from its own result, at most 200 with their total and the calls' gaps."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.delegation.hook import Child, ChildEnd
from sandbox_science.orchestrator.delegation.records import (
    FILES_BYTES,
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.projects import ProjectStore

from .control_versions import older_store
from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import CHECK, REPORT, RESULT, children, delegation, results, started, wait
from .test_delegation import delegating as delegating
from .test_delegation_lifecycle import BRIEF, _delegations


def entry(n: int, path: str = "") -> dict[str, JsonValue]:
    return {
        "path": path or f"out/file-{n:03d}.txt",
        "artifact_id": f"artifact-{n:024x}",
        "revision": 1,
        "sha256": "ab" * 32,
        "size": 10,
        "content_retained": True,
        "deleted": False,
        "reproduced": False,
    }


async def test_a_checks_recorded_files_reach_its_parent_apart_from_its_result(
    delegating: Harness,
) -> None:
    """The child writes a file; its outcome lists the revision its call recorded, and
    the parent's wait returns it after the result."""
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Done.")],
        delegated=[
            Reply(calls=[call("write_file", path="check.py", content="print(2 ** 16 % 17)\n")]),
            Reply(REPORT),
        ],
    )
    (child,) = children(delegating, root)
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert found.files is not None
    assert found.files == child.summaries["files"]
    assert found.files["total"] == 1
    listed = found.files["files"]
    assert isinstance(listed, list)
    (recorded,) = listed
    assert isinstance(recorded, dict)
    assert (recorded["path"], recorded["revision"], recorded["deleted"]) == ("check.py", 1, False)
    waited = json.loads(results(root)[1])["delegations"][0]
    assert list(waited)[-2:] == ["result", "files"]
    assert waited["files"] == found.files


async def test_a_check_that_wrote_nothing_has_no_files(delegating: Harness) -> None:
    root = await delegating.run(
        "root", [Reply(calls=[CHECK]), wait(), Reply("Done.")], delegated=[Reply(REPORT)]
    )
    (delegation_id,) = started(root)
    assert delegation(delegating, delegation_id).files is None
    assert "files" not in json.loads(results(root)[1])["delegations"][0]


async def test_a_long_list_is_cut_to_fit_the_parents_answer(tmp_path: Path) -> None:
    """The list is cut before the result, which always fits; its total stays."""
    files: dict[str, JsonValue] = {
        "total": 250,
        "files": [entry(n) for n in range(200)],
        "gaps": {"over the 100-file limit of a call": 50},
    }

    async def wrote(child: Child, stop: asyncio.Event) -> ChildEnd:
        return ChildEnd("answered", False, REPORT, result=RESULT, files=files)

    made, records, projects, documents = _delegations(tmp_path, wrote)
    try:
        async with made.lease():
            text, _ = await made.call("delegate", BRIEF)
            await asyncio.gather(*(c.task for c in made.children.values()))
            delegation_id = json.loads(text)["delegation_id"]
            answer, _ = await made.call("delegation_status", {"delegation_id": delegation_id})
        assert records.get(PROJECT, SESSION, delegation_id).files == files
        shown = json.loads(answer)
        assert len(answer) <= made.output_chars
        assert shown["result"] == RESULT
        assert (shown["files"]["total"], shown["files"]["cut"]) == (250, True)
        assert shown["files"]["gaps"] == {"over the 100-file limit of a call": 50}
        assert 0 < len(shown["files"]["files"]) < 200
    finally:
        documents.close()
        projects.close()


def made(n: int) -> Delegation:
    return Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": "20261015T120000000000Z-root",
            "run": f"20261015T12000{n}000000Z-child",
            "kind": "check",
            "brief": {"task": "Check."},
            "started_at": "2026-10-15T12:00:01+00:00",
        }
    )


def test_the_list_is_bounded_as_it_is_kept(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    try:
        store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        store.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(store.control)
        records.start(PROJECT, SESSION, made(0))
        long = [entry(n, "p/" + "x" * 2000 + str(n)) for n in range(200)]
        files: dict[str, JsonValue] = {"total": 300, "files": list[JsonValue](long)}
        ending = Ending("returned", "answered", "{}", files=files)
        kept = records.end(PROJECT, SESSION, made(0).delegation_id, ending)
        assert kept.files is not None
        assert kept.files["total"] == 300
        listed = kept.files["files"]
        assert isinstance(listed, list)
        assert 0 < len(listed) < 200
        assert listed == long[: len(listed)]
        assert len(json.dumps(kept.files).encode()) <= FILES_BYTES
    finally:
        store.close()


def test_a_version_37_store_keeps_its_outcomes_and_writes_files_once(tmp_path: Path) -> None:
    """Version 38 adds the files: none for the outcomes written before, and once with the
    rest of an outcome after."""
    current = ProjectStore(tmp_path / "current")
    try:
        current.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        current.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(current.control)
        for n in (0, 1):
            records.start(PROJECT, SESSION, made(n))
        records.end(PROJECT, SESSION, made(0).delegation_id, Ending("returned", "answered", "x"))
    finally:
        current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 37)
    with sqlite3.connect(target / "control.sqlite3") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(delegations)")}
    assert "files" not in columns
    migrated = ProjectStore(target)
    try:
        records = DelegationRecords(migrated.control)
        assert records.get(PROJECT, SESSION, made(0).delegation_id).files is None
        for statement, error in (
            ("UPDATE delegations SET files = '{}' WHERE ended IS NOT NULL", "written once"),
            ("UPDATE delegations SET files = '{}' WHERE ended IS NULL", "CHECK constraint"),
        ):
            with (
                pytest.raises(sqlite3.IntegrityError, match=error),
                migrated.control.write() as tx,
            ):
                tx.sql.execute(statement)
        files: dict[str, JsonValue] = {"total": 1, "files": [entry(1)]}
        ending = Ending("returned", "answered", "x", files=files)
        assert records.end(PROJECT, SESSION, made(1).delegation_id, ending).files == files
    finally:
        migrated.close()
