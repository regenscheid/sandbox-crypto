# SPDX-License-Identifier: MIT
"""Paged run metadata and exact bytes behind compact final-record references."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from sandbox_science.orchestrator.context.history.collections import Collection
from sandbox_science.orchestrator.projects import ProjectStore

from .route_support import ClientFactory, error_of, workspace

RUN = "20261016T030000000000Z-run-1"
PATH = f"/api/v0/projects/p1/sessions/a/runs/{RUN}/history"


async def test_history_pages_are_bounded_scoped_and_exact(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
) -> None:
    store = ProjectStore(state_dir)
    try:
        values = Collection(store.control, ("p1", "a", RUN), "fixture")
        for n in range(105):
            values.append({"n": n})
        large = Collection(store.control, ("p1", "a", RUN), "large")
        original = {"text": "é\x00🚀" * 50000}
        large.append(original)
    finally:
        store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(PATH + "?collection=fixture&limit=100")
    assert response.status == 200
    page = await response.json()
    assert len(page["items"]) == 100
    assert page["next_offset"] == 100
    following = await (await client.get(PATH + "?collection=fixture&offset=100")).json()
    assert following["items"][-1]["value"] == {"n": 104}
    assert following["next_offset"] is None
    await error_of(await client.get(PATH + "?collection=large"), 422, "unsupported")
    data = bytearray()
    offset = 0
    while True:
        response = await client.get(PATH + f"/chunks?collection=large&position=0&offset={offset}")
        assert response.status == 200
        chunk = await response.json()
        decoded = base64.b64decode(chunk["data_base64"], validate=True)
        assert len(decoded) <= 65536
        data.extend(decoded)
        if chunk["next_offset"] is None:
            break
        offset = chunk["next_offset"]
    assert json.loads(data) == original
    await error_of(
        await client.get(PATH.replace("sessions/a", "sessions/missing") + "?collection=fixture"),
        404,
        "not_found",
    )
    for query in ("limit=101", "limit=-1", "offset=1&offset=2", "unknown=1", "collection=../bad"):
        await error_of(await client.get(PATH + "?" + query), 400, "invalid_request")
