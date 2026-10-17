# SPDX-License-Identifier: MIT
"""Active research retains its sandbox without disabling bounded idle suspension."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from sandbox_science.contracts import EnvironmentInfo, EnvironmentState, ExecutionContext
from sandbox_science.contracts.environment_claims import MAX_ENVIRONMENT_CLAIMS, EnvironmentClaim
from sandbox_science.worker.claims import EnvironmentClaims
from sandbox_science.worker.connections import Activity
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.suspend import Suspender

from .test_environments import A_TOKEN, B_TOKEN, Api, Setup, StubManager, always
from .test_environments import api as api
from .test_environments import paused as paused
from .test_environments import setup as setup

pytestmark = pytest.mark.slow


def claim(info: EnvironmentInfo, holder: str = "root-a") -> EnvironmentClaim:
    return EnvironmentClaim(
        context=ExecutionContext(
            project_id=info.project_id,
            session_id="research",
            environment_id=info.environment_id,
            policy_revision=info.policy,
        ),
        holder=holder,
    )


async def test_model_only_research_retains_idle_sandbox_until_claim_expires(paused: Setup) -> None:
    clock = paused.fake_clock()
    info, _ = await paused.environments.create(paused.request())
    paused.environments.release_idle = always
    stub = StubManager()
    stub.activities = {info.environment_id: Activity(clock.monotonic(), 0, None, 0)}
    suspender = Suspender(paused.environments, cast(ExecutionManager, stub), 10, clock=clock)
    held = await paused.environments.claim(claim(info))
    assert held.state == "held"
    clock.advance(60)
    assert await suspender.check() == []
    assert not await paused.environments.suspend(info.environment_id, clock.monotonic())
    assert paused.fake.calls("sandbox", "stop") == []
    clock.advance(60)
    assert await suspender.check() == [info.environment_id]
    assert paused.environments.get(info.environment_id).state is EnvironmentState.STOPPED
    with pytest.raises(WorkerError, match="ready sandbox"):
        await paused.environments.claim(claim(info))


async def test_claims_survive_reopen_and_release_only_their_own_scope(
    paused: Setup, tmp_path: Path
) -> None:
    info, _ = await paused.environments.create(paused.request())
    path = tmp_path / "claims.sqlite3"
    clock = paused.fake_clock()
    first, second = claim(info), claim(info, "root-b")
    store = EnvironmentClaims(path, clock=clock)
    store.grant(first, info)
    store.grant(second, info)
    store.close()
    store = EnvironmentClaims(path, clock=clock)
    try:
        assert store.held(info)
        wrong = first.model_copy(
            update={
                "context": first.context.model_copy(update={"session_id": "other"}),
                "sandbox_id": info.sandbox_id,
            }
        )
        store.release(wrong)
        store.release(first.model_copy(update={"sandbox_id": "another-sandbox"}))
        store.release(first.model_copy(update={"sandbox_id": info.sandbox_id}))
        assert store.held(info)  # the other root still owns its claim
        assert not store.held(info.model_copy(update={"sandbox_id": "another-sandbox"}))
        clock.advance(60)
        store.grant(second, info)
        clock.advance(61)
        assert store.held(info)
        store.release(second.model_copy(update={"sandbox_id": info.sandbox_id}))
        assert not store.held(info)
    finally:
        store.close()


async def test_claim_cap_and_generation_and_policy_checks(paused: Setup) -> None:
    info, _ = await paused.environments.create(paused.request())
    for index in range(MAX_ENVIRONMENT_CLAIMS):
        await paused.environments.claim(claim(info, f"root-{index}"))
    await paused.environments.claim(claim(info, "root-0"))  # renewal needs no new slot
    with pytest.raises(WorkerError, match="claim limit"):
        await paused.environments.claim(claim(info, "one-too-many"))
    request = claim(info, "root-0")
    with pytest.raises(WorkerError, match="sandbox was replaced"):
        await paused.environments.claim(request.model_copy(update={"sandbox_id": "wrong"}))
    with pytest.raises(WorkerError, match="policy is stale"):
        await paused.environments.claim(
            request.model_copy(
                update={
                    "context": request.context.model_copy(
                        update={"policy_revision": "sha256:" + "f" * 64}
                    )
                }
            )
        )
    with pytest.raises(WorkerError, match="another scope"):
        await paused.environments.claim(
            request.model_copy(
                update={"context": request.context.model_copy(update={"session_id": "other"})}
            )
        )
    paused.fake_clock().advance(120)
    assert (await paused.environments.claim(claim(info, "one-too-many"))).state == "held"


async def test_claim_api_enforces_project_bounds_and_explicit_release(api: Api) -> None:
    status, _ = await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a"))
    assert status == 201
    info = api.setup.environments.get("env-a")
    request = claim(info)
    body = request.model_dump(mode="json")
    assert await api.refused("POST", "/v1/environment-claims", B_TOKEN, body) == (
        403,
        "policy_denied",
    )
    cross_project = {**body, "context": {**body["context"], "project_id": "p2"}}
    assert await api.refused("POST", "/v1/environment-claims", B_TOKEN, cross_project) == (
        404,
        "not_found",
    )
    for change in ({"seconds": 301}, {"seconds": True}, {"surprise": True}):
        assert await api.refused("POST", "/v1/environment-claims", A_TOKEN, {**body, **change}) == (
            400,
            "invalid_request",
        )
    status, reply = await api.call("POST", "/v1/environment-claims", A_TOKEN, body)
    assert status == 200
    assert reply["state"] == "held"
    status, suspended = await api.call("POST", "/v1/environments/env-a/suspend", A_TOKEN, {})
    assert status == 200
    assert not suspended["suspended"]
    assert api.setup.fake.calls("sandbox", "stop") == []
    status, released = await api.call(
        "POST",
        "/v1/environment-claims",
        A_TOKEN,
        {
            **body,
            "seconds": 0,
            "sandbox_id": reply["sandbox_id"],
        },
    )
    assert status == 200
    assert released["state"] == "released"
    assert not api.setup.environments.protected("env-a")
