# SPDX-License-Identifier: MIT
"""The execution journal's numbering, through ``ExecutionJournal``'s own methods."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ExecutionState
from sandbox_science.worker.journal import ExecutionJournal
from sandbox_science.worker.timeouts import MANAGER_TIMEOUTS

from .harness import Harness, view


async def test_an_event_that_fails_to_store_takes_no_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with await Harness.start(tmp_path) as worker:
        done = await worker.run("journal-seed", kind="shell", script="true")
        store = worker.manager.store
        stored = worker.manager.get(done["execution_id"])
        journal = ExecutionJournal(
            store,
            view(worker.manager).connections(),
            clock=worker.clock,
            timeouts=MANAGER_TIMEOUTS,
        )
        live = journal.track(stored)
        before = live.execution.last_seq
        commit = store.commit_event

        def failing(*args: Any) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(store, "commit_event", failing)
        with pytest.raises(sqlite3.OperationalError):
            journal.state(live, ExecutionState.RUNNING)
        assert live.execution.last_seq == before
        monkeypatch.setattr(store, "commit_event", commit)
        journal.state(live, ExecutionState.RUNNING)
        assert live.execution.last_seq == before + 1
        assert store.last_seq(stored.execution_id) == before + 1
