# SPDX-License-Identifier: MIT
"""The usage view's routes (D102): a session's spend by agent and model against its soft
caps with its check-ins, and a project's spend by session over a window. Both read only,
authenticated, scoped to their project, and strict about their query."""

from datetime import UTC, datetime
from pathlib import Path

from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.usage import AnsweredUsage
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.resources import (
    CheckInSummary,
    ResourceDecisionRequest,
    ResourceRequests,
)
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .route_support import ClientFactory, error_of, origin, workspace
from .test_routes_recovery import CHILD, ROOT, RUN
from .test_routes_resources import seed_resources

PROJECT = "/api/v0/projects/p1/usage/view"
SESSION = ROOT.removesuffix("/recovery") + "/usage/view"


def answer(store: ProjectStore, run: str, ordinal: int, *, cost: float | None, model: str) -> None:
    Archive(store.control).append(
        "p1",
        "a",
        run,
        Entry("assistant", "assistant", f"answer {ordinal}"),
        model_usage=AnsweredUsage(
            ordinal=ordinal,
            observed_at=datetime.now(UTC),
            usage=ModelUsage(model=model, input_tokens=100, output_tokens=10, cost=cost),
        ),
        parent=RUN if run == CHILD else None,
    )


def seed_usage(state_dir: Path) -> None:
    seed_resources(state_dir)
    store = ProjectStore(state_dir)
    try:
        answer(store, RUN, 1, cost=1.5, model="big")
        answer(store, RUN, 2, cost=None, model="big")
        answer(store, CHILD, 1, cost=0.5, model="small")
        works = WorkRecords(store.control)
        child = works.owner("p1", "a", CHILD)
        root = works.owner("p1", "a", RUN)
        assert child is not None
        assert root is not None
        with store.control.write() as tx:
            tx.sql.execute("UPDATE research_work SET state='running' WHERE work=?", (CHILD,))
        intents = ModelIntents(store.control)
        identity = intents.begin(child, kind="reviewer", alias="fixture")
        intents.observe(
            child,
            identity,
            ModelUsage(model="judge", input_tokens=100, output_tokens=10, cost=0.25),
        )
        requests = ResourceRequests(store.control)
        loop = requests.observe(
            child,
            "loop",
            used=8,
            total=8,
            revision=0,
            force=True,
            summary=CheckInSummary(
                rule="same_call", detail="8 calls of shell with the same arguments"
            ),
        )
        assert loop is not None
        requests.decide(
            root,
            ResourceDecisionRequest(
                request_id=loop.request_id, action="redirect", reason="Try a different approach"
            ),
            operation="op-redirect",
        )
        RunState(store.control).set_cost_notices("p1", [1.0, 50.0])
        RunState(store.control).note_cost_crossings("p1")
    finally:
        store.close()


