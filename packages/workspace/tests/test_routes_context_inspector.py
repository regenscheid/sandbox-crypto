# SPDX-License-Identifier: MIT
"""The inspector searches retained original messages, not the folded working view."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.usage import AnsweredUsage
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.testing import FakeClock

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


async def test_continuation_scope_uses_work_identity_not_parent_tree_or_run_name(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    from sandbox_science.orchestrator.continuity.records import WorkRecords
    from sandbox_science.orchestrator.runstate import RunState

    projects = ProjectStore(state_dir)
    works = WorkRecords(projects.control)
    root = "20261018T000000000000Z-root"
    child = "20261018T000001000000Z-child"
    sibling = "20261018T000002000000Z-sibling"
    next_root = "20261018T010000000000Z-root"
    next_child = "20261018T010001000000Z-child"
    works.admit("p1", "a", root)
    works.admit("p1", "a", child, parent=root)
    works.admit("p1", "a", sibling, parent=root)
    works.interrupt("p1", "a", root)
    works.interrupt("p1", "a", child)
    works.interrupt("p1", "a", sibling)
    request = works.request("p1", "a")
    assert request is not None
    RunState(projects.control).acquire("p1", "a", next_root, "fixture")
    _, epoch = works.claim("p1", "a", request, lease_run=next_root)
    works.admit("p1", "a", next_root, work=root, predecessor=root, epoch=epoch)
    works.admit("p1", "a", next_child, work=child, predecessor=child, epoch=epoch)
    archive = Archive(projects.control)
    saved = {}
    for run in (root, child, sibling, next_root, next_child):
        saved[run] = archive.append("p1", "a", run, Entry("user", "task", f"NEEDLE {run}"))
    # Same run name in a different session must not expand the search.
    projects.open_session("p1", "other", dedicated=False)
    archive.append("p1", "other", child, Entry("user", "task", "NEEDLE other session"))
    client = await workspace(aiohttp_client, state_dir, static_dir)
    base = f"/api/v0/projects/p1/sessions/a/runs/{next_child}/context"
    for selected in (child, next_child):
        path = base.replace(next_child, selected)
        page = await (await client.get(path + "/history?scope=agent&contains=NEEDLE")).json()
        assert page["scope"] == "agent"
        assert [item["message"] for item in page["messages"]] == [
            saved[child].message_id,
            saved[next_child].message_id,
        ]
    single = await (await client.get(base + "/history")).json()
    assert [item["run"] for item in single["messages"]] == [next_child]
    first = await (await client.get(base + "/attempts?limit=1")).json()
    assert [item["run"] for item in first["attempts"]] == [child]
    assert first["next_after"] == child
    assert first["attempts"][0]["saved"] is False
    second = await (await client.get(base + "/attempts", params={"after": child})).json()
    assert second["next_after"] is None
    assert second["attempts"][0]["run"] == next_child
    assert second["attempts"][0]["predecessor"] == child
    assert second["attempts"][0]["epoch"] == epoch
    await error_of(await client.get(base + "/history?scope=tree"), 400, "invalid_request")
    await error_of(await client.get(base + "/attempts?limit=51"), 400, "invalid_request")
    await error_of(await client.get(base + "/attempts?after=a&after=b"), 400, "invalid_request")
    await client.post("/api/v0/logout", headers={"Origin": str(client.make_url("/")).rstrip("/")})
    assert (await client.get(base + "/attempts")).status == 401
    projects.close()


async def test_legacy_attempt_does_not_guess_continuations(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    runs = list(projects.iter_runs("p1", "a"))
    selected, other = runs[0].name, runs[1].name
    archive = Archive(projects.control)
    for run in (selected, other):
        archive.append("p1", "a", run, Entry("user", "task", "match"))
    client = await workspace(aiohttp_client, state_dir, static_dir)
    base = f"/api/v0/projects/p1/sessions/a/runs/{selected}/context"
    page = await (await client.get(base + "/history?scope=agent")).json()
    assert [item["run"] for item in page["messages"]] == [selected]
    attempts = await (await client.get(base + "/attempts")).json()
    assert attempts["attempts"] == [
        {"run": selected, "predecessor": None, "epoch": None, "outcome": None, "saved": True}
    ]
    projects.close()


async def test_sparse_agent_search_advances_across_bounded_session_slices(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    runs = list(projects.iter_runs("p1", "a"))
    selected, other = runs[0].name, runs[1].name
    archive = Archive(projects.control)
    for _ in range(70):
        archive.append("p1", "a", other, Entry("user", "task", "unrelated"))
    saved = archive.append("p1", "a", selected, Entry("user", "task", "match"))
    client = await workspace(aiohttp_client, state_dir, static_dir)
    base = f"/api/v0/projects/p1/sessions/a/runs/{selected}/context/history"
    first = await (await client.get(base + "?scope=agent&contains=match")).json()
    assert first["messages"] == []
    assert first["next_after"] is not None
    second = await (
        await client.get(
            base, params={"scope": "agent", "contains": "match", "after": first["next_after"]}
        )
    ).json()
    assert [item["message"] for item in second["messages"]] == [saved.message_id]
    assert second["next_after"] is None
    projects.close()


async def test_live_timeline_pages_one_attempt_and_advances_through_sparse_fold_events(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    selected, sibling = (run.name for run in list(projects.iter_runs("p1", "a"))[:2])
    clock = FakeClock(datetime(2026, 10, 18, 12, tzinfo=UTC))
    archive = Archive(projects.control, clock=clock)
    state = RunState(projects.control, clock=clock)
    for ordinal in range(1, 56):
        archive.append(
            "p1",
            "a",
            selected,
            Entry("assistant", "assistant", f"Answer {ordinal}"),
            model_usage=AnsweredUsage(
                ordinal=ordinal,
                observed_at=clock.now(),
                usage=ModelUsage(
                    input_tokens=100 + ordinal,
                    output_tokens=2,
                    cache_read_input_tokens=10 if ordinal % 2 else None,
                ),
            ),
        )
        state.append("p1", "a", selected, "notice", {"source": "fixture"})
    fold = archive.append("p1", "a", selected, Entry("user", "fold", "saved fold"))
    state.append(
        "p1",
        "a",
        selected,
        "fold",
        {"state": "skeleton_only", "fold": fold.message_id, "span": [1, 4], "freed": 12},
    )
    archive.append(
        "p1",
        "a",
        sibling,
        Entry("assistant", "assistant", "Other answer"),
        model_usage=AnsweredUsage(
            ordinal=1, observed_at=clock.now(), usage=ModelUsage(input_tokens=999)
        ),
    )
    client = await workspace(aiohttp_client, state_dir, static_dir)
    path = f"/api/v0/projects/p1/sessions/a/runs/{selected}/context/timeline"
    first = await (await client.get(path)).json()
    assert len(first["answers"]) == 50
    assert first["answers"][0]["ordinal"] == 1
    assert first["answers"][0]["observed_at"].endswith("+00:00")
    assert first["answers"][0]["usage"]["input_tokens"] == 101
    assert first["answers"][1]["usage"]["cache_read_input_tokens"] is None
    assert first["folds"] == []
    assert first["answers_after"] == 50
    assert first["events_after"] == 50
    assert first["more_answers"]
    assert first["more_events"]
    later = await (await client.get(path, params={"answers_after": 50, "events_after": 50})).json()
    assert [answer["ordinal"] for answer in later["answers"]] == [51, 52, 53, 54, 55]
    assert later["folds"] == [
        {
            "seq": 56,
            "at": clock.now().isoformat(),
            "state": "skeleton_only",
            "fold": fold.message_id,
            "span_start": 1,
            "span_end": 4,
            "freed_estimate": 12,
            "tokens_estimate": None,
        }
    ]
    assert not later["more_answers"]
    assert not later["more_events"]
    tail = await (await client.get(path, params={"tail": "1"})).json()
    assert [answer["ordinal"] for answer in tail["answers"]] == list(range(6, 56))
    assert tail["folds"] == later["folds"]
    assert tail["events_after"] == 56
    assert not tail["more_answers"]
    assert not tail["more_events"]
    for invalid in ("limit=51", "answers_after=-1", "events_after=1&events_after=2", "x=1"):
        await error_of(await client.get(path + "?" + invalid), 400, "invalid_request")
    await error_of(
        await client.get(path.replace("sessions/a", "sessions/missing")), 404, "not_found"
    )
    await client.post("/api/v0/logout", headers={"Origin": str(client.make_url("/")).rstrip("/")})
    assert (await client.get(path)).status == 401
    projects.close()


async def test_auxiliary_timeline_snapshot_rechecks_equal_time_intents_and_late_settlement(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    selected = "20261018T120000000000Z-run-live"
    clock = FakeClock(datetime(2026, 10, 18, 12, tzinfo=UTC))
    owner = WorkRecords(projects.control).admit("p1", "a", selected)
    intents = ModelIntents(projects.control, clock=clock)
    first = intents.begin(owner, kind="fold", alias="fixture")
    client = await workspace(aiohttp_client, state_dir, static_dir)
    path = f"/api/v0/projects/p1/sessions/a/runs/{selected}/context/timeline"
    pending = await (await client.get(path)).json()
    assert [(item["identity"], item["usage"]) for item in pending["auxiliary"]] == [(first, None)]
    # Neither equal creation timestamps nor settlement of a previously returned
    # identity may be missed by a live append-only random-ID cursor.
    second = intents.begin(owner, kind="reviewer", alias="fixture")
    intents.observe(owner, first, ModelUsage(input_tokens=42, output_tokens=3))
    updated = await (await client.get(path)).json()
    assert {item["identity"] for item in updated["auxiliary"]} == {first, second}
    assert (
        next(item for item in updated["auxiliary"] if item["identity"] == first)["usage"][
            "input_tokens"
        ]
        == 42
    )
    assert (
        next(item for item in updated["auxiliary"] if item["identity"] == second)["usage"] is None
    )
    clock.advance(1)
    for _ in range(51):
        intents.begin(owner, kind="fold", alias="fixture")
    bounded = await (await client.get(path)).json()
    assert len(bounded["auxiliary"]) == 50
    assert bounded["more_auxiliary"]
    assert first not in {item["identity"] for item in bounded["auxiliary"]}
    projects.close()


async def test_captured_context_live_parts_chunks_and_scope_are_authenticated(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    from sandbox_science.orchestrator.history.capture import request

    projects = ProjectStore(state_dir)
    run = "20261018T120000000000Z-run-captured"
    owner = WorkRecords(projects.control).admit("p1", "a", run)
    identity = ModelIntents(projects.control).begin(
        owner,
        kind="main",
        alias="fixture",
        captured=request(
            [
                {"role": "system", "content": "Pinned source"},
                {"role": "user", "content": "x" * 40000},
            ],
            [],
        ),
    )
    client = await workspace(aiohttp_client, state_dir, static_dir)
    base = f"/api/v0/projects/p1/sessions/a/runs/{run}/context/captures"
    listing = await (await client.get(base)).json()
    assert [item["identity"] for item in listing["calls"]] == [identity]
    assert listing["calls"][0]["captured"]
    parts = await (await client.get(f"{base}/{identity}/parts")).json()
    assert [part["role"] for part in parts["parts"]] == ["system", "user", "tools"]
    first = await (await client.get(f"{base}/{identity}/parts/1")).json()
    assert len(first["text"]) == 16384
    assert first["next_offset"] == 16384
    second = await (await client.get(f"{base}/{identity}/parts/1?offset=16384")).json()
    assert second["sha256"] == first["sha256"]
    other = await (await client.get(base.replace(run, "20261018T120001000000Z-other"))).json()
    assert other["calls"] == []
    await error_of(await client.get(f"{base}/{identity}/parts/1?offset=-1"), 400, "invalid_request")
    await error_of(await client.get(f"{base}/{identity}/parts/nope"), 400, "invalid_request")
    await error_of(
        await client.get(base.replace("sessions/a", "sessions/missing")), 404, "not_found"
    )
    await client.post("/api/v0/logout", headers={"Origin": str(client.make_url("/")).rstrip("/")})
    assert (await client.get(base)).status == 401
    projects.close()


async def test_indexed_history_reports_pending_and_explicit_backfill_preserves_original(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    run = next(projects.iter_runs("p1", "a")).name
    source = "x" * 300000 + "literal-evidence-after-long-body"
    saved = Archive(projects.control).append(
        "p1", "a", run, Entry("tool", "tool_result", source, source="history_read")
    )
    client = await workspace(aiohttp_client, state_dir, static_dir)
    base = f"/api/v0/projects/p1/sessions/a/runs/{run}/context/history"
    params = {"contains": "literal-evidence", "indexed": "1"}
    first = await (await client.get(base, params=params)).json()
    assert first["index_pending"]
    assert first["indexed"]
    assert first["messages"] == []
    assert (await client.post(base + "/index")).status == 403
    origin = {"Origin": str(client.make_url("/")).rstrip("/")}
    progress = await (await client.post(base + "/index", headers=origin)).json()
    assert progress["chunks"] <= 16
    assert not progress["pending"]
    found = await (await client.get(base, params=params)).json()
    assert [m["message"] for m in found["messages"]] == [saved.message_id]
    assert not found["index_pending"]
    assert Archive(projects.control).message("p1", saved.message_id).entry.content == source
    await error_of(
        await client.post(base + "/index?scope=tree", headers=origin), 400, "invalid_request"
    )
    projects.close()
