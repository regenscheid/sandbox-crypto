# SPDX-License-Identifier: MIT
"""The project's file settings over the workspace API (research artifacts, RA-29, RA-39)."""

from __future__ import annotations

import pytest

from .test_reviews import client as client

pytestmark = pytest.mark.asyncio


async def test_read_and_change_the_file_settings(client):
    base = "/api/v0/projects/p1/files/settings"
    assert (await client.get("/api/v0/projects/missing/files/settings")).status == 404
    shown = await (await client.get(base)).json()
    assert (shown["revision"], shown["recording"]) == (0, "all")
    assert shown["ignore"] == shown["defaults"]
    value = {"expected": 0, "recording": "results", "ignore": ["*.h5"], "operation_id": "o1"}
    assert (await client.post(base, json=value)).status == 403  # no same-origin proof
    headers = {"Origin": str(client.make_url("/")).rstrip("/")}
    changed = await client.post(base, json=value, headers=headers)
    assert changed.status == 200
    body = await changed.json()
    assert (body["revision"], body["recording"], body["ignore"], body["replayed"]) == (
        1, "results", ["*.h5"], False,
    )  # fmt: skip
    stale = await client.post(base, json={**value, "operation_id": "o2"}, headers=headers)
    assert stale.status == 409
    bad = {**value, "ignore": ["!x"], "operation_id": "o3", "expected": 1}
    assert (await client.post(base, json=bad, headers=headers)).status == 400
