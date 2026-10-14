# SPDX-License-Identifier: MIT
"""An open session's snapshot tells a close and reopen apart from an unchanged session."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore

POLICY = "sha256:" + "a" * 64


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    store = ProjectStore(tmp_path)
    store.create("p", "minimal", POLICY)
    store.open_session("p", "s", dedicated=False)
    return store


def test_an_unchanged_session_matches(store: ProjectStore) -> None:
    first = store.session_snapshot("p", "s")
    store.set_policy("p", "sha256:" + "b" * 64)  # a change of the project, not the session
    second = store.session_snapshot("p", "s")
    assert first == second
    assert first.context.environment_id == store.get("p").default_environment_id


def test_a_close_and_reopen_does_not_match(store: ProjectStore) -> None:
    first = store.session_snapshot("p", "s")
    store.close_session("p", "s")
    store.open_session("p", "s", dedicated=False)
    second = store.session_snapshot("p", "s")
    assert first.context == second.context
    assert first != second


@pytest.mark.parametrize(("project", "session"), [("missing", "s"), ("p", "missing"), ("p", "s")])
def test_missing_and_closed_are_not_found(store: ProjectStore, project: str, session: str) -> None:
    if (project, session) == ("p", "s"):
        store.close_session(project, session)
    with pytest.raises(ProjectError) as error:
        store.session_snapshot(project, session)
    assert error.value.kind is ErrorKind.NOT_FOUND


@pytest.mark.parametrize(("project", "session"), [("../p", "s"), ("p", "../s")])
def test_invalid_scope_is_refused(store: ProjectStore, project: str, session: str) -> None:
    with pytest.raises(ProjectError) as error:
        store.session_snapshot(project, session)
    assert error.value.kind is ErrorKind.INVALID_REQUEST
