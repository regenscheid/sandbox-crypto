# SPDX-License-Identifier: MIT
"""The artifact list's groups and filters (research artifacts, RA-37): results, every
other recorded file, of one session or run, with who made each latest revision."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts.artifact_inventory import MAX_ARTIFACT_BYTES
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.workspace.api import dumps
from sandbox_science.workspace.artifacts import ArtifactEntry, ArtifactListing

from .test_pdf_viewer import PDF_BYTES, SOURCE, publish
from .test_pdf_viewer import origins as origins


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


@pytest.fixture
def many(state_dir: Path) -> None:
    documents = DocumentStore(store_path(state_dir))
    inventory = ArtifactInventory(documents)
    for index in range(25):
        inventory.record_metadata(
            SOURCE.model_copy(update={"kind": "execution_record", "tool_call": f"c{index}"}),
            f"out/n{index:02d}.txt", sha256=hashlib.sha256(b"%d" % index).hexdigest(),
            size=index, media_type="text/plain", note="quota",
        )  # fmt: skip
    documents.close()


async def test_all_files_come_newest_first_a_page_at_a_time(origins, many) -> None:
    workspace = origins[0]
    seen: list[str] = []
    before = ""
    while True:
        response = await workspace.get(f"/api/v0/projects/p1/artifacts?group=files{before}")
        page = await response.json()
        seen += [e["path"] for e in page["artifacts"]]
        if page["next_before"] is None:
            break
        before = f"&before={page['next_before']}"
    assert seen == [f"sessions/a/out/n{index:02d}.txt" for index in reversed(range(25))]


async def test_a_path_filter_finds_a_file_among_many(origins, many) -> None:
    workspace = origins[0]
    assert [e["path"] for e in await listing(workspace, "contains=n07")] == [
        "sessions/a/out/n07.txt"
    ]
    assert len(await listing(workspace, "contains=out/")) == 10  # a page
    for bad in ("before=x", "before=-1", "contains=%01", f"contains={'x' * 257}"):
        response = await workspace.get(f"/api/v0/projects/p1/artifacts?{bad}")
        assert response.status == 400, bad


async def test_a_bad_pattern_names_its_line(origins) -> None:
    workspace = origins[0]
    headers = {"Origin": str(workspace.make_url("/")).rstrip("/")}
    value = {"expected": 0, "recording": "all", "ignore": [".cache/", "!keep"], "operation_id": "o"}
    route = "/api/v0/projects/p1/files/settings"
    response = await workspace.post(route, json=value, headers=headers)
    assert response.status == 400
    assert (await response.json())["error"]["message"].startswith("line 2:")


API = Path(__file__).parents[3] / "web" / "workspace" / "src" / "lib" / "api.ts"


def test_the_client_reads_every_listing_the_route_can_send() -> None:
    found = re.search(r"ARTIFACT_LISTING_MAX_BYTES = (\d+) \* 1024;", API.read_text())
    assert found, "api.ts must set ARTIFACT_LISTING_MAX_BYTES as a number of KiB"
    entry = ArtifactEntry(
        artifact_id="a" * 128, revision=999999999,
        # 4,096 bytes of four-byte characters, each sent as two escapes, after the session.
        path="sessions/" + "s" * 128 + "/" + "\U0001f600" * 1024,
        sha256="f" * 64, size=MAX_ARTIFACT_BYTES * 2**10, media_type="x/" + "y" * 250,
        created_at=datetime(2026, 10, 14, 12, 0, 0, 123456, UTC), result=True, deleted=False,
        content_retained=False, content_note="too_large", kind="execution_record",
        session_id="s" * 128, run_id="r" * 128, role="delegated", tool_call="c" * 128,
    )  # fmt: skip
    listing = ArtifactListing(artifacts=[entry] * 10, next_before=2**62, viewer=True)
    largest = len(dumps(listing.model_dump(mode="json")).encode())
    assert largest <= int(found[1]) * 1024
