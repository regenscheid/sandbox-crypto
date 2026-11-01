# SPDX-License-Identifier: MIT
"""Prepared contexts keep reconstruction, append and retrieval in one admitted scope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.contracts.worker import KernelObservation
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.geometry import DEFAULT_SHARES
from sandbox_science.orchestrator.context.preparation import (
    FoldLimits,
    HistoryScope,
    prepare_context,
)
from sandbox_science.orchestrator.control import ControlError, store_path
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.hooks import Appended
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

PROJECT, SESSION = "project-a", "session-a"
CONTEXT = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="env-a")


@pytest.mark.parametrize("delegated", [False, True])
async def test_prepared_history_appends_reads_and_reopens_in_its_admitted_scope(
    tmp_path: Path, delegated: bool
) -> None:
    projects = ProjectStore(tmp_path)
    documents = DocumentStore(store_path(tmp_path))
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    archive = Archive(projects.control)
    root = archive.append(PROJECT, SESSION, "root", Entry("user", "user", "ROOT SECRET"))
    sibling = archive.append(
        PROJECT, SESSION, "sibling", Entry("user", "task", "SIBLING SECRET"), parent="root"
    )
    scope = HistoryScope(CONTEXT, "child" if delegated else "root", "root" if delegated else None)

    async def observe(context: ExecutionContext) -> KernelObservation:
        raise AssertionError("there are no kernels in this fixture")

    async def prepare():
        return await prepare_context(
            scope,
            projects=projects,
            documents=documents,
            run_state=RunState(projects.control),
            limits=FoldLimits(160_000, 32_000, DEFAULT_SHARES),
            alias="test-model",
            brief="Find exact squares.",
            delegation=None,
            narrator=None,
            former_model=lambda alias: None,
            learned={},
            observe_kernels=observe,
        )

    try:
        made = await prepare()
        await made.log.append(
            {"role": "user", "content": "Exact square 17^2 = 289."}, Appended("task")
        )
        text, _ = await made.retrieval.call("history_search", {"query": "289"})
        hit = json.loads(text)["hits"][0]
        assert hit["run"] == scope.run
        text, _ = await made.retrieval.call("history_read", {"message": hit["message_id"]})
        assert "17^2 = 289" in text
        if delegated:
            for hidden in (root, sibling):
                with pytest.raises(ControlError, match="has no"):
                    await made.retrieval.call("history_read", {"message": hidden.message_id})
        else:
            text, _ = await made.retrieval.call("history_read", {"message": sibling.message_id})
            assert "SIBLING SECRET" in text

        # A new preparation reconstructs canonical appends without sharing live objects.
        reopened = await prepare()
        view = json.dumps(reopened.view.messages)
        assert "17^2 = 289" in view
        if delegated:
            assert "ROOT SECRET" not in view
            assert "SIBLING SECRET" not in view
        else:
            assert "ROOT SECRET" in view
    finally:
        documents.close()
        projects.close()
