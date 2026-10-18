# SPDX-License-Identifier: MIT
"""The inspector searches retained original messages, not the folded working view."""

from __future__ import annotations

from pathlib import Path

from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.projects import ProjectStore

from .route_support import ClientFactory, error_of, workspace


async def test_original_history_pages_filters_and_searches_the_selected_attempt(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    runs = list(projects.iter_runs("p1", "a"))
    selected = runs[0].name
    other = runs[1].name
    archive = Archive(projects.control)
    task = archive.append(
        "p1", "a", selected, Entry(role="user", kind="task", content="SNOVA setup")
    )
    answer = archive.append(
        "p1",
        "a",
        selected,
        Entry(role="assistant", kind="assistant", content="Search the literature"),
    )
    result = archive.append(
        "p1",
        "a",
        selected,
        Entry(role="tool", kind="tool_result", source="sage", content="exact candidate 42"),
    )
    archive.append("p1", "a", selected, Entry(role="user", kind="fold", content="hidden 42"))
    archive.append("p1", "a", other, Entry(role="user", kind="task", content="other 42"))
    client = await workspace(aiohttp_client, state_dir, static_dir)
    path = f"/api/v0/projects/p1/sessions/a/runs/{selected}/context/history"
    first = await (await client.get(path + "?limit=2")).json()
    assert [message["message"] for message in first["messages"]] == [
        task.message_id,
        answer.message_id,
    ]
    assert first["next_after"] == answer.position
    later = await (await client.get(path + f"?after={answer.position}")).json()
    assert [message["message"] for message in later["messages"]] == [result.message_id]
    assert later["next_after"] is None
    found = await (await client.get(path + "?contains=42&role=tool")).json()
    assert [message["message"] for message in found["messages"]] == [result.message_id]
    assert "candidate 42" in found["messages"][0]["snippet"]
    assert (await (await client.get(path + "?contains=hidden")).json())["messages"] == []
    with_derived = await (await client.get(path + "?contains=hidden&include_derived=1")).json()
    assert len(with_derived["messages"]) == 1
    for invalid in ("limit=51", "contains=x&contains=y", "unknown=1", "role=system"):
        await error_of(await client.get(path + "?" + invalid), 400, "invalid_request")
    await error_of(
        await client.get(path.replace("sessions/a", "sessions/missing")), 404, "not_found"
    )


async def test_no_match_search_advances_in_bounded_slices_and_large_text_stays_openable(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    selected = next(projects.iter_runs("p1", "a")).name
    archive = Archive(projects.control)
    for _ in range(70):
        archive.append("p1", "a", selected, Entry(role="user", kind="task", content="ordinary"))
    large = archive.append(
        "p1", "a", selected, Entry(role="user", kind="task", content="x" * 1100000 + "NEEDLE")
    )
    client = await workspace(aiohttp_client, state_dir, static_dir)
    path = f"/api/v0/projects/p1/sessions/a/runs/{selected}/context/history"
    first = await (await client.get(path + "?contains=NEEDLE")).json()
    assert first["messages"] == []
    assert first["next_after"] is not None
    second = await (
        await client.get(path, params={"contains": "NEEDLE", "after": first["next_after"]})
    ).json()
    assert [item["message"] for item in second["messages"]] == [large.message_id]
    assert second["next_after"] is None
    await error_of(
        await client.get(path.replace("/p1/", "/p2/")),
        404,
        "not_found",
    )


async def test_oversized_tool_call_search_gap_is_reported(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    selected = next(projects.iter_runs("p1", "a")).name
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "sage", "arguments": "x" * (8 * 1024 * 1024) + "NEEDLE"},
    }
    saved = Archive(projects.control).append(
        "p1",
        "a",
        selected,
        Entry(role="assistant", kind="assistant", content="small", tool_calls=(call,)),
    )
    client = await workspace(aiohttp_client, state_dir, static_dir)
    path = f"/api/v0/projects/p1/sessions/a/runs/{selected}/context/history"
    found = await (await client.get(path, params={"contains": "NEEDLE"})).json()
    assert found["messages"] == []
    assert found["unsearched_oversized_tool_calls"] == 1
    page = await (
        await client.get(f"/api/v0/projects/p1/messages/{saved.message_id}/chunks?field=tool_calls")
    ).json()
    assert page["total"] > 8 * 1024 * 1024


async def test_oversized_tool_call_gap_is_counted_on_the_page_that_passes_it(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    selected = next(projects.iter_runs("p1", "a")).name
    archive = Archive(projects.control)
    for index in range(20):
        archive.append("p1", "a", selected, Entry("user", "task", f"NEEDLE {index}"))
    call = {
        "id": "call-gap",
        "type": "function",
        "function": {"name": "sage", "arguments": "x" * (8 * 1024 * 1024) + "NEEDLE"},
    }
    archive.append(
        "p1",
        "a",
        selected,
        Entry("assistant", "assistant", "small", tool_calls=(call,)),
    )
    after_gap = archive.append("p1", "a", selected, Entry("user", "task", "NEEDLE later"))
    client = await workspace(aiohttp_client, state_dir, static_dir)
    path = f"/api/v0/projects/p1/sessions/a/runs/{selected}/context/history"
    first = await (await client.get(path, params={"contains": "NEEDLE", "limit": "20"})).json()
    assert len(first["messages"]) == 20
    assert first["unsearched_oversized_tool_calls"] == 0
    assert first["next_after"] is not None
    second = await (
        await client.get(path, params={"contains": "NEEDLE", "after": first["next_after"]})
    ).json()
    assert [item["message"] for item in second["messages"]] == [after_gap.message_id]
    assert second["unsearched_oversized_tool_calls"] == 1
