# SPDX-License-Identifier: MIT
"""The ordinary authenticated read surface exports one scoped stored-evidence snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.export import EvidenceExport
from sandbox_science.orchestrator.context.history.operations import ToolRecord
from sandbox_science.orchestrator.projects import ProjectStore

from .route_support import ClientFactory, error_of, workspace
from .test_routes_messages import RUN

PATH = f"/api/v0/projects/p1/sessions/a/runs/{RUN}/evidence"


async def test_export_and_scoped_refusals(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    store = ProjectStore(state_dir)
    try:
        Archive(store.control).append(
            "p1",
            "a",
            RUN,
            Entry("tool", "tool_result", "4 (sample)"),
            tool_record=ToolRecord({"value": 4, "output": "\u03b1\x00😀" * 30000}),
        )
    finally:
        store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(PATH)
    assert response.status == 200
    assert response.headers["Content-Type"] == "application/x-ndjson"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Disposition"].endswith('.evidence.ndjson"')
    digest, count, complete = hashlib.sha256(), 0, False
    async for line in response.content:
        parsed = json.loads(line)
        if parsed["kind"] == "complete":
            assert parsed["sha256"] == digest.hexdigest()
            assert parsed["lines"] == count
            complete = True
        else:
            assert not complete
            digest.update(line)
            count += 1
    assert complete
    await error_of(await client.get(PATH + "?offset=0"), 400, "invalid_request")
    await error_of(await client.get(PATH.replace("p1", "p9")), 404, "not_found")
    await error_of(
        await client.get(PATH.replace("sessions/a", "sessions/missing")), 404, "not_found"
    )
    await error_of(await client.get(PATH.replace(RUN, RUN + "missing")), 404, "not_found")
    await error_of(await client.get(PATH.replace(RUN, "invalid")), 400, "invalid_request")
    assert (await client.head(PATH)).status == 404
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    await error_of(await anonymous.get(PATH), 401, "policy_denied")


async def test_exports_limit_concurrency_and_release_after_disconnect(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProjectStore(state_dir)
    try:
        Archive(store.control).append("p1", "a", RUN, Entry("user", "task", "Fixture"))
    finally:
        store.close()
    loop = asyncio.get_running_loop()
    entered: asyncio.Queue[None] = asyncio.Queue()
    release = threading.Event()

    def blocked(export: EvidenceExport) -> Iterator[bytes]:
        loop.call_soon_threadsafe(entered.put_nowait, None)
        assert release.wait(10), "test failed to release the export"
        yield b"{}\n"

    monkeypatch.setattr(EvidenceExport, "lines", blocked)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    try:
        first = await client.get(PATH)
        second = await client.get(PATH)
        await entered.get()
        await entered.get()
        assert (await client.get(PATH)).status == 429
        first.close()
    finally:
        release.set()
    assert await second.read() == b"{}\n"
    # The disconnected stream also releases its slot after its owned read ends.
    third = await client.get(PATH)
    assert third.status == 200
    assert await third.read() == b"{}\n"
