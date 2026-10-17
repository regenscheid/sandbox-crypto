# SPDX-License-Identifier: MIT
"""USER recovery token grants are scoped, durable and independent of project defaults."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest

from sandbox_science.orchestrator.continuity.grants import TokenGrantRequest, TokenGrants
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.recovery import Recovery
from sandbox_science.orchestrator.continuity.resolutions import RecoveryControls, Resolution
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import DelegationRecords
from sandbox_science.orchestrator.harness import Harness, Role, Settings
from sandbox_science.orchestrator.history import Archive
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_run_harness import Sink, main
from .test_tree_recovery import RecoveryWorker, drive, prepare_tree
from .test_work_ownership import CHILD, GRANDCHILD, RUN, record


def request(*, total: int = 500_000, revision: int = 0) -> TokenGrantRequest:
    return TokenGrantRequest(
        work=CHILD,
        current=CHILD,
        epoch=1,
        expected_revision=revision,
        total_tokens=total,
        reason="The check made useful progress; continue the same work.",
    )


def test_user_grant_is_explicit_idempotent_and_survives_settings_changes(tmp_path: Path):
    state, documents, works = prepare_tree(tmp_path)
    try:
        grants = TokenGrants(state.projects.control)
        records = DelegationRecords(state.projects.control)
        records.configure("p", task_tokens=1_000_000)
        RunState(state.projects.control).set_cost_limit("p", 100)
        works.finish(record(CHILD, stopped="run_budget"))
        works.block("p", "s", CHILD, "model_budget: project budget is exhausted")
        assert grants.total("p", "s", CHILD, 100_000) == 100_000
        first = grants.renew("p", "s", request(), operator="user")
        assert grants.renew("p", "s", request(), operator="user") == first
        assert first.revision == 1
        assert grants.total("p", "s", CHILD, 100_000) == 500_000
        assert Archive(state.projects.control).message("p", first.message_id).entry.source == (
            "operator_token_grant"
        )
        with pytest.raises(ControlError, match="another USER decision"):
            grants.renew("p", "s", request(total=600_000), operator="user")
        second = grants.renew("p", "s", request(total=750_000, revision=1), operator="user")
        assert second.previous_total == 500_000
        assert grants.total("p", "s", CHILD, 100_000) == 750_000
        records.configure("p", task_tokens=100_000)
        assert grants.total("p", "s", CHILD, 100_000) == 750_000
        assert works.request("p", "s") is not None
        assert grants.renew("p", "s", request(total=750_000, revision=1), operator="user") == (
            second
        )
    finally:
        documents.close()
        state.projects.close()


def test_user_grant_refuses_unbounded_stale_nested_or_active_work(tmp_path: Path):
    state, documents, works = prepare_tree(tmp_path)
    try:
        grants = TokenGrants(state.projects.control)
        works.block("p", "s", CHILD, "run_budget")
        with pytest.raises(ControlError, match="spending cap"):
            grants.renew("p", "s", request(), operator="user")
        RunState(state.projects.control).set_cost_limit("p", 100)
        with pytest.raises(ControlError, match="configured child ceiling"):
            grants.renew("p", "s", request(total=6_000_000), operator="user")
        with pytest.raises(ControlError, match="attempt or epoch changed"):
            grants.renew("p", "s", request().model_copy(update={"epoch": 2}), operator="user")
        with pytest.raises(ControlError, match="direct root children"):
            grants.renew(
                "p",
                "s",
                request().model_copy(update={"work": GRANDCHILD, "current": GRANDCHILD}),
                operator="user",
            )
        queued = works.request("p", "s")
        assert queued is not None
        with pytest.raises(ControlError, match="Resume is pending"):
            grants.renew("p", "s", request(), operator="user")
    finally:
        documents.close()
        state.projects.close()


async def test_resume_uses_extended_cumulative_ceiling_for_same_child(tmp_path: Path):
    state, documents, works = prepare_tree(tmp_path)
    try:
        DelegationRecords(state.projects.control).configure("p", task_tokens=500_000)
        RunState(state.projects.control).set_cost_limit("p", 100)
        works.block("p", "s", CHILD, "run_budget")
        TokenGrants(state.projects.control).renew("p", "s", request(), operator="user")
        harness = Harness(
            cast(WorkerClient, RecoveryWorker()),
            main("Recovered root"),
            delegated=Role(ScriptedModel([]), "scripted"),
            stores=state,
            settings=Settings(min_isolation_tier=None),
        )
        resume = works.request("p", "s")
        assert resume is not None
        recovery = Recovery(harness, "p", "s", resume)
        RunState(state.projects.control).acquire("p", "s", recovery.run_name, "fixture")
        root, epoch = works.claim("p", "s", resume, lease_run=recovery.run_name)
        assert root == RUN
        recovery.root, recovery.epoch = root, epoch
        with recovery.plans.authority(epoch, recovery.run_name):
            recovery.plans.prepare(root, run=recovery.run_name, started=recovery.started_at)
            admitted = await recovery.prepare(works.get("p", "s", CHILD))
        assert admitted.request.work == CHILD
        assert admitted.request.tokens == 500_000
        assert TokenGrants(state.projects.control).total("p", "s", CHILD, 100_000) == 500_000
    finally:
        documents.close()
        state.projects.close()


async def test_exhausted_renewal_remains_renewable_after_failed_resume(tmp_path: Path):
    state, documents, works = prepare_tree(tmp_path)
    try:
        DelegationRecords(state.projects.control).configure("p", task_tokens=1_000_000)
        RunState(state.projects.control).set_cost_limit("p", 100)
        works.block("p", "s", CHILD, "run_budget")
        grants = TokenGrants(state.projects.control)
        grants.renew("p", "s", request(), operator="user")
        owner = works.owner("p", "s", CHILD)
        assert owner is not None
        # A delayed unknown paid call is discovered after the first extension.
        with state.projects.control.write() as tx:
            tx.sql.execute("UPDATE research_work SET state='running' WHERE work=?", (CHILD,))
        identity = ModelIntents(state.projects.control).begin(
            owner, kind="delegated", alias="scripted"
        )
        works.interrupt("p", "s", RUN)
        works.block("p", "s", CHILD, "run_budget")
        RecoveryControls(state.projects.control).resolve(
            "p",
            "s",
            Resolution(
                work=CHILD,
                current=CHILD,
                epoch=1,
                run=CHILD,
                kind="model_reservation",
                identity=identity,
                reason="Original paid call remains unknown.",
                cost=4,
                input_tokens=500_000,
                output_tokens=1,
            ),
            operator="user",
        )
        harness = Harness(
            cast(WorkerClient, RecoveryWorker()),
            main("Root waits"),
            delegated=Role(
                ScriptedModel(
                    [ModelTurn(json.dumps({"status": "complete", "summary": "Finished"}))] * 8
                ),
                "scripted",
            ),
            stores=state,
            settings=Settings(min_isolation_tier=None),
        )
        resume = works.request("p", "s")
        assert resume is not None
        assert await drive(Recovery(harness, "p", "s", resume), Sink(), asyncio.Event())
        assert works.get("p", "s", CHILD).reason == "run_budget"
        epoch = works.get("p", "s", CHILD).epoch
        next_grant = request(total=750_000, revision=1).model_copy(update={"epoch": epoch})
        assert grants.renew("p", "s", next_grant, operator="user").revision == 2
    finally:
        documents.close()
        state.projects.close()
