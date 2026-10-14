# SPDX-License-Identifier: MIT
"""The chat routes through HTTP: messages to a session and a run's events (RS-4, RS-15)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .route_support import (
    ClientFactory,
    assert_security_headers,
    error_of,
    origin,
    workspace,
)

SESSION = "/api/v0/projects/p1/sessions/a"


def runs(state_dir: Path) -> RunState:
    return RunState(ProjectStore(state_dir).control)


async def test_a_message_is_queued_once(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    body = {"message_id": "m-1", "text": "What is $p$?"}
    first = await client.post(SESSION + "/messages", json=body, headers=origin(client))
    assert first.status == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert_security_headers(first.headers)
    answer = await first.json()
    assert answer["message"]["state"] == "queued"
    assert answer["message"]["author"] == "user"
    assert answer["run"] is None
    again = await client.post(SESSION + "/messages", json=body, headers=origin(client))
    assert (await again.json())["message"] == answer["message"]
    other = {"message_id": "m-1", "text": "Something else"}
    await error_of(
        await client.post(SESSION + "/messages", json=other, headers=origin(client)),
        409,
        "conflict",
    )
    listed = await (await client.get(SESSION + "/messages")).json()
    assert [m["message_id"] for m in listed["messages"]] == ["m-1"]
    after = await client.get(SESSION + "/messages", params={"after": answer["message"]["number"]})
    assert (await after.json())["messages"] == []


async def test_a_message_names_the_run_that_holds_the_session(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    runs(state_dir).acquire("p1", "a", "run-x", "holder")
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.post(
        SESSION + "/messages", json={"message_id": "m-1", "text": "Hi"}, headers=origin(client)
    )
    assert (await response.json())["run"]["run"] == "run-x"


async def test_bad_messages_are_refused(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    bodies: list[Any] = [
        {"message_id": "m-1"},
        {"message_id": "m-1", "text": "x", "run": "r"},
        {"message_id": "../m", "text": "x"},
        {"message_id": "m-1", "text": 7},
        {"message_id": "m-1", "text": "   "},
        ["m-1", "x"],
    ]
    for body in bodies:
        response = await client.post(SESSION + "/messages", json=body, headers=origin(client))
        await error_of(response, 400, "invalid_request")
    unknown = "/api/v0/projects/p1/sessions/nope/messages"
    response = await client.post(
        unknown, json={"message_id": "m-1", "text": "x"}, headers=origin(client)
    )
    await error_of(response, 404, "not_found")
    await error_of(await client.get(SESSION + "/messages?after=-1"), 400, "invalid_request")


async def test_a_message_needs_the_login_and_the_page_origin(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    body = {"message_id": "m-1", "text": "Hi"}
    client = await workspace(aiohttp_client, state_dir, static_dir)
    assert (await client.post(SESSION + "/messages", json=body)).status == 403
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    response = await anonymous.post(SESSION + "/messages", json=body, headers=origin(anonymous))
    assert response.status == 401
    assert runs(state_dir).messages("p1", "a") == []


async def test_a_runs_events_are_read_by_cursor_with_its_state(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    state = runs(state_dir)
    state.acquire("p1", "a", "run-x", "holder")
    for turn in (1, 2, 3):
        state.append("p1", "a", "run-x", "model", {"turn": turn})
    client = await workspace(aiohttp_client, state_dir, static_dir)
    events = SESSION + "/runs/run-x/events"
    page = await (await client.get(events, params={"after": "1", "limit": "1"})).json()
    assert page["state"] == "active"
    assert [(e["seq"], e["kind"], e["data"]) for e in page["events"]] == [(2, "model", {"turn": 2})]
    state.release("p1", "a", "holder")
    assert (await (await client.get(events)).json())["state"] == "interrupted"
    state.append("p1", "a", "run-x", "stopped", {"stopped": "answered"})
    assert (await (await client.get(events)).json())["state"] == "ended"
    await error_of(await client.get(events, params={"limit": "201"}), 400, "invalid_request")
