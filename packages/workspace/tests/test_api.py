# SPDX-License-Identifier: MIT
"""The API's answers, from a state directory the harness's own store wrote (conftest)."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.control import migrations, store_path
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.projects import (
    ModelUsage,
    ProjectStore,
    RunRecord,
    SessionRecord,
    run_name,
)
from sandbox_science.workspace import api
from sandbox_science.workspace.app import create_app

TOKEN = "test-secret-login-token-0123456789abcdef"
POLICY = "sha256:" + "a" * 64
FIRST_RUN = "20261011T130102345678Z-run-1"
SECOND_RUN = "20261011T140102345678Z-run-2"

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


async def logged_in(client: TestClient[Any, Any]) -> TestClient[Any, Any]:
    response = await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert response.status == 303
    return client


@pytest_asyncio.fixture
async def client(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> TestClient[Any, Any]:
    app = create_app(ProjectStore(state_dir, readonly=True), static_dir, login_token=TOKEN)
    return await logged_in(await aiohttp_client(app))


async def get(client: TestClient[Any, Any], path: str, status: int = 200) -> Any:
    response = await client.get(path)
    assert response.status == status, await response.text()
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.headers["Cache-Control"] == "no-store"
    return await response.json()


async def test_the_projects(client: TestClient[Any, Any]) -> None:
    listed = (await get(client, "/api/v0/projects"))["projects"]
    assert [project["project_id"] for project in listed] == ["broken", "p1", "p2"]
    broken, p1, p2 = listed
    assert "is damaged" in broken["error"]
    assert (p1["profile"], p1["policy"], p1["sessions"]) == ("crypto-base", POLICY, 3)
    assert p1["default_environment_id"].startswith("p1-default-")
    assert (p2["profile"], p2["sessions"]) == ("minimal", 0)
    assert set(p1) == {
        "project_id",
        "profile",
        "policy",
        "default_environment_id",
        "created_at",
        "updated_at",
        "sessions",
    }


async def test_failed_partial_run_is_readable_only_in_its_session(
    client: TestClient[Any, Any], state_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    original = projects.run("p1", "a", FIRST_RUN)
    partial = original.model_copy(
        update={
            "run_id": "failed",
            "stopped": "error",
            "failure": "model",
            "final_text": None,
            "messages": original.messages[:-1],
        }
    )
    entry = projects.save_run(partial)
    path = f"/api/v0/projects/p1/sessions/a/runs/{entry.name}"
    result = await get(client, path)
    assert (result["stopped"], result["failure"], result["final_text"]) == ("error", "model", None)
    assert result["messages"] == partial.messages
    assert result["tools"] == partial.tools
    await get(client, path.replace("/sessions/a/", "/sessions/b/"), 404)
    await get(client, path.replace("/projects/p1/", "/projects/p2/"), 404)


async def test_one_project_with_its_sessions_and_environments(
    client: TestClient[Any, Any], state_dir: Path
) -> None:
    record = ProjectStore(state_dir).get("p1")
    found = (await get(client, "/api/v0/projects/p1"))["project"]
    assert (found["profile"], found["policy"]) == ("crypto-base", POLICY)
    sessions = {session["session_id"]: session for session in found["sessions"]}
    assert list(sessions) == ["a", "b", "c"]
    assert sessions["a"]["runs"] == 2
    assert sessions["b"]["closed_at"] is not None
    assert sessions["c"]["dedicated"] is True
    assert sessions["a"]["environment_id"] == record.default_environment_id
    assert found["environments"] == [
        {"environment_id": record.default_environment_id, "default": True, "sessions": ["a", "b"]},
        {"environment_id": sessions["c"]["environment_id"], "default": False, "sessions": ["c"]},
    ]


async def test_a_projects_model_cost_is_what_the_gateway_reported(
    client: TestClient[Any, Any], state_dir: Path
) -> None:
    found = (await get(client, "/api/v0/projects/p1"))["project"]
    unpriced = {"model_info_last_day": None, "model_info_total": None}
    assert found["model_cost"] == {"last_day": None, "total": None, "by_role": [], **unpriced}
    store = ProjectStore(state_dir)
    now = datetime.now(UTC)
    priced = store.run("p1", "a", FIRST_RUN).model_copy(
        update={
            "run_id": "run-priced",
            "started_at": now,
            "ended_at": now,
            "usage": [ModelUsage(input_tokens=10, output_tokens=2, cost=0.125)],
        }
    )
    store.save_run(priced)
    found = (await get(client, "/api/v0/projects/p1"))["project"]
    main = {"role": "main", "last_day": 0.125, "total": 0.125}
    assert found["model_cost"] == {
        "last_day": 0.125,
        "total": 0.125,
        "by_role": [main],
        **unpriced,
    }
    # Lab 40, B5: each role's calls, by role: a delegated run's, and the job watcher's.
    later = now + timedelta(seconds=1)
    child = priced.model_copy(
        update={
            "run_id": "run-child",
            "role": "delegated",
            "parent": run_name(now, "run-priced"),
            "delegation": "dlg-1",
            "started_at": later,
            "ended_at": later,
            "usage": [ModelUsage(input_tokens=10, output_tokens=2, cost=0.25)],
        }
    )
    store.save_run(child)
    jobs = JobState(state_dir)
    try:
        context = ExecutionContext(project_id="p1", session_id="a", environment_id="e")
        call = jobs.reserve(context, "run-priced", "exe-1", "watcher", {}, 60, 0)
        assert call is not None
        # Lab 43, B2: priced from the gateway's model info, which the page tells apart.
        jobs.finish_call(call, "fine", "small", 10, 2, 0.5, "model_info")
    finally:
        jobs.close()
    found = (await get(client, "/api/v0/projects/p1"))["project"]
    assert found["model_cost"] == {
        "last_day": 0.875,
        "total": 0.875,
        "by_role": [
            main,
            {"role": "delegated", "last_day": 0.25, "total": 0.25},
            {"role": "watcher", "last_day": 0.5, "total": 0.5},
        ],
        "model_info_last_day": 0.5,
        "model_info_total": 0.5,
    }


async def test_one_session_with_its_runs_oldest_first(client: TestClient[Any, Any]) -> None:
    found = await get(client, "/api/v0/projects/p1/sessions/a")
    assert found["project_id"] == "p1"
    assert found["session"]["session_id"] == "a"
    # The page sees the session's facts, not the store's own lifetime marker.
    session_keys = {"session_id", "environment_id", "dedicated", "opened_at", "closed_at"}
    assert set(found["session"]) == session_keys
    project = (await get(client, "/api/v0/projects/p1"))["project"]
    assert all(set(item) == session_keys | {"runs"} for item in project["sessions"])
    assert found["runs"] == [
        {
            "name": FIRST_RUN,
            "run_id": "run-1",
            "started_at": "2026-10-11T13:01:02.345678Z",
            "role": "main",
            "parent": None,
        },
        {
            "name": SECOND_RUN,
            "run_id": "run-2",
            "started_at": "2026-10-11T14:01:02.345678Z",
            "role": "main",
            "parent": None,
        },
    ]
    assert (await get(client, "/api/v0/projects/p1/sessions/c"))["runs"] == []


async def test_one_run_as_the_harness_recorded_it(
    client: TestClient[Any, Any], state_dir: Path
) -> None:
    found = await get(client, f"/api/v0/projects/p1/sessions/a/runs/{FIRST_RUN}")
    kept = ProjectStore(state_dir).run("p1", "a", FIRST_RUN)
    assert found == kept.model_dump(mode="json")
    assert found["final_text"] == "It is $1$."
    assert found["tools"][0]["arguments"] == '{"code": "print(1**2)"}'


@pytest.mark.parametrize(
    ("path", "status", "kind"),
    [
        ("/api/v0/projects/missing", 404, "not_found"),
        ("/api/v0/projects/bad%20id", 400, "invalid_request"),
        ("/api/v0/projects/..", 400, "invalid_request"),
        ("/api/v0/projects/broken", 503, "unavailable"),
        ("/api/v0/projects/p1/sessions/missing", 404, "not_found"),
        ("/api/v0/projects/p1/sessions/missing/runs/" + FIRST_RUN, 404, "not_found"),
        ("/api/v0/projects/p2/sessions/a/runs/" + FIRST_RUN, 404, "not_found"),
        ("/api/v0/projects/p1/sessions/a/runs/20261011T130102345678Z-run-9", 404, "not_found"),
        ("/api/v0/projects/p1/sessions/a/runs/run-1", 400, "invalid_request"),
        ("/api/v0/projects/p1/sessions/a/runs/..%2F..%2F..%2Fp1", 400, "invalid_request"),
        ("/api/v0/projects/p1/sessions/a/runs/" + FIRST_RUN + "%2F..", 400, "invalid_request"),
    ],
)
async def test_errors_name_their_kind(
    client: TestClient[Any, Any], path: str, status: int, kind: str
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", client.port)
    cookie = "; ".join(f"{c.key}={c.value}" for c in client.session.cookie_jar)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nCookie: {cookie}\r\n"
        "Connection: close\r\n\r\n".encode()
    )
    reply = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    await writer.wait_closed()
    head, _, body = reply.partition(b"\r\n\r\n")
    assert int(head.split(b" ", 2)[1]) == status, body
    assert json.loads(body)["error"]["kind"] == kind


async def test_a_run_too_large_to_show_is_refused(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    app = create_app(
        ProjectStore(state_dir, readonly=True), static_dir, login_token=TOKEN, max_run_bytes=100
    )
    client = await logged_in(await aiohttp_client(app))
    refused = await get(client, f"/api/v0/projects/p1/sessions/a/runs/{FIRST_RUN}", 422)
    assert refused["error"]["kind"] == "unsupported"


async def test_a_run_holding_values_json_cannot_carry_is_still_shown(
    client: TestClient[Any, Any], state_dir: Path
) -> None:
    # A lone surrogate from a model's reply and a NaN from a tool's JSON: the browser's
    # JSON.parse must still read the answer.
    store = ProjectStore(state_dir)
    started = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)
    entry = store.save_run(
        RunRecord(
            run_id="run-3",
            context=ExecutionContext(project_id="p1", session_id="a", environment_id="env-1"),
            task="t",
            started_at=started,
            ended_at=started,
            stopped="answered",
            final_text="half an emoji \ud83d",
            messages=[],
            tools=[{"tool": "pdf_info", "pdf": {"x0": math.nan, "x1": math.inf}}],
        )
    )
    response = await client.get(f"/api/v0/projects/p1/sessions/a/runs/{entry.name}")
    assert response.status == 200
    text = await response.text()

    def no_constants(name: str) -> None:
        raise AssertionError(f"{name} is not JSON")

    found = json.loads(text, parse_constant=no_constants)
    assert found["final_text"] == "half an emoji \ud83d"
    assert found["tools"][0]["pdf"] == {"x0": "nan", "x1": "inf"}
    assert text.isascii()


async def test_a_newer_control_store_is_503_with_the_reason_on_project_routes(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    database = sqlite3.connect(store_path(state_dir))
    database.execute(f"PRAGMA user_version = {migrations.VERSION + 1}")  # a newer harness
    database.commit()
    database.close()
    client = await logged_in(
        await aiohttp_client(
            create_app(ProjectStore(state_dir, readonly=True), static_dir, login_token=TOKEN)
        )
    )
    for path in ("/api/v0/projects", "/api/v0/projects/p1", "/api/v0/projects/p1/documents"):
        body = await get(client, path, 503)
        assert body["error"]["kind"] == "unavailable"
        assert "newer than this version's" in body["error"]["message"]


async def test_typed_answers_equal_their_models_and_omit_the_lifetime(
    client: TestClient[Any, Any],
) -> None:
    for path, model in (
        ("/api/v0/projects", api.ProjectListing),
        ("/api/v0/projects/p1", api.ProjectAnswer),
        ("/api/v0/projects/p2", api.ProjectAnswer),
        ("/api/v0/projects/p1/sessions/a", api.SessionDetail),
        ("/api/v0/projects/p1/sessions/b", api.SessionDetail),
    ):
        response = await client.get(path)
        raw = await response.text()
        assert "kernel_notice_lifetime" not in raw
        found = json.loads(raw)
        assert model.model_validate(found).model_dump(mode="json") == found
    listed = (await get(client, "/api/v0/projects"))["projects"]
    damaged = [item for item in listed if "error" in item]
    assert [list(item) for item in damaged] == [["project_id", "error"]]


def test_a_renamed_store_field_stays_out_of_the_session_view() -> None:
    # A field the store adds or renames must not turn every project and session view
    # into a 500: the view copies only its own fields.
    class Renamed(SessionRecord):
        notice_lifetime: str | None = None  # the lifetime marker under a new name

    found = Renamed(
        session_id="a",
        environment_id="e",
        opened_at=datetime(2026, 10, 14, tzinfo=UTC),
        notice_lifetime="kns-1",
    )
    view = api.SessionView.of(found)
    assert "notice_lifetime" not in view.model_dump()
    assert view.session_id == "a"
