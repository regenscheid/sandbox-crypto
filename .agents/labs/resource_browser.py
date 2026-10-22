# SPDX-License-Identifier: MIT
"""Synthetic resource-approval fixture, called by the disposable browser lab."""

from datetime import UTC, datetime

from sandbox_science.contracts import ExecutionContext, new_id
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.resources import ResourceRequests
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.usage import AnsweredUsage
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.orchestrator.runstate import RunState


def seed_resources(projects: ProjectStore, project: str) -> str:
    """Create an idle, synthetic branch; no provider, worker or research dispatch."""
    session = projects.open_session(project, "allowance", dedicated=False)
    root, child = "20261018T150000000000Z-budget", "20261018T150001000000Z-check"
    works = WorkRecords(projects.control)
    works.admit(project, session.session_id, root)
    owner = works.admit(project, session.session_id, child, parent=root)
    Blueprints(projects.control).save(
        owner,
        Blueprint(
            context=ExecutionContext(
                project_id=project,
                session_id=session.session_id,
                environment_id=session.environment_id,
            ),
            request={
                "project": project,
                "session": session.session_id,
                "tokens": 20_000_000,
                "max_turns": 300,
                "task": "Synthetic check: verify a finite-field calculation",
            },
        ),
    )
    delegations = DelegationRecords(projects.control)
    delegations.start(
        project,
        session.session_id,
        Delegation(
            delegation_id=new_id("dlg"),
            parent=root,
            run=child,
            kind="check",
            brief={"task": "Synthetic check: verify a finite-field calculation"},
            started_at=delegations.clock.now(),
        ),
    )
    Archive(projects.control).append(
        project,
        session.session_id,
        child,
        Entry(
            "assistant",
            "assistant",
            "Synthetic checkpoint: the reference calculation is saved. No model was called.",
        ),
        parent=root,
        model_usage=AnsweredUsage(
            ordinal=1,
            observed_at=datetime.now(UTC),
            usage=ModelUsage(model="fixture", input_tokens=19_999_900, output_tokens=100, cost=0),
        ),
    )
    ResourceRequests(projects.control).observe(
        owner,
        "tokens",
        used=20_000_000,
        total=20_000_000,
        revision=0,
    )
    works.interrupt(project, session.session_id, root)
    works.block(project, session.session_id, child, "run_budget")
    RunState(projects.control).set_cost_limit(project, 100)
    return session.session_id
