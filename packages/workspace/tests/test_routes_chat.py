# SPDX-License-Identifier: MIT
"""The chat routes through HTTP: messages to a session and a run's events (RS-4, RS-15)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from sandbox_science.contracts import EnvironmentState
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.workspace.api import TIMEOUTS

from .route_support import (
    ClientFactory,
    FakeWorker,
    ServerFactory,
    assert_security_headers,
    environment_of,
    error_of,
    fake_worker,
    origin,
    refusing_port,
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


async def test_the_events_of_an_unknown_run_are_not_found(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(await client.get(SESSION + "/runs/no-such-run/events"), 404, "not_found")


async def test_resume_and_cancel_are_written_for_the_service(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    state = runs(state_dir)
    state.post("p1", "a", "m-1", "Held.", author="user")
    state.hold("p1", "a", ["m-1"])
    client = await workspace(aiohttp_client, state_dir, static_dir)
    listed = await (await client.get(SESSION + "/messages")).json()
    assert listed["service"] is False
    state.beat("a-service")
    assert (await (await client.get(SESSION + "/messages")).json())["service"] is True
    resumed = await client.post(SESSION + "/resume", json={}, headers=origin(client))
    assert await resumed.json() == {"requested": True, "resumed": 1}
    assert [m.state for m in state.messages("p1", "a")] == ["queued"]
    cancel = SESSION + "/runs/run-x/cancel"
    answered = await client.post(cancel, json={}, headers=origin(client))
    assert await answered.json() == {"requested": True, "resumed": 0}
    assert state.cancelled("p1", "a", "run-x")
    await error_of(
        await client.post(cancel, json={"now": True}, headers=origin(client)),
        400,
        "invalid_request",
    )
    assert (await client.post(cancel, json={})).status == 403  # not from the page


async def test_a_waiting_message_says_what_the_worker_is_making(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    # The first run of a project waits for its sandbox: no lease, no events, only a queued
    # message, until the worker has made it. The listing says what the worker records.
    state = runs(state_dir)
    state.beat("a-service")
    state.post("p1", "a", "m-1", "What is $p$?", author="user")
    worker = FakeWorker()
    worker.environment = EnvironmentState.CREATING
    client = await workspace(
        aiohttp_client, state_dir, static_dir, worker_url=await fake_worker(aiohttp_server, worker)
    )
    listed = await (await client.get(SESSION + "/messages")).json()
    assert listed["environment"] == "creating"
    assert [m["state"] for m in listed["messages"]] == ["queued"]
    # Only the worker's record is read, for the session's own environment: nothing starts.
    environment = environment_of(state_dir, "p1", "a")
    assert worker.calls == [("GET", f"/v1/environments/{environment}", {})]
    worker.environment = EnvironmentState.READY
    listed = await (await client.get(SESSION + "/messages")).json()
    assert listed["environment"] == "ready"


async def test_the_environment_is_asked_only_while_a_message_waits_for_a_run(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    state = runs(state_dir)
    worker = FakeWorker()
    worker.environment = EnvironmentState.CREATING
    client = await workspace(
        aiohttp_client, state_dir, static_dir, worker_url=await fake_worker(aiohttp_server, worker)
    )

    async def asked() -> tuple[object, int]:
        before = len(worker.calls)
        listed = await (await client.get(SESSION + "/messages")).json()
        return listed["environment"], len(worker.calls) - before

    assert await asked() == (None, 0)  # nothing waits
    state.post("p1", "a", "m-1", "Hi", author="user")
    assert await asked() == (None, 0)  # no run service will start it
    state.beat("a-service")
    assert await asked() == ("creating", 1)
    state.acquire("p1", "a", "run-x", "holder")
    assert await asked() == (None, 0)  # a run holds the session: it has its events
    state.release("p1", "a", "holder")
    assert await asked() == ("creating", 1)
    state.deliver("p1", "a", ["m-1"], "run-x")
    state.post("p1", "a", "m-2", "Again", author="user")
    state.hold("p1", "a", ["m-2"])
    assert await asked() == (None, 0)  # delivered and held messages wait for no run


async def test_a_worker_that_does_not_say_leaves_the_state_unknown(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    # The chat reads this at every poll, so the worker's trouble never becomes the chat's.
    state = runs(state_dir)
    state.beat("a-service")
    state.post("p1", "a", "m-1", "Hi", author="user")

    async def listed(client: Any) -> dict[str, Any]:
        response = await client.get(SESSION + "/messages")
        assert response.status == 200
        return await response.json()

    absent = FakeWorker()  # knows no such environment: not made yet
    client = await workspace(
        aiohttp_client, state_dir, static_dir, worker_url=await fake_worker(aiohttp_server, absent)
    )
    assert (await listed(client))["environment"] is None
    assert len(absent.calls) == 1

    refusing = FakeWorker()
    refusing.refusal = (503, "unavailable")
    client = await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        worker_url=await fake_worker(aiohttp_server, refusing),
    )
    assert (await listed(client))["environment"] is None

    with refusing_port() as url:
        client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
        assert (await listed(client))["environment"] is None

    slow = FakeWorker()
    slow.environment = EnvironmentState.CREATING
    slow.environment_delay = 5.0
    client = await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        worker_url=await fake_worker(aiohttp_server, slow),
        timeouts=replace(TIMEOUTS, environment=0.05),
    )
    answer = await listed(client)
    assert answer["environment"] is None
    assert [m["message_id"] for m in answer["messages"]] == ["m-1"]

    client = await workspace(aiohttp_client, state_dir, static_dir)  # no worker configured
    assert (await listed(client))["environment"] is None
