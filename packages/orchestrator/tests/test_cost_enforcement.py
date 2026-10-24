# SPDX-License-Identifier: MIT
"""LiteLLM is the only cost enforcement by default (D102).

A project has no local cost limit unless an operator sets one; then the model-call
journal alone refuses a paid call, and the refusal reads as the gateway's own budget
refusal, so the run stops ``gateway_budget`` with its reason on record and the recovery
rules for a budget stop hold unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.usage import AnsweredUsage
from sandbox_science.orchestrator.continuity.models import IntentModel, ModelIntents
from sandbox_science.orchestrator.continuity.transient import classify
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import GatewayBudgetError, ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import ModelUsage
from sandbox_science.orchestrator.runstate import RunState

from .test_resource_requests import setup
from .test_tool_journal import RUN, toolbox
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD


def spend(projects, run: str, cost: float, ordinal: int = 1) -> None:
    Archive(projects.control).append(
        "p",
        "s",
        run,
        Entry("assistant", "assistant", f"answer {ordinal}"),
        model_usage=AnsweredUsage(
            ordinal=ordinal,
            observed_at=datetime.now(UTC),
            usage=ModelUsage(input_tokens=1000, output_tokens=10, cost=cost),
        ),
    )


def test_without_a_limit_no_local_code_refuses_a_model_call(projects):
    _, parent, child, _ = setup(projects)
    spend(projects, RUN, 10_000.0)
    runs = RunState(projects.control)
    assert runs.cost_limit("p") is None
    assert runs.cost_refusal("p", "s", RUN, datetime.now(UTC), share=0.01) is None
    intents = ModelIntents(projects.control, run_share=0.01)
    assert intents.begin(parent, kind="main", alias="fixture")
    assert intents.begin(child, kind="delegated", alias="fixture")
    assert intents.begin(child, kind="reviewer", alias="fixture")


def test_a_configured_limit_refuses_every_paid_kind_with_a_durable_reason(projects):
    _, parent, child, _ = setup(projects)
    runs = RunState(projects.control)
    runs.set_cost_limit("p", 5.0)
    intents = ModelIntents(projects.control)
    assert intents.begin(parent, kind="main", alias="fixture")
    spend(projects, RUN, 5.0)
    for owner, kind in ((parent, "main"), (child, "delegated"), (child, "fold")):
        with pytest.raises(ControlError, match="model_budget: the project's local daily cost"):
            intents.begin(owner, kind=kind, alias="fixture")
    runs.set_cost_limit("p", None)
    assert intents.begin(child, kind="delegated", alias="fixture")


def test_a_configured_run_share_refuses_only_that_run(projects):
    _, parent, child, _ = setup(projects)
    RunState(projects.control).set_cost_limit("p", 10.0)
    spend(projects, RUN, 6.0)
    shared = ModelIntents(projects.control, run_share=0.5)
    with pytest.raises(ControlError, match="used its share"):
        shared.begin(parent, kind="main", alias="fixture")
    assert shared.begin(child, kind="delegated", alias="fixture")
    assert ModelIntents(projects.control).begin(parent, kind="main", alias="fixture")


async def test_a_local_refusal_stops_the_run_as_the_gateway_budget_would(projects):
    _, parent, _, _ = setup(projects)
    RunState(projects.control).set_cost_limit("p", 1.0)
    spend(projects, RUN, 2.0)
    model = IntentModel(
        ScriptedModel([ModelTurn("never")]), ModelIntents(projects.control), parent, "fixture"
    )
    with pytest.raises(GatewayBudgetError) as raised:
        await model.complete([{"role": "user", "content": "Go."}], [])
    assert classify(raised.value) is None  # a budget stop, never retried automatically

    async def unused(request):
        raise AssertionError("no tool call")

    result = await run_task(model, toolbox(unused), task="Go.", run_id="root", max_turns=5)
    assert result.stopped == "gateway_budget"
    assert result.summaries["budget"]["refusal"].startswith("model_budget: the project's")
    assert result.turns == 1


def test_the_retired_grant_settings_load_and_do_not_stop(projects):
    """A saved ``max_token_grant`` still loads; it now caps soft token extensions."""
    from sandbox_science.orchestrator.delegation.records import DelegationRecords

    found = DelegationRecords(projects.control).configure("p", max_token_grant=60_000_000)
    assert found.max_token_grant == 60_000_000
    assert Transcript(context=AgentContext(messages=[])).halted is None
    with pytest.raises(ModuleNotFoundError):
        __import__("sandbox_science.orchestrator.continuity.grants")


def test_child_soft_token_cap_is_not_charged_against_a_child_on_resume(projects):
    """A child's work past its original tokens is never refused by local code."""
    _, _, child, _ = setup(projects, tokens=50_000)
    spend(projects, CHILD, 0.0)
    Archive(projects.control).append(
        "p",
        "s",
        CHILD,
        Entry("assistant", "assistant", "more"),
        model_usage=AnsweredUsage(
            ordinal=2,
            observed_at=datetime.now(UTC),
            usage=ModelUsage(input_tokens=60_000, output_tokens=10),
        ),
    )
    assert ModelIntents(projects.control).begin(child, kind="delegated", alias="fixture")
