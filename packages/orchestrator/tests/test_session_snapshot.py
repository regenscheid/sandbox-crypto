# SPDX-License-Identifier: MIT
"""Bounded registry reads retain an inode fence through close/reopen ABA."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.projects import (
    MAX_SESSION_RECORD_BYTES,
    ProjectError,
    ProjectStore,
)


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    store = ProjectStore(tmp_path)
    store.create("p", "minimal", "sha256:" + "a" * 64)
    store.open_session("p", "s", dedicated=False)
    return store


def test_unchanged_binding_matches_without_writes(store: ProjectStore) -> None:
    path = store.root / "p.json"
    original = path.read_bytes()
    first = store.session_snapshot("p", "s")
    second = store.session_snapshot("p", "s")
    try:
        assert first.matches(second)
        assert first.context.environment_id == store.get("p").default_environment_id
    finally:
        second.close()
        first.close()
    assert path.read_bytes() == original


def test_complete_close_reopen_aba_even_with_identical_record_bytes(store: ProjectStore) -> None:
    path = store.root / "p.json"
    original = path.read_bytes()
    first = store.session_snapshot("p", "s")
    store.close_session("p", "s")
    store.open_session("p", "s", dedicated=False)
    # Even restoration of every timestamp/value cannot restore the pinned inode.
    restored = store.root / "restored"
    restored.write_bytes(original)
    os.replace(restored, path)
    second = store.session_snapshot("p", "s")
    try:
        assert first.session == second.session
        assert not first.matches(second)
    finally:
        first.close()
        second.close()


def test_in_place_change_cannot_reuse_the_identity(store: ProjectStore) -> None:
    first = store.session_snapshot("p", "s")
    record = store.get("p")
    changed = record.model_copy(update={"updated_at": datetime.now(UTC)})
    (store.root / "p.json").write_text(changed.model_dump_json())
    second = store.session_snapshot("p", "s")
    try:
        assert not first.matches(second)
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("case", ["oversize", "sparse", "fifo", "symlink", "directory"])
def test_preflight_refuses_special_or_oversize_before_read(
    store: ProjectStore, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    path = store.root / "p.json"
    path.unlink()
    if case == "oversize":
        path.write_bytes(b"x" * (MAX_SESSION_RECORD_BYTES + 1))
    elif case == "sparse":
        with path.open("wb") as file:
            file.truncate(1024**3)
    elif case == "fifo":
        os.mkfifo(path)
    elif case == "symlink":
        target = store.root / "other"
        target.write_text("{}")
        path.symlink_to(target)
    else:
        path.mkdir()

    def no_read(*args: object) -> NoReturn:
        pytest.fail("special/oversize registry crossed the allocation preflight")

    monkeypatch.setattr(os, "read", no_read)
    with pytest.raises(ProjectError) as error:
        store.session_snapshot("p", "s")
    assert error.value.kind is ErrorKind.UNAVAILABLE


@pytest.mark.parametrize("case", ["project", "session", "bool", "naive", "json"])
def test_damaged_scope_and_types_are_unavailable(store: ProjectStore, case: str) -> None:
    path = store.root / "p.json"
    value = json.loads(path.read_text())
    if case == "project":
        value["project_id"] = "other"
    elif case == "session":
        value["sessions"]["s"]["session_id"] = "other"
    elif case == "bool":
        value["sessions"]["s"]["dedicated"] = 1
    elif case == "naive":
        value["sessions"]["s"]["opened_at"] = "2026-10-12T00:00:00"
    path.write_text("{" if case == "json" else json.dumps(value))
    with pytest.raises(ProjectError) as error:
        store.session_snapshot("p", "s")
    assert error.value.kind is ErrorKind.UNAVAILABLE


@pytest.mark.parametrize(("project", "session"), [("missing", "s"), ("p", "missing"), ("p", "s")])
def test_missing_and_closed_are_not_found(store: ProjectStore, project: str, session: str) -> None:
    if (project, session) == ("p", "s"):
        store.close_session(project, session)
    with pytest.raises(ProjectError) as error:
        store.session_snapshot(project, session)
    assert error.value.kind is ErrorKind.NOT_FOUND


@pytest.mark.parametrize(("project", "session"), [("../p", "s"), ("p", "../s")])
def test_invalid_scope_is_not_a_path(store: ProjectStore, project: str, session: str) -> None:
    with pytest.raises(ProjectError) as error:
        store.session_snapshot(project, session)
    assert error.value.kind is ErrorKind.INVALID_REQUEST


def test_growth_during_read_is_bounded_and_refused(
    store: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.read
    read_bytes = 0

    def grow(fd: int, size: int) -> bytes:
        nonlocal read_bytes
        value = real(fd, size)
        if not read_bytes:
            with (store.root / "p.json").open("ab") as file:
                file.write(b" " * MAX_SESSION_RECORD_BYTES)
        read_bytes += len(value)
        return value

    monkeypatch.setattr(os, "read", grow)
    with pytest.raises(ProjectError) as error:
        store.session_snapshot("p", "s")
    assert error.value.kind is ErrorKind.UNAVAILABLE
    assert read_bytes <= MAX_SESSION_RECORD_BYTES + 1
