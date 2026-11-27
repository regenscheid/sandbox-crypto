# SPDX-License-Identifier: MIT
"""What an acceptance records of the parent's reads of a returned task's files
(plans-and-delegation spec, PD-46), and what ``accept_delegation`` returns.

The records' rules on a store of their own: a result file or deliverable counts as read
only when the accepting run, or the task's parent run, read it whole at the revision the
result names, after the task's latest return; the child's own reads, and reads before
the return, do not count. The account is written once, with the acceptance, and a store
of version 91 gains the column empty.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from sandbox_science.contracts.child_environments import ChildLimits
from sandbox_science.orchestrator.context.history import Archive
from sandbox_science.orchestrator.context.history.archive import Entry
from sandbox_science.orchestrator.context.history.reads import Read
from sandbox_science.orchestrator.delegation.acceptance import unread_text
from sandbox_science.orchestrator.delegation.hook import accepted, limits_text
from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.testing import FakeClock

from .control_versions import older_store

PROJECT, SESSION = "p", "s"
PARENT = "20261126T120000000000Z-run-parent"
CHILD = "20261126T120001000000Z-run-child"
DELEGATION = "dlg-" + "a" * 24
A, B, C = "a" * 64, "b" * 64, "c" * 64


def pinned(path: str, sha256: str, revision: int = 1) -> dict[str, JsonValue]:
    return {
        "path": path,
        "artifact_id": f"artifact-{sha256[:24]}",
        "revision": revision,
        "sha256": sha256,
        "size": 100,
        "content_retained": True,
    }


def task() -> Delegation:
    return Delegation.model_validate(
        {
            "delegation_id": DELEGATION,
            "parent": PARENT,
            "run": CHILD,
            "kind": "task",
            "brief": {
                "task": "Tabulate.",
                "deliverables": [
                    {"id": "d1", "title": "Table", "kind": "artifact", "description": "It."},
                    {"id": "d2", "title": "Notes", "kind": "file", "description": "Them."},
                ],
            },
            "started_at": "2026-11-26T12:00:01+00:00",
        }
    )


AREA = f"work/{DELEGATION}"
FILES: dict[str, JsonValue] = {
    "total": 3,
    "files": [
        {**pinned(f"{AREA}/table.txt", A), "deleted": False},
        {**pinned(f"{AREA}/check.py", B), "deleted": False},
        {**pinned(f"{AREA}/notes.md", C), "deleted": False},
    ],
    "results": [pinned(f"{AREA}/table.txt", A), pinned(f"{AREA}/check.py", B)],
}
RESULT: dict[str, JsonValue] = {
    "status": "complete",
    "summary": "Tabulated.",
    "result_files": [f"{AREA}/table.txt", f"{AREA}/check.py"],
    "deliverables": [
        {"id": "d1", "status": "produced", "path": f"{AREA}/table.txt", "revision": 1},
        {"id": "d2", "status": "produced", "path": f"{AREA}/notes.md"},
    ],
}


def read(archive: Archive, run: str, sha256: str, *, shown: int = 100) -> None:
    """A ``read_file`` of ``run`` that showed ``shown`` bytes of a 100-byte file."""
    detail = {"ranges": [[0, shown]], "size": 100}
    archive.append(
        PROJECT,
        SESSION,
        run,
        Entry("tool", "tool_result", "...", source="read_file", call_id=f"c-{sha256[:4]}"),
        reads=[Read("file", sha256, "whole" if shown == 100 else "partial", detail)],
    )


@pytest.fixture
def store(tmp_path: Path) -> Any:
    projects = ProjectStore(tmp_path)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    yield projects
    projects.close()


def test_an_acceptance_records_which_files_its_run_read_after_the_return(store: Any) -> None:
    clock = FakeClock(datetime(2026, 11, 26, 12, tzinfo=UTC))
    records = DelegationRecords(store.control, clock=clock)
    archive = Archive(store.control, clock=clock)
    records.start(PROJECT, SESSION, task())
    # Before the return, the parent read the check whole: it does not count.
    read(archive, PARENT, B)
    clock.advance(60)
    records.end(PROJECT, SESSION, DELEGATION, Ending("returned", "answered", "{}", result=RESULT,
                                                     files=FILES))  # fmt: skip
    clock.advance(60)
    # After it: the child read its own notes whole, and the parent read the table whole
    # in two ranges and the notes in part.
    read(archive, CHILD, C)
    read(archive, PARENT, A, shown=60)
    archive.append(
        PROJECT,
        SESSION,
        PARENT,
        Entry("tool", "tool_result", "...", source="read_file", call_id="c-rest"),
        reads=[Read("file", A, "partial", {"ranges": [[60, 100]], "size": 100})],
    )
    read(archive, PARENT, C, shown=10)
    closed = records.accept(PROJECT, SESSION, DELEGATION, by=PARENT, reason="parent").delegation
    assert closed.review_reads == {
        "files": [
            {"path": f"{AREA}/table.txt", "revision": 1, "deliverables": ["d1"], "read": True},
            {"path": f"{AREA}/check.py", "revision": 1, "read": False},
            {"path": f"{AREA}/notes.md", "revision": 1, "deliverables": ["d2"], "read": False},
        ],
        "unread": 2,
    }
    assert unread_text(closed.review_reads) == (
        f"accepted unread: {AREA}/check.py (revision 1), {AREA}/notes.md (revision 1)"
    )
    # It is kept as written, and never written again.
    assert records.get(PROJECT, SESSION, DELEGATION).review_reads == closed.review_reads
    with store.control.write() as tx, pytest.raises(sqlite3.IntegrityError, match="written once"):
        tx.sql.execute(
            "UPDATE delegations SET review_reads = '{}' WHERE delegation_id = ?", (DELEGATION,)
        )


def test_accept_delegation_returns_the_acceptance_not_the_outcome(store: Any) -> None:
    clock = FakeClock(datetime(2026, 11, 26, 12, tzinfo=UTC))
    records = DelegationRecords(store.control, clock=clock)
    archive = Archive(store.control, clock=clock)
    records.start(PROJECT, SESSION, task())
    records.end(PROJECT, SESSION, DELEGATION, Ending("returned", "answered", "{}", result=RESULT,
                                                     files=FILES))  # fmt: skip
    clock.advance(60)
    for digest in (A, B, C):
        read(archive, PARENT, digest)
    closed = records.accept(
        PROJECT, SESSION, DELEGATION, by=PARENT, reason="parent", note="All read."
    ).delegation
    shown = accepted(closed)
    assert not {"result", "result_files", "summary"} & set(shown)
    assert shown["review"] == "accepted"
    assert shown["reviewed"] == {"by": PARENT, "reason": "parent", "note": "All read."}
    files = shown["files"]
    assert isinstance(files, list)
    assert [f["read"] for f in files] == [True, True, True]
    assert "unread" not in shown
    assert "Accepted, which is final" in str(shown["closed"])


def test_a_cancel_records_no_reads(store: Any) -> None:
    records = DelegationRecords(store.control)
    records.start(PROJECT, SESSION, task())
    records.end(PROJECT, SESSION, DELEGATION, Ending("returned", "answered", "{}", result=RESULT,
                                                     files=FILES))  # fmt: skip
    closed = records.accept(
        PROJECT, SESSION, DELEGATION, by=PARENT, reason="parent", review="cancelled"
    ).delegation
    assert (closed.review, closed.review_reads) == ("cancelled", None)


def test_a_file_whose_revision_was_not_recorded_is_never_shown_read(store: Any) -> None:
    """A deliverable whose path the task's calls recorded no revision of has no digest to
    match, so no read can show it."""
    records = DelegationRecords(store.control)
    archive = Archive(store.control)
    records.start(PROJECT, SESSION, task())
    files: dict[str, JsonValue] = {
        "total": 1,
        "files": [{**pinned(f"{AREA}/table.txt", A), "deleted": False}],
        "results": [pinned(f"{AREA}/table.txt", A)],
    }
    result = {**RESULT, "result_files": [f"{AREA}/table.txt"]}
    records.end(PROJECT, SESSION, DELEGATION, Ending("returned", "answered", "{}", result=result,
                                                     files=files))  # fmt: skip
    read(archive, PARENT, C)
    closed = records.accept(PROJECT, SESSION, DELEGATION, by=PARENT, reason="parent").delegation
    assert closed.review_reads is not None
    listed = closed.review_reads["files"]
    assert listed == [
        {"path": f"{AREA}/table.txt", "revision": 1, "deliverables": ["d1"], "read": False},
        {"path": f"{AREA}/notes.md", "revision": None, "deliverables": ["d2"], "read": False},
    ]


def test_a_version_91_store_gains_the_column_empty_and_fills_it_once(tmp_path: Path) -> None:
    current = ProjectStore(tmp_path / "current")
    try:
        current.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        current.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(current.control)
        records.start(PROJECT, SESSION, task())
        records.end(PROJECT, SESSION, DELEGATION, Ending("returned", "answered", "{}",
                                                         result=RESULT, files=FILES))  # fmt: skip
    finally:
        current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 91)
    with sqlite3.connect(target / "control.sqlite3") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(delegations)")}
    assert "review_reads" not in columns
    migrated = ProjectStore(target)
    try:
        records = DelegationRecords(migrated.control)
        assert records.get(PROJECT, SESSION, DELEGATION).review_reads is None
        with (
            pytest.raises(sqlite3.IntegrityError, match="written once"),
            migrated.control.write() as tx,
        ):
            tx.sql.execute("UPDATE delegations SET review_reads = '{}'")
        closed = records.accept(PROJECT, SESSION, DELEGATION, by=PARENT, reason="parent")
        assert closed.delegation.review_reads is not None
        assert closed.delegation.review_reads["unread"] == 3
    finally:
        migrated.close()


def test_the_limits_line_names_cpus_memory_and_disk() -> None:
    limits = ChildLimits(cpu_millicores=1500, memory_mib=4096, scratch_disk_mib=512)
    assert limits_text(limits, own=False) == (
        "1.5 CPUs, 4 GiB of memory and 512 MiB of scratch disk outside the workspace"
    )
    assert limits_text(ChildLimits(cpu_millicores=1000, memory_mib=1536, scratch_disk_mib=20480))
    line = limits_text(limits)
    assert line.startswith("Your environment's limits: 1.5 CPUs, 4 GiB of memory")
    assert json.dumps(line)
