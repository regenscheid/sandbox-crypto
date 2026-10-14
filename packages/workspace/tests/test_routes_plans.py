# SPDX-License-Identifier: MIT
"""Characterization of the session plan routes through HTTP only.

``GET …/sessions/{session}/plan`` and ``POST …/sessions/{session}/plan/changes``.
Plans are seeded through ``SessionPlans``' public methods. These pin what the browser
sees, so that the E-01/E-02 rewrite keeps it: shapes, status codes, operation-ID
replay, the compare-and-swap conflict, no-store and the guards.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import (
    PlanAuthority,
    PlanBody,
    PlanRef,
    PlanScope,
    SessionPlans,
)

from .route_support import (
    ClientFactory,
    Json,
    assert_security_headers,
    error_of,
    origin,
    workspace,
)

PLAN = "/api/v0/projects/{project}/sessions/{session}/plan"


def plan_path(project: str = "p1", session: str = "a") -> str:
    return PLAN.format(project=project, session=session)


def changes_path(project: str = "p1", session: str = "a") -> str:
    return plan_path(project, session) + "/changes"


def main(session: str = "a") -> PlanAuthority:
    return PlanAuthority(
        scope=PlanScope(project_id="p1", session_id=session), role="main", run_id="main-run"
    )


def body(description: str = "Check the claim") -> Json:
    return {
        "goal": "A bounded goal",
        "feasibility": "Inspect the assumptions",
        "outputs": [{"title": "Result", "description": "Derive", "evidence_standard": "Check"}],
        "phases": [
            {
                "phase_id": "phase",
                "title": "Analysis",
                "steps": [
                    {
                        "step_id": "step",
                        "title": "Analyze",
                        "description": description,
                        "evidence_standard": "Retain evidence",
                    },
                    {
                        "step_id": "other",
                        "title": "Compare",
                        "description": "Keep this intent",
                        "evidence_standard": "Known case",
                    },
                ],
            }
        ],
    }


def plan_body(value: Json) -> PlanBody:
    return PlanBody.model_validate_json(json.dumps(value))


@pytest.fixture
def plans(state_dir: Path) -> Iterator[SessionPlans]:
    documents = DocumentStore(store_path(state_dir))
    yield SessionPlans(documents)
    documents.close()


def seed(plans: SessionPlans, session: str = "a") -> PlanRef:
    """An active version-1 plan, as the agent's plan tools make one."""
    created = plans.publish(main(session), "create", plan_body(body()), "Initial")
    plans.set_status(main(session), "activate", created.ref, 1, 0, "active", "Authorized")
    return created.ref


def current_version(plans: SessionPlans) -> int:
    current = plans.compact_current(main())
    assert current is not None
    return current.head.ref.version


def change(
    base: PlanRef, *, operation: str = "user-edit-1", description: str = "USER refined it"
) -> Json:
    return {
        "operation_id": operation,
        "base": base.model_dump(mode="json"),
        "body": body(description),
        "reason": "USER steering",
    }


async def post(client: TestClient[Any, Any], value: object, path: str | None = None) -> Any:
    return await client.post(path or changes_path(), json=value, headers=origin(client))


# GET …/plan


