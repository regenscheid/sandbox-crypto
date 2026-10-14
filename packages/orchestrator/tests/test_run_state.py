# SPDX-License-Identifier: MIT
"""The run service's records in the control store (run-service spec RS-4, RS-5, RS-13)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandbox_science.orchestrator.control import ControlError, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.testing import FakeClock


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
