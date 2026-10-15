# SPDX-License-Identifier: MIT
"""The project memory over the workspace API: the Memory page's reads and the user's
decisions (docs/specs/project-memory.md, PM-4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.memory import ProjectMemory

from .test_reviews import client as client

BASE = "/api/v0/projects/p1/memory"
AGENT = Author(kind="agent", session_id="a", run_id="r1", role="main", alias="m", model="m-1")


@pytest.fixture
def proposed(state_dir: Path) -> None:
    documents = DocumentStore(store_path(state_dir))
    memory = ProjectMemory(documents)
    request = {"action": "add", "text": "Install with `provision`.", "reason": "Lost on restart."}
    memory.propose("p1", request, AGENT, "op-1")
    memory.propose("p1", {**request, "text": "Use 128-bit floats."}, AGENT, "op-2")
    documents.close()


async def post(client: Any, value: dict[str, Any]) -> Any:
    headers = {"Origin": str(client.make_url("/")).rstrip("/")}
    return await client.post(BASE, json=value, headers=headers)


async def test_the_user_decides_the_proposals(client, proposed) -> None:
    shown = await (await client.get(BASE)).json()
    assert shown["bound"] == 16000
    assert (shown["memory"]["version"], shown["memory"]["waiting"]) == (0, 2)
    proposals = (await (await client.get(BASE + "/proposals")).json())["proposals"]
    assert [p["proposal_id"] for p in proposals] == ["prop-1", "prop-2"]
    assert proposals[0]["proposer"]["model"] == "m-1"
    value = {"action": "accept", "proposal": "prop-1", "operation_id": "u1"}
    assert (await client.post(BASE, json=value)).status == 403  # no same-origin proof
    accepted = await post(client, value)
    assert accepted.status == 200
    assert (await accepted.json())["entry_id"] == "mem-1"
    assert (await (await post(client, value)).json())["replayed"] is True
    declined = await post(client, {"action": "decline", "proposal": "prop-2", "operation_id": "u2"})
    assert declined.status == 200
    shown = await (await client.get(BASE)).json()
    assert [e["text"] for e in shown["memory"]["entries"]] == ["Install with `provision`."]
    assert shown["memory"]["waiting"] == 0
    history = (await (await client.get(BASE + "/mem-1/history")).json())["history"]
    assert history["versions"][0]["proposer"]["run_id"] == "r1"
    assert history["versions"][0]["author"]["kind"] == "user"


async def test_the_user_edits_and_conflicts_are_named(client) -> None:
    added = await post(client, {"action": "add", "text": "One.", "operation_id": "u1"})
    assert (await added.json())["revision"] == 1
    edit = {"action": "edit", "entry": "mem-1", "expected": 1, "text": "Two.", "operation_id": "u2"}
    assert (await post(client, edit)).status == 200
    stale = await post(client, {**edit, "text": "Three.", "operation_id": "u3"})
    assert stale.status == 409
    assert (await post(client, {**edit, "operation_id": "u2", "text": "Other."})).status == 409


@pytest.mark.parametrize(
    "path",
    [
        BASE + "?x=1",
        BASE + "/proposals?after=1",
        BASE + "/mem-0/history",
        BASE + "/entry-1/history",
        BASE + "/mem-1/history?before=0",
        BASE + "/mem-1/history?before=1&before=2",
    ],
)
async def test_bad_reads_are_refused(client, path: str) -> None:
    assert (await client.get(path)).status == 400


async def test_bad_changes_are_refused(client) -> None:
    assert (await client.get("/api/v0/projects/missing/memory")).status == 404
    assert (await client.get(BASE + "/mem-1/history")).status == 404
    for value in (
        {"action": "add", "text": "x" * 1001, "operation_id": "u1"},
        {"action": "add", "text": "x", "operation_id": "u1", "author": "agent"},
        {"action": "edit", "entry": "mem-1", "text": "x", "operation_id": "u1"},
        {
            "action": "move",
            "entry": "mem-1",
            "expected": 1,
            "direction": "left",
            "operation_id": "u",
        },
        {"action": "accept", "proposal": "1", "operation_id": "u1"},
    ):
        assert (await post(client, value)).status == 400, value
    missing = {"action": "decline", "proposal": "prop-9", "operation_id": "u1"}
    assert (await post(client, missing)).status == 404
