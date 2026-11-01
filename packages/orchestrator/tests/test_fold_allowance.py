# SPDX-License-Identifier: MIT
"""Child summaries count toward their own work's usage, not another work's (D102: a
child's tokens are a soft cap; no allowance refuses its folds)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from sandbox_science.orchestrator.context.history.collections import Collection
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.plans import spent_tokens
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.harness import Harness, Role, RunRequest, Settings
from sandbox_science.orchestrator.model import GatewayModel, ModelGateway
from sandbox_science.orchestrator.model_profiles import ModelProfile, ModelRole, profiles
from sandbox_science.orchestrator.projects import ModelUsage
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, CONTINUATION, record


def test_fold_tokens_survive_resume_and_exclude_parent_and_sibling(projects):
    works = WorkRecords(projects.control)
    root = works.admit("p", "s", RUN)
    child = works.admit("p", "s", CHILD, parent=RUN)
    intents = ModelIntents(projects.control)
    for owner, n in ((root, 9000), (child, 500)):
        identity = intents.begin(owner, kind="fold", alias="fixture")
        intents.observe(owner, identity, ModelUsage(input_tokens=n, output_tokens=20))
        # All three copies must not charge a summary three times.
        usage = ModelUsage(input_tokens=n, output_tokens=20, intent_id=identity).model_dump(
            mode="json"
        )
        Collection(projects.control, ("p", "s", owner.run), "fold_calls").append(usage)
        RunState(projects.control).append("p", "s", owner.run, "fold", {"calls": [usage]})
    works.finish(record(CHILD, stopped="turn_limit"))
    works.finish(record(RUN))
    resume = works.request("p", "s")
    assert resume is not None
    RunState(projects.control).acquire("p", "s", CONTINUATION, "fixture")
    _, epoch = works.claim("p", "s", resume, lease_run=CONTINUATION)
    works.admit("p", "s", CONTINUATION, work=CHILD, predecessor=CHILD, epoch=epoch)
    Collection(projects.control, ("p", "s", CONTINUATION), "fold_calls").append(
        {"estimated": True, "cancelled": True, "input_tokens": 100, "cost": None}
    )
    projects.control.close()
    with projects.control.read() as tx:
        assert spent_tokens(tx.sql, "p", "s", CHILD) == 620
        assert spent_tokens(tx.sql, "p", "s", CHILD, run=CONTINUATION) == 100
        assert spent_tokens(tx.sql, "p", "s", RUN) == 9020


@pytest.mark.parametrize("explicit", [False, True])
async def test_child_summary_profiles_use_selected_model_and_preserve_resume_pins(explicit):
    gateway = ModelGateway(
        "http://unused.invalid",
        "test-only-no-network",
        apis={"child": "responses", "alternate": "responses"},
        reasoning_effort={"fold": "high", "overview": "low", "delegated": "max"},
    )
    client = WorkerClient("http://unused.invalid", "test-only")
    try:
        harness = Harness(
            client,
            Role(gateway.model("main"), "main"),
            delegated=Role(gateway.model("child", role="delegated"), "child"),
            gateway=gateway,
            settings=Settings(),
        )
        parent_pins = harness.model_profiles(RunRequest("p", "s", "Root"))
        request = RunRequest(
            "p", "s", "Child", parent=RUN, delegation="delegate-fixture", alias="alternate"
        )
        with profiles(parent_pins):
            pins: dict[ModelRole, ModelProfile] = harness.model_profiles(request)
        assert pins["fold"] == ModelProfile(
            alias="alternate", api="responses", reasoning_effort="high"
        )
        assert pins["overview"].reasoning_effort == "low"
        if explicit:
            pins = {
                **pins,
                "fold": ModelProfile(alias="main", reasoning_effort="high"),
                "overview": ModelProfile(alias="main", reasoning_effort="low"),
            }
        resumed = replace(request, model_profiles=pins)
        assert harness.model_profiles(resumed) == pins
        with profiles(pins):
            narrator = harness._child_narrator(resumed, CONTEXT)
            assert isinstance(narrator, GatewayModel)
            assert narrator.requested_profile == pins["fold"]
            assert narrator.for_role("overview").requested_profile == pins["overview"]
    finally:
        await gateway.close()
        await client.close()
