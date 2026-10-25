# SPDX-License-Identifier: MIT
"""A session's agent messages through HTTP (PD-44; B10): listed oldest first by an
exclusive message cursor, at most ``limit`` a page, read-only, in their own session."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sandbox_science.orchestrator.agent_messages import AgentMessage, AgentMessageStore
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .route_support import ClientFactory, error_of, origin, workspace

SESSION = "/api/v0/projects/p1/sessions/a"
ROOT = "20261025T120000000000Z-root"
CHILD = "20261025T120001000000Z-child"
DELEGATION = "dlg-" + "1" * 24


def send(state_dir: Path, count: int) -> list[AgentMessage]:
    """``count`` messages between the root and its running child, alternating."""
    store = ProjectStore(state_dir)
    try:
        RunState(store.control).acquire("p1", "a", ROOT, "holder")
        DelegationRecords(store.control).start(
            "p1",
            "a",
            Delegation(
                delegation_id=DELEGATION,
                parent=ROOT,
                run=CHILD,
                kind="task",
                brief={"task": "Count the points."},
                started_at=datetime.now(UTC),
            ),
        )
        mailbox = AgentMessageStore(store.control)
        return [
            mailbox.send_child("p1", "a", ROOT, None, DELEGATION, f"Down {n}", f"op-down-{n}")
            if n % 2 == 0
            else mailbox.send_parent("p1", "a", CHILD, DELEGATION, f"Up {n}", f"op-up-{n}")
            for n in range(count)
        ]
    finally:
        store.close()


async def test_a_sessions_messages_are_listed_oldest_first_with_their_fields(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    sent = send(state_dir, 2)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(SESSION + "/agent-messages")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    page = await response.json()
    assert page["has_more"] is False
    assert page["next_after"] == sent[1].message_id
    down, up = page["messages"]
    assert down == {
        "message_id": sent[0].message_id,
        "sender_run": ROOT,
        "sender_delegation": None,
        "recipient_delegation": DELEGATION,
        "body": "Down 0",
        "created_at": sent[0].created_at,
        "delivered_run": None,
        "references": [],
    }
    assert (up["sender_run"], up["sender_delegation"], up["recipient_delegation"]) == (
        CHILD,
        DELEGATION,
        None,
    )
    assert up["body"] == "Up 1"


async def test_the_cursor_and_the_limit_page_the_messages(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    sent = send(state_dir, 5)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    first = await (await client.get(SESSION + "/agent-messages?limit=2")).json()
    assert [m["body"] for m in first["messages"]] == ["Down 0", "Up 1"]
    assert (first["next_after"], first["has_more"]) == (sent[1].message_id, True)
    second = await (
        await client.get(SESSION + f"/agent-messages?after={first['next_after']}&limit=2")
    ).json()
    assert [m["body"] for m in second["messages"]] == ["Down 2", "Up 3"]
    assert second["has_more"] is True
    last = await (
        await client.get(SESSION + f"/agent-messages?after={second['next_after']}")
    ).json()
    assert [m["body"] for m in last["messages"]] == ["Down 4"]
    assert (last["next_after"], last["has_more"]) == (sent[4].message_id, False)
    # After the last one, an empty page keeps the cursor, for the next read.
    empty = await (await client.get(SESSION + f"/agent-messages?after={sent[4].message_id}")).json()
    assert empty == {"messages": [], "next_after": sent[4].message_id, "has_more": False}


async def test_a_session_without_messages_lists_none(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    page = await (await client.get(SESSION + "/agent-messages")).json()
    assert page == {"messages": [], "next_after": None, "has_more": False}


async def test_messages_stay_in_their_session_and_an_unknown_session_is_not_found(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    sent = send(state_dir, 1)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    other = await (await client.get("/api/v0/projects/p1/sessions/c/agent-messages")).json()
    assert other["messages"] == []
    # A cursor of another session's message is not found here.
    await error_of(
        await client.get(
            f"/api/v0/projects/p1/sessions/c/agent-messages?after={sent[0].message_id}"
        ),
        404,
        "not_found",
    )
    await error_of(
        await client.get("/api/v0/projects/p1/sessions/nope/agent-messages"), 404, "not_found"
    )
    await error_of(
        await client.get("/api/v0/projects/nope/sessions/a/agent-messages"), 404, "not_found"
    )


async def test_bad_paging_is_refused_and_the_route_only_reads(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    send(state_dir, 1)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    for query in (
        "limit=0",
        "limit=101",
        "limit=-1",
        "limit=1.5",
        "after=msg-0000000000000000",
        "after=agm-XYZ",
        "after=",
        "other=1",
        "limit=2&limit=3",
    ):
        await error_of(
            await client.get(SESSION + f"/agent-messages?{query}"), 400, "invalid_request"
        )
    await error_of(
        await client.get(SESSION + "/agent-messages?after=agm-" + "0" * 24), 404, "not_found"
    )
    refused = await client.post(SESSION + "/agent-messages", json={}, headers=origin(client))
    assert refused.status in (404, 405)
    page = await (await client.get(SESSION + "/agent-messages?limit=100")).json()
    assert len(page["messages"]) == 1
