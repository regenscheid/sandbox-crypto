# SPDX-License-Identifier: MIT
"""An archived message through HTTP (context management, CM-3; consistency review,
CR-14 as amended): read whole by its ID, as ``history_read`` gives it, for the review
views' links."""

from __future__ import annotations

from pathlib import Path

from sandbox_science.orchestrator.history import Archive, Entry
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
