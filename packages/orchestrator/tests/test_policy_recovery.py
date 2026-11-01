# SPDX-License-Identifier: MIT
"""Resume under the project's current policy (RC-2, RC-13), as on the Mac, 2026-10-21.

The session's environment had been left on another policy revision than the project's,
and every Resume failed at the worker's claim ("environment claim's policy is stale").
A Resume now brings the environment to the project's policy first. When the project's
policy changed since the work began, the user's own Resume takes the new one and says
so; an automatic Resume is refused with the reason, never widening authority unasked.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sandbox_science.contracts import ErrorKind, new_id
from sandbox_science.contracts.environments import EnvironmentInfo, EnvironmentState
from sandbox_science.orchestrator.continuity.recovery import POLICY_CHANGED, Recovery
from sandbox_science.orchestrator.harness import Harness, Role, Settings
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .test_run_harness import Sink, main
from .test_tree_recovery import RecoveryWorker, drive, prepare_tree

OTHER = "sha256:" + "3" * 64
NEWER = "sha256:" + "5" * 64


class PolicyWorker(RecoveryWorker):
    """A worker that manages the environment, under one policy revision at a time."""

    def __init__(self, policy: str, profile: str) -> None:
        super().__init__()
        self.policy, self.profile = policy, profile
        self.revised: list[str] = []

    def info(self, environment_id: str) -> EnvironmentInfo:
        now = datetime.now(UTC)
        return EnvironmentInfo(
            environment_id=environment_id,
            project_id="p",
            profile=self.profile,
            image="fixture",
            sandbox="ss-fixture",
            sandbox_id="sandbox-1",
            policy=self.policy,
            state=EnvironmentState.READY,
            created_at=now,
            updated_at=now,
        )

    async def environment(self, environment_id):  # pyright: ignore[reportIncompatibleMethodOverride]
        return self.info(environment_id)

    async def revise_environment(self, environment_id, policy):
        self.revised.append(policy)
        self.policy = policy
        return self.info(environment_id)

    async def claim_environment(self, request):
        if request.seconds and request.context.policy_revision != self.policy:
            raise WorkerAPIError(ErrorKind.POLICY_DENIED, "environment claim's policy is stale")
        return await super().claim_environment(request)


def harness(state, worker) -> Harness:
    models = [ModelTurn(json.dumps({"status": "complete", "summary": "Done"})) for _ in range(8)]
    return Harness(
        cast(WorkerClient, worker),
        main("Recovered root"),
        delegated=Role(ScriptedModel(models), "scripted"),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )


async def test_resume_brings_a_stale_environment_to_the_projects_policy(tmp_path: Path):
    state, documents, works = prepare_tree(tmp_path)
    policy = state.projects.get("p").policy
    worker = PolicyWorker(OTHER, state.projects.get("p").profile)  # as the Mac's was
    try:
        request = works.request("p", "s")
        assert request is not None
        recovery = Recovery(harness(state, worker), "p", "s", request)
        sink = Sink()
        failed = await drive(recovery, sink, asyncio.Event())
        assert not failed, sink.errors
        assert worker.revised == [policy]
        assert worker.claims
        assert worker.claims[0].context.policy_revision == policy
        assert recovery.policy is None  # the work's own policy, unchanged
    finally:
        documents.close()
        state.projects.close()


async def test_the_users_resume_takes_the_projects_new_policy_and_says_so(tmp_path: Path):
    state, documents, works = prepare_tree(tmp_path)
    began = state.projects.get("p").policy
    state.projects.set_policy("p", NEWER)
    worker = PolicyWorker(began, state.projects.get("p").profile)
    try:
        request = works.request("p", "s")
        assert request is not None
        recovery = Recovery(harness(state, worker), "p", "s", request)
        sink = Sink()
        failed = await drive(recovery, sink, asyncio.Event())
        assert not failed, sink.errors
        assert recovery.policy == NEWER
        assert worker.revised == [NEWER]
        assert {claim.context.policy_revision for claim in worker.claims} == {NEWER}
        assert any("resuming under the project's current policy" in note for note in sink.notes)
    finally:
        documents.close()
        state.projects.close()


async def test_an_automatic_resume_does_not_take_a_changed_policy(tmp_path: Path):
    state, documents, _ = prepare_tree(tmp_path)
    state.projects.set_policy("p", NEWER)
    worker = PolicyWorker(NEWER, state.projects.get("p").profile)
    request = new_id("resume")
    now = time.time()
    with state.projects.control.write() as tx:
        root = tx.sql.execute(
            "SELECT root FROM research_work WHERE project='p' AND session='s' LIMIT 1"
        ).fetchone()[0]
        tx.sql.execute(
            "INSERT INTO research_resumes VALUES (?,?,?,?,?,'queued',NULL)",
            ("p", "s", request, root, now),
        )
        tx.sql.execute(
            "INSERT INTO research_observations VALUES (?,?,?,'auto_resume',?,?,?)",
            ("p", "s", root, request, now, json.dumps({"progress": [], "streak": 1})),
        )
    try:
        recovery = Recovery(harness(state, worker), "p", "s", request)
        assert await recovery.run(Sink(), asyncio.Event())
        assert not worker.claims
        assert not worker.revised
        with state.projects.control.read() as tx:
            status, reason = tx.sql.execute(
                "SELECT status, reason FROM research_resumes WHERE request=?", (request,)
            ).fetchone()
        assert (status, reason) == ("blocked", POLICY_CHANGED)
    finally:
        documents.close()
        state.projects.close()