async def test_the_session_view_gives_spend_by_agent_and_model_with_caps_and_check_ins(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed_usage(state_dir)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(SESSION)
    assert response.status == 200, await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    view = await response.json()
    assert (view["project"], view["session"]) == ("p1", "a")
    assert view["total"]["cost"] == 2.25
    assert view["total"]["unpriced"] == 1
    assert [item["model"] for item in view["by_model"]] == ["big", "small", "judge"]
    agents = {item["key"]: item for item in view["agents"]}
    assert [item["key"] for item in view["agents"]] == [RUN, CHILD]  # the root first
    root, child = agents[RUN], agents[CHILD]
    assert (root["role"], root["parent"], root["cost"], root["unpriced"]) == ("main", None, 1.5, 1)
    assert child["parent"] == RUN
    assert child["task"] == "Verify finite-field claims"
    # The reviewer's calls are under the run they reviewed, apart from its own.
    assert [(line["role"], line["model"], line["cost"]) for line in child["lines"]] == [
        ("delegated", "small", 0.5),
        ("reviewer", "judge", 0.25),
    ]
    assert child["cost"] == 0.75
    caps = {cap["resource"]: cap for cap in child["caps"]}
    assert caps["tokens"] == {"resource": "tokens", "used": 110, "total": 20_000_000}
    assert caps["cost"]["used"] == 75
    assert {cap["resource"] for cap in root["caps"]} == {
        "tokens",
        "turns",
        "elapsed_seconds",
        "cost",
    }
    counts = view["counts"]
    assert counts["loops"] == {"same_call": 1}
    assert counts["answered_by"] == {"parent": 1, "none": 1}
    assert counts["actions"] == {"redirect": 1}
    kinds = {item["resource"]: item for item in view["check_ins"]["requests"]}
    assert kinds["loop"]["decision"]["request"]["action"] == "redirect"
    assert kinds["loop"]["summary"]["rule"] == "same_call"
    assert view["daily_cost_limit"] is None
    assert [item["threshold"] for item in view["notices"]] == [1.0, 50.0]
    assert view["notices"][0]["crossed_at"] is not None


async def test_the_session_view_is_authenticated_scoped_and_takes_no_query(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed_usage(state_dir)
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    await error_of(await anonymous.get(SESSION), 401, "policy_denied")
    await error_of(await anonymous.get(PROJECT), 401, "policy_denied")
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(await client.get(SESSION + "?x=1"), 400, "invalid_request")
    await error_of(
        await client.get(SESSION.replace("/sessions/a/", "/sessions/missing/")), 404, "not_found"
    )
    await error_of(
        await client.get(SESSION.replace("/projects/p1/", "/projects/nope/")), 404, "not_found"
    )
    assert (await client.post(SESSION, json={}, headers=origin(client))).status in (
        404,
        405,
    )  # nothing writes here


async def test_a_session_without_research_records_lists_its_runs_apart_and_has_no_caps(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    store = ProjectStore(state_dir)
    try:
        orphan = "20261017T000002000000Z-orphan"
        Archive(store.control).append(
            "p1",
            "c",
            orphan,
            Entry("assistant", "assistant", "an answer"),
            model_usage=AnsweredUsage(
                ordinal=1,
                observed_at=datetime.now(UTC),
                usage=ModelUsage(input_tokens=10, output_tokens=5, cost=0.1),
            ),
        )
    finally:
        store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir)
    quiet = await (await client.get(SESSION.replace("/sessions/a/", "/sessions/b/"))).json()
    assert (quiet["agents"], quiet["total"]["calls"], quiet["counts"]["total"]) == ([], 0, 0)
    assert quiet["check_ins"]["requests"] == []
    view = await (await client.get(SESSION.replace("/sessions/a/", "/sessions/c/"))).json()
    (agent,) = view["agents"]
    assert (agent["key"], agent["work"], agent["runs"], agent["caps"]) == (
        orphan,
        None,
        [orphan],
        [],
    )
    assert agent["lines"] == [
        {"role": "main", "model": None, "cost": 0.1, "tokens": 15, "calls": 1, "unpriced": 0}
    ]


async def test_the_project_view_lists_sessions_for_a_window(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed_usage(state_dir)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    for window in ("24h", "7d", "all", None):
        response = await client.get(PROJECT + ("" if window is None else f"?window={window}"))
        assert response.status == 200, await response.text()
        view = await response.json()
        assert view["window"] == (window or "24h")
        assert view["total"]["cost"] == 2.25
        assert view["all_time"]["cost"] == 2.25
        assert [
            (item["session"], item["check_ins"], item["loops"]) for item in view["sessions"]
        ] == [("a", 2, 1)]
        assert view["sessions"][0]["waiting"] == 1  # the loop check-in was answered, tokens not
        assert view["counts"]["loops"] == {"same_call": 1}
        assert view["unassigned"]["calls"] == 0
        assert view["daily_cost_limit"] is None
        assert view["last_day_cost"] is None
    since = (await (await client.get(PROJECT + "?window=7d")).json())["since"]
    assert since is not None
    assert (await (await client.get(PROJECT + "?window=all")).json())["since"] is None


async def test_the_project_view_checks_its_window_and_project(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed_usage(state_dir)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    for bad in ("?window=30d", "?window=", "?other=1", "?window=24h&x=1"):
        await error_of(await client.get(PROJECT + bad), 400, "invalid_request")
    await error_of(await client.get(PROJECT.replace("/p1/", "/nope/")), 404, "not_found")
    store = ProjectStore(state_dir)
    try:
        RunState(store.control).set_cost_limit("p1", 10.0)
    finally:
        store.close()
    view = await (await client.get(PROJECT)).json()
    assert view["daily_cost_limit"] == 10.0
    assert view["last_day_cost"] == 2.25
