# SPDX-License-Identifier: MIT
"""Public tool offers and lifecycle ordering before capability extraction."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.agent_messages import (
    CHILD_SPEC,
    PARENT_SPEC,
    STATUS_SPEC,
    WAIT_SPEC,
)
from sandbox_science.orchestrator.consistency.controller import AUTHOR_SPECS
from sandbox_science.orchestrator.context.history import SPECS as HISTORY_SPECS
from sandbox_science.orchestrator.delegation.specs import SPECS as DELEGATION_SPECS
from sandbox_science.orchestrator.delegation.tracks import SPECS as TRACK_SPECS
from sandbox_science.orchestrator.documents.tool import SPECS as DOCUMENT_SPECS
from sandbox_science.orchestrator.jobs import JOB_SPECS
from sandbox_science.orchestrator.ledger.tool import SPECS as LEDGER_SPECS
from sandbox_science.orchestrator.memory_tools import SPECS as MEMORY_SPECS
from sandbox_science.orchestrator.model import ToolCall, ToolSpec
from sandbox_science.orchestrator.plan_tools import SPECS as PLAN_SPECS
from sandbox_science.orchestrator.question_tool import SPECS as QUESTION_SPECS
from sandbox_science.orchestrator.resource_hooks import SPEC as RESOURCE_SPEC
from sandbox_science.orchestrator.skills.tool import SPEC as SKILL_SPEC
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="e", actor="agent")


def domain(specs: tuple[ToolSpec, ...] = (), **kwargs):
    return SimpleNamespace(specs=specs, **kwargs)


def offered(role: str) -> Toolbox:
    delegated = role != "main"
    kwargs = {
        "documents": domain(DOCUMENT_SPECS),
        "jobs": domain(handlers={spec.name: None for spec in JOB_SPECS}),
        "ledger": domain(LEDGER_SPECS),
        "memory": domain(MEMORY_SPECS),
        "tracks": domain(TRACK_SPECS),
        "delegations": domain(DELEGATION_SPECS, name="delegations", live=lambda: ()),
        "mailbox": domain((CHILD_SPEC, PARENT_SPEC, STATUS_SPEC, WAIT_SPEC), name="agent_messages"),
        "resources": domain((RESOURCE_SPEC,), name="resources"),
        "skills": domain((SKILL_SPEC,)),
        "history": domain(HISTORY_SPECS),
        "images": domain(),
        "math_checks": domain(),
        "recorder": domain(),
        "artifacts": domain(),
    }
    if not delegated:
        kwargs.update(
            reviews=domain(AUTHOR_SPECS),
            plans=domain(PLAN_SPECS),
            questions=domain(QUESTION_SPECS, name="questions"),
        )
    return Toolbox(
        cast(WorkerClient, object()),
        CONTEXT,
        languages=("python", "sage"),
        features=("jobs", "pdf", "admission", "provisioning"),
        check_kernels=True,
        delegated=delegated,
        check=role == "check",
        **kwargs,
    )


@pytest.mark.parametrize("role", ["main", "task", "check"])
def test_offered_schemas_effects_and_entry_flags(role: str) -> None:
    toolbox = offered(role)
    encoded = json.dumps(
        [
            [
                entry.spec.name,
                entry.spec.description,
                entry.spec.parameters,
                entry.effect,
                entry.raw,
                entry.bound,
                entry.whole,
            ]
            for entry in toolbox.entries.values()
        ],
        sort_keys=True,
    )
    expected = {
        "main": "ede46be24460f7b63dba855141999c5f930f82b58c731eb1c20a19f2f635fb48",
        "task": "12347860863e9290f6af51c6a7c1b040cfc75de51bb86b3596ed3e06603e703c",
        "check": "b2454ba7722d12efd330860f255248c0dfcfd04d0ae8c70ce17a8139e1a222ea",
    }
    assert hashlib.sha256(encoded.encode()).hexdigest() == expected[role]
    assert toolbox.specs == tuple(entry.spec for entry in toolbox.entries.values())


@pytest.mark.parametrize("role", ["main", "task", "check"])
def test_hook_order_and_fresh_adapter_state(role: str) -> None:
    toolbox = offered(role)
    first, second = toolbox.turn_hooks(), toolbox.turn_hooks()
    names = ["requests", "jobs", "delegations", "agent_messages", "resources"]
    if role == "main":
        names += ["questions", "plans", "reviews"]
    names += ["math", "kernels", "files"]
    assert [hook.name for hook in first] == names
    for left, right in zip(first, second, strict=True):
        if left.name in {"delegations", "agent_messages", "resources", "questions"}:
            assert left is right
        else:
            assert left is not right


@pytest.mark.parametrize("name", ["ask_user", "review_start", "plan_create", "mcp_tools"])
async def test_delegated_unoffered_harness_tools_remain_role_denials(name: str) -> None:
    toolbox = Toolbox(cast(WorkerClient, object()), CONTEXT, delegated=True)
    outcome = await toolbox.call(ToolCall("c", name, "{}"), "run")
    assert json.loads(outcome.text)["error"]["kind"] == "policy_denied"
    assert "not_permitted_for_role" in outcome.text


def test_delegated_setup_rejects_user_and_connector_capabilities() -> None:
    with pytest.raises(ValueError, match="cannot ask the user"):
        Toolbox(cast(WorkerClient, object()), CONTEXT, delegated=True, questions=domain())
    with pytest.raises(ValueError, match="no connector tools"):
        Toolbox(cast(WorkerClient, object()), CONTEXT, delegated=True, connectors=domain())


def test_background_offer_and_hook_are_enabled_together() -> None:
    toolbox = Toolbox(cast(WorkerClient, object()), CONTEXT, jobs=domain())
    assert not any(spec.name.startswith("job_") for spec in toolbox.specs)
    assert toolbox.turn_hooks() == ()