async def test_session_without_a_plan_reads_as_null(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(plan_path())
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content_type == "application/json"
    assert_security_headers(response.headers)
    body = await response.json()
    assert set(body) == {"project_id", "session_id", "observed_at", "session_closed_at", "plan"}
    assert (body["project_id"], body["session_id"]) == ("p1", "a")
    assert body["session_closed_at"] is None
    assert body["plan"] is None


async def test_current_plan_has_the_shape_the_page_reads(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    ref = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(plan_path())
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    plan = (await response.json())["plan"]
    assert set(plan) == {"event_cursor", "head", "version", "steps"}
    assert (
        plan["head"]["ref"] == ref.model_dump(mode="json") == {"plan_id": ref.plan_id, "version": 1}
    )
    assert plan["head"]["status"] == "active"
    assert plan["head"]["scope"] == {"project_id": "p1", "session_id": "a"}
    assert plan["version"]["ref"] == plan["head"]["ref"]
    assert plan["version"]["body"]["goal"] == "A bounded goal"
    assert [step["step_id"] for step in plan["steps"]] == ["step", "other"]
    assert all(step["latest"] is None for step in plan["steps"])


async def test_closed_session_plan_stays_readable_with_its_closing_time(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    seed(plans, "b")
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(plan_path(session="b"))
    assert response.status == 200
    body = await response.json()
    assert body["session_closed_at"] is not None
    assert body["plan"]["head"]["status"] == "active"


@pytest.mark.parametrize(
    ("project", "session"),
    [("missing", "a"), ("p1", "missing"), ("p2", "a")],
    ids=["unknown-project", "unknown-session", "session-of-another-project"],
)
async def test_plan_of_an_unknown_session_is_not_found(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, project: str, session: str
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(await client.get(plan_path(project, session)), 404, "not_found")


@pytest.mark.parametrize(
    "path",
    [
        plan_path() + "?version=1",
        plan_path(project="-bad"),
        plan_path(session="-bad"),
        plan_path(session="a" * 129),
    ],
    ids=["query", "invalid-project-id", "invalid-session-id", "overlong-session-id"],
)
async def test_plan_read_refuses_an_invalid_request(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, path: str
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(await client.get(path), 400, "invalid_request")


async def test_damaged_project_record_is_unavailable_for_plans(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(plan_path(project="broken"))
    await error_of(response, 503, "unavailable")
    assert str(state_dir) not in await response.text()


# POST …/plan/changes


async def test_edit_answers_a_receipt_and_the_next_read_shows_it(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    base = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await post(client, change(base))
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content_type == "application/json"
    assert_security_headers(response.headers)
    answer = await response.json()
    # planEdits.ts checks every one of these.
    assert set(answer) == {"project_id", "session_id", "receipt"}
    assert (answer["project_id"], answer["session_id"]) == ("p1", "a")
    receipt = answer["receipt"]
    assert receipt["scope"] == {"project_id": "p1", "session_id": "a"}
    assert receipt["operation_id"] == "user-edit-1"
    assert receipt["action"] == "revise"
    assert receipt["ref"] == {"plan_id": base.plan_id, "version": 2}
    assert receipt["event_sequence"] >= 1
    assert receipt["status_revision"] >= 1
    assert receipt["progress_cursor"] >= 0

    current = (await (await client.get(plan_path())).json())["plan"]
    assert current["head"]["ref"] == {"plan_id": base.plan_id, "version": 2}
    assert current["head"]["status"] == "active"
    steps = current["version"]["body"]["phases"][0]["steps"]
    assert steps[0]["description"] == "USER refined it"
    assert current["version"]["author"]["role"] == "user"


async def test_an_operation_replays_after_logging_in_again(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    # The user identity is the workspace's one USER, not the login's cookie, so a save
    # whose answer was lost replays after a new login instead of conflicting with itself.
    base = seed(plans)
    first = await post(await workspace(aiohttp_client, state_dir, static_dir), change(base))
    assert first.status == 200
    again = await post(await workspace(aiohttp_client, state_dir, static_dir), change(base))
    assert again.status == 200
    assert await again.json() == await first.json()


async def test_a_save_recorded_under_a_login_digest_conflicts_and_never_applies_twice(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    # Saves recorded before the one USER named their author by a login cookie's digest.
    base = seed(plans)
    earlier = PlanAuthority(
        scope=PlanScope(project_id="p1", session_id="a"), role="user", user_id="a" * 64
    )
    plans.revise(earlier, "user-edit-1", base, plan_body(body("USER refined it")), "USER")
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await post(client, change(base))
    assert (await error_of(response, 409, "conflict"))["current"]["version"] == 2
    assert current_version(plans) == 2


async def test_repeating_an_operation_replays_its_receipt(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    base = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    first = await post(client, change(base))
    assert first.status == 200
    again = await post(client, change(base))
    assert again.status == 200
    assert await again.json() == await first.json()
    current = (await (await client.get(plan_path())).json())["plan"]
    assert current["head"]["ref"]["version"] == 2


async def test_reusing_an_operation_id_for_other_content_conflicts(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    base = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    assert (await post(client, change(base))).status == 200
    response = await post(client, change(base, description="Something else"))
    await error_of(response, 409, "conflict")
    current = (await (await client.get(plan_path())).json())["plan"]
    assert current["head"]["ref"]["version"] == 2
    assert current["version"]["body"]["phases"][0]["steps"][0]["description"] == "USER refined it"


async def test_stale_base_conflicts_and_names_the_current_version(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    base = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    assert (await post(client, change(base))).status == 200
    response = await post(client, change(base, operation="user-edit-2", description="Late"))
    error = await error_of(response, 409, "conflict")
    assert error["current"] == {"plan_id": base.plan_id, "version": 2}
    current = (await (await client.get(plan_path())).json())["plan"]
    assert current["head"]["ref"]["version"] == 2


async def test_a_plan_changed_by_the_agent_meanwhile_conflicts(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    base = seed(plans)
    newer = plans.revise(main(), "agent-edit", base, plan_body(body("MAIN")), "MAIN")
    client = await workspace(aiohttp_client, state_dir, static_dir)
    error = await error_of(await post(client, change(base)), 409, "conflict")
    assert error["current"] == newer.ref.model_dump(mode="json")


async def test_editing_a_closed_session_conflicts(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    base = seed(plans, "b")
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(await post(client, change(base), changes_path(session="b")), 409, "conflict")
    current = (await (await client.get(plan_path(session="b"))).json())["plan"]
    assert current["head"]["ref"]["version"] == 1


@pytest.mark.parametrize(
    ("project", "session"),
    [("missing", "a"), ("p1", "missing"), ("p2", "a"), ("p1", "c")],
    ids=["unknown-project", "unknown-session", "session-of-another-project", "no-plan"],
)
async def test_editing_an_unknown_session_or_absent_plan_is_not_found(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    plans: SessionPlans,
    project: str,
    session: str,
) -> None:
    base = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(
        await post(client, change(base), changes_path(project, session)), 404, "not_found"
    )


def _without(value: Json, key: str) -> Json:
    return {name: item for name, item in value.items() if name != key}


@pytest.mark.parametrize(
    "case",
    [
        "missing-reason",
        "empty-reason",
        "extra-field",
        "invalid-operation-id",
        "body-not-a-plan",
        "not-json",
        "not-json-content-type",
        "query",
        "invalid-session-id",
    ],
)
async def test_edit_refuses_an_invalid_request(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    plans: SessionPlans,
    case: str,
) -> None:
    base = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    wanted = change(base)
    headers = origin(client)
    path = changes_path()
    if case == "missing-reason":
        response = await post(client, _without(wanted, "reason"))
    elif case == "empty-reason":
        response = await post(client, {**wanted, "reason": ""})
    elif case == "extra-field":
        response = await post(client, {**wanted, "user_id": "someone-else"})
    elif case == "invalid-operation-id":
        response = await post(client, {**wanted, "operation_id": "-bad"})
    elif case == "body-not-a-plan":
        response = await post(client, {**wanted, "body": {"goal": "only a goal"}})
    elif case == "not-json":
        response = await client.post(
            path, data=b"{not json", headers={**headers, "Content-Type": "application/json"}
        )
    elif case == "not-json-content-type":
        response = await client.post(
            path, data=b"operation_id=x", headers={**headers, "Content-Type": "text/plain"}
        )
    elif case == "query":
        response = await post(client, wanted, path + "?force=1")
    else:
        response = await post(client, wanted, changes_path(session="-bad"))
    await error_of(response, 400, "invalid_request")
    current = (await (await client.get(plan_path())).json())["plan"]
    assert current["head"]["ref"]["version"] == 1


async def test_an_oversized_edit_is_refused(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    base = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await post(client, {**change(base), "reason": "x" * (200 * 1024)})
    await error_of(response, 413, "invalid_request")


# Guards


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_plan_routes_need_a_login(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    plans: SessionPlans,
    method: str,
) -> None:
    base = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    if method == "GET":
        response = await client.get(plan_path())
    else:
        response = await post(client, change(base))
    await error_of(response, 401, "policy_denied")
    assert_security_headers(response.headers)
    assert current_version(plans) == 1


@pytest.mark.parametrize("sent", [None, "http://evil.example", "null"])
async def test_edit_needs_the_pages_own_origin(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    plans: SessionPlans,
    sent: str | None,
) -> None:
    base = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    headers = {} if sent is None else {"Origin": sent}
    response = await client.post(changes_path(), json=change(base), headers=headers)
    assert response.status == 403
    # Every plan route response carries no-store, the guard's refusals included.
    assert response.headers["Cache-Control"] == "no-store"
    assert_security_headers(response.headers)
    assert current_version(plans) == 1


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_plan_routes_refuse_a_foreign_host_name(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    plans: SessionPlans,
    method: str,
) -> None:
    base = seed(plans)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    host = {"Host": "rebound.example"}
    if method == "GET":
        response = await client.get(plan_path(), headers=host)
    else:
        response = await client.post(
            changes_path(), json=change(base), headers={**host, "Origin": "http://rebound.example"}
        )
    assert response.status == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert current_version(plans) == 1


async def test_plan_reads_do_not_touch_the_project_record(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, plans: SessionPlans
) -> None:
    seed(plans)
    record = ProjectStore(state_dir).get("p1")
    client = await workspace(aiohttp_client, state_dir, static_dir)
    assert (await client.get(plan_path())).status == 200
    assert ProjectStore(state_dir).get("p1") == record
