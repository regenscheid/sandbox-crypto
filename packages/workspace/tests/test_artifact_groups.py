# SPDX-License-Identifier: MIT
"""The artifact list's groups and filters (research artifacts, RA-37): results, every
other recorded file, of one session or run, with who made each latest revision."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentStore, store_path

from .test_pdf_viewer import PDF_BYTES, SOURCE, publish
from .test_pdf_viewer import origins as origins

pytestmark = pytest.mark.asyncio


@pytest.fixture
def recorded(state_dir: Path) -> None:
    documents = DocumentStore(store_path(state_dir))
    inventory = ArtifactInventory(documents)
    publish(inventory, "op-pdf", "paper.pdf", PDF_BYTES, "application/pdf")
    child = SOURCE.model_copy(
        update={
            "kind": "execution_record",
            "run_id": "child-1",
            "role": "delegated",
            "parent_run": "20261014T120000000000Z-root",
            "delegation": "dlg-1",
            "tool_call": "call3",
            "execution_id": "exe-1",
        }
    )
    inventory.record_metadata(
        child, "data/raw.h5", sha256=hashlib.sha256(b"x").hexdigest(), size=2 * 1024**3,
        media_type="application/x-hdf5", note="too_large",
    )  # fmt: skip
    publish(inventory, "op-draft", "draft.md", b"draft", "text/markdown")
    inventory.record_deletion(child, "draft.md")  # a published file, deleted later
    documents.close()


async def listing(workspace: Any, query: str) -> list[dict[str, Any]]:
    response = await workspace.get(f"/api/v0/projects/p1/artifacts?{query}")
    assert response.status == 200, await response.text()
    return (await response.json())["artifacts"]


async def test_results_first_then_every_other_file(origins, recorded) -> None:
    workspace = origins[0]
    results = await listing(workspace, "group=results")
    assert {e["path"] for e in results} == {"sessions/a/paper.pdf", "sessions/a/draft.md"}
    draft = next(e for e in results if e["path"] == "sessions/a/draft.md")
    assert (draft["result"], draft["deleted"], draft["kind"], draft["role"]) == (
        True, True, "execution_record", "delegated",
    )  # fmt: skip
    (raw,) = await listing(workspace, "group=files")
    assert (raw["path"], raw["result"], raw["content_retained"], raw["content_note"]) == (
        "sessions/a/data/raw.h5", False, False, "too_large",
    )  # fmt: skip
    assert (raw["run_id"], raw["tool_call"], raw["session_id"]) == ("child-1", "call3", "a")
    assert len(await listing(workspace, "")) == 3  # both groups


async def test_a_session_or_a_run_keeps_its_own(origins, recorded) -> None:
    workspace = origins[0]
    assert {e["path"] for e in await listing(workspace, "run=child-1")} == {
        "sessions/a/data/raw.h5", "sessions/a/draft.md",
    }  # fmt: skip
    assert len(await listing(workspace, "session=a")) == 3
    assert await listing(workspace, "session=elsewhere") == []
    for bad in ("group=everything", "session=..%2Fx", "run=%20"):
        response = await workspace.get(f"/api/v0/projects/p1/artifacts?{bad}")
        assert response.status == 400, bad
