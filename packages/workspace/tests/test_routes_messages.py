# SPDX-License-Identifier: MIT
"""An archived message through HTTP (context management, CM-3; consistency review,
CR-14 as amended): read whole by its ID, as ``history_read`` gives it, for the review
views' links."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.operations import CHUNK_BYTES, ToolRecord
from sandbox_science.orchestrator.projects import ProjectStore

from .route_support import ClientFactory, error_of, workspace

RUN = "20261016T030000000000Z-run-1"
PATH = "/api/v0/projects/p1/messages/"


async def test_an_archived_message_reads_whole_and_an_unknown_one_is_not_found(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    store = ProjectStore(state_dir)
    try:
        archive = Archive(store.control)
        call = {"id": "c1", "type": "function", "function": {"name": "sage", "arguments": "{}"}}
        answer = archive.append(
            "p1", "a", RUN, Entry("assistant", "assistant", "Factoring.", tool_calls=(call,))
        )
        result = archive.append(
            "p1", "a", RUN, Entry("tool", "tool_result", "7031\n" * 3, source="sage", call_id="c1")
        )
    finally:
        store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir)
    found = await (await client.get(PATH + result.message_id)).json()
    assert (found["message"], found["session"], found["position"], found["run"]) == (
        result.message_id, "a", result.position, RUN
    )  # fmt: skip
    assert (found["kind"], found["source"], found["call_id"], found["text"]) == (
        "tool_result", "sage", "c1", "7031\n" * 3
    )  # fmt: skip
    called = await (await client.get(PATH + answer.message_id)).json()
    assert called["text"].splitlines()[0] == "Factoring."
    assert '"tool_call": "c1"' in called["text"]
    await error_of(await client.get(PATH + "msg-" + "0" * 16), 404, "not_found")
    await error_of(await client.get(PATH + "message-1"), 400, "invalid_request")
    await error_of(await client.get("/api/v0/projects/p9/messages/" + result.message_id), 404,
                   "not_found")  # fmt: skip


async def test_tool_record_survives_run_loss_and_is_scoped_and_bounded(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    from sandbox_science.orchestrator.history.operations import READ_BYTES, ToolRecord

    store = ProjectStore(state_dir)
    full = {"tool": "arithmetic", "arguments": "{}", "result": {"value": 4, "text": "whole\n"}}
    try:
        archive = Archive(store.control)
        entry = Entry("tool", "tool_result", "sample", source="arithmetic")
        saved = archive.append("p1", "a", RUN, entry, tool_record=ToolRecord(full))
        large = archive.append(
            "p1", "a", RUN, entry, tool_record=ToolRecord({"output": "x" * READ_BYTES})
        )
        old = archive.append("p1", "a", RUN, entry)
        assert all(run.name != RUN for run in store.runs("p1", "a"))
    finally:
        store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(PATH + saved.message_id + "/tool-record")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert await response.json() == full
    await error_of(await client.get(PATH + large.message_id + "/tool-record"), 422, "unsupported")
    await error_of(await client.get(PATH + old.message_id + "/tool-record"), 404, "not_found")
    await error_of(await client.get(PATH + "invalid/tool-record"), 400, "invalid_request")
    await error_of(
        await client.get("/api/v0/projects/p2/messages/" + saved.message_id + "/tool-record"),
        404,
        "not_found",
    )


async def test_large_record_chunks_are_exact_resumable_scoped_and_authenticated(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    full = {"output": "\u00e9\U0001f680\x00\n" * 160000, "value": "000001"}
    raw = json.dumps(full, ensure_ascii=False, allow_nan=False).encode()
    store = ProjectStore(state_dir)
    try:
        archive = Archive(store.control)
        saved = archive.append(
            "p1", "a", RUN, Entry("tool", "tool_result", "sample"), tool_record=ToolRecord(full)
        )
        old = archive.append("p1", "a", RUN, Entry("tool", "tool_result", "old"))
    finally:
        store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir)
    url = PATH + saved.message_id + "/tool-record/chunks"
    offset, digest = 0, hashlib.sha256()
    while True:
        response = await client.get(url, params={"offset": offset})
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert len(await response.read()) < 2 * CHUNK_BYTES
        page = await response.json()
        assert page["message"] == saved.message_id
        assert page["offset"] == offset
        assert page["total"] == len(raw)
        data = base64.b64decode(page["data_base64"], validate=True)
        assert data == raw[offset : offset + CHUNK_BYTES]
        digest.update(data)
        if offset == 0:
            assert await (await client.get(url, params={"offset": offset})).json() == page
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + len(data)
        offset = page["next_offset"]
    assert digest.digest() == hashlib.sha256(raw).digest()
    eof = await (await client.get(url, params={"offset": len(raw)})).json()
    assert eof["next_offset"] is None
    assert eof["data_base64"] == ""
    cut = raw.index("\u00e9".encode()) + 1
    partial = await (await client.get(url, params={"limit": cut})).json()
    assert base64.b64decode(partial["data_base64"]) == raw[:cut]
    await error_of(await client.get(url, params={"offset": len(raw) + 1}), 400, "invalid_request")
    await error_of(
        await client.get(PATH + old.message_id + "/tool-record/chunks"), 404, "not_found"
    )
    await error_of(await client.get(url.replace("/p1/", "/p2/")), 404, "not_found")
    await error_of(await client.get(url, data=b"unexpected body"), 400, "invalid_request")
    client.session.cookie_jar.clear()
    await error_of(await client.get(url), 401, "policy_denied")


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=65537",
        "limit=-1",
        "limit=1.5",
        "limit=1.0",
        "limit=true",
        "offset=-1",
        "offset=",
        "offset=1e2",
        "offset=+1",
        "offset=%201",
        "offset=0x10",
        "offset=4611686018427387905",
        "offset=" + "9" * 100,
        "offset=0&offset=1",
        "limit=1&limit=2",
        "unknown=1",
    ],
)
async def test_record_chunk_query_is_bounded_and_unambiguous(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, query: str
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(
        await client.get(PATH + "msg-" + "0" * 16 + "/tool-record/chunks?" + query),
        400,
        "invalid_request",
    )
