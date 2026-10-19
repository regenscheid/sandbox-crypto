# SPDX-License-Identifier: MIT
"""Authenticated Resume effort changes keep one exact immutable request audit."""

from __future__ import annotations

from contextlib import closing

import pytest

from sandbox_science.orchestrator.continuity.effort import read_override
from sandbox_science.orchestrator.projects import ProjectStore

from .route_support import error_of, origin, workspace
from .test_routes_recovery import RUN, seed

PATH = "/api/v0/projects/p1/sessions/a/resume"
BODY = {
    "root": RUN,
    "reasoning_effort": {"main": "high", "fold": "medium"},
    "reason": "Operator requests a deeper continuation within the original budget.",
}


async def test_effort_resume_requires_login_origin_and_exact_replay(
    aiohttp_client, state_dir, static_dir
):
    seed(state_dir)
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    await error_of(
        await anonymous.post(PATH, json=BODY, headers=origin(anonymous)), 401, "policy_denied"
    )
    client = await workspace(aiohttp_client, state_dir, static_dir)
    assert (await client.post(PATH, json=BODY)).status == 403
    accepted = await client.post(PATH, json=BODY, headers=origin(client))
    assert accepted.status == 200, await accepted.text()
    receipt = await accepted.json()
    assert receipt["resumed"] == 0
    assert accepted.headers["Cache-Control"] == "no-store"
    repeated = await client.post(PATH, json=BODY, headers=origin(client))
    assert await repeated.json() == receipt
    with closing(ProjectStore(state_dir)) as store, store.control.read() as tx:
        audit = read_override(tx.sql, "p1", "a", RUN, receipt["resume_request"])
        assert audit is not None
        assert audit.reasoning_effort == {"main": "high", "fold": "medium"}
        assert audit.reason == BODY["reason"]
        assert audit.operator == "user"
    for changed in ({}, {**BODY, "reasoning_effort": {"main": "medium"}}):
        await error_of(
            await client.post(PATH, json=changed, headers=origin(client)), 409, "conflict"
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"reason": "Missing effort"},
        {"reasoning_effort": {"main": "high"}},
        {"reasoning_effort": {"main": None}, "reason": "Explicit"},
        {"reasoning_effort": {"unsupported": "high"}, "reason": "Explicit"},
        {"reasoning_effort": {"main": "a" * 33}, "reason": "Explicit"},
        {"reasoning_effort": {"main": "high"}, "reason": " "},
        {**BODY, "alias": "another"},
        {**BODY, "api": "responses"},
        {**BODY, "operator": "forged"},
    ],
)
async def test_invalid_resume_override_has_no_admission(
    aiohttp_client, state_dir, static_dir, changed
):
    seed(state_dir)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(
        await client.post(PATH, json=changed, headers=origin(client)), 400, "invalid_request"
    )
    with closing(ProjectStore(state_dir)) as store, store.control.read() as tx:
        assert tx.sql.execute("SELECT count(*) FROM research_resumes").fetchone()[0] == 0


async def test_override_cannot_disappear_into_legacy_message_resume(
    aiohttp_client, state_dir, static_dir
):
    client = await workspace(aiohttp_client, state_dir, static_dir)
    body = {key: value for key, value in BODY.items() if key != "root"}
    await error_of(await client.post(PATH, json=body, headers=origin(client)), 409, "conflict")
