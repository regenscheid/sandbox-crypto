# SPDX-License-Identifier: MIT
"""Public tool offers and lifecycle ordering before capability extraction."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast

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
from sandbox_science.orchestrator.jobs import JOB_SPECS, JobNotices
from sandbox_science.orchestrator.ledger.tool import SPECS as LEDGER_SPECS
from sandbox_science.orchestrator.memory_tools import SPECS as MEMORY_SPECS
from sandbox_science.orchestrator.model import ToolSpec
from sandbox_science.orchestrator.plan_tools import SPECS as PLAN_SPECS
from sandbox_science.orchestrator.purpose import offered as with_purpose
from sandbox_science.orchestrator.question_tool import SPECS as QUESTION_SPECS
from sandbox_science.orchestrator.resource_hooks import SPEC as RESOURCE_SPEC
from sandbox_science.orchestrator.skills.tool import SPEC as SKILL_SPEC
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .scripted import ToolCall

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="e", actor="agent")


def domain(specs: tuple[ToolSpec, ...] = (), **kwargs) -> Any:
    # Assembly needs only this controller surface; no domain operation runs here.
    return SimpleNamespace(specs=specs, **kwargs)


def offered(role: str) -> Toolbox:
    delegated = role != "main"
    kwargs = {
        "documents": domain(DOCUMENT_SPECS),
        "jobs": domain(JOB_SPECS, handlers={spec.name: None for spec in JOB_SPECS}),
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
        "main": "239a3395e5c4d8691134334f1243e9df4a3583fd0bfe1f4db07c5ca779e83407",
        "task": "58e23b4b31cf0b3bac542e58272b7b5803d26f7a24c2349fa82fd843ec2bf396",
        "check": "d53a38f1959543ffea98de7fe9148f0e34c3919d91bad3dd4ca96b727c7d7acd",
    }
    assert hashlib.sha256(encoded.encode()).hexdigest() == expected[role]
    # Each as the model is offered it, with the required purpose_note (Purpose 1).
    assert toolbox.specs == tuple(with_purpose(entry.spec) for entry in toolbox.entries.values())


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


def test_delegated_setup_rejects_user_capabilities() -> None:
    """A delegated run cannot ask the user (PD-41); its connectors are its own (DA-3,
    test_connectors.py)."""
    with pytest.raises(ValueError, match="cannot ask the user"):
        Toolbox(cast(WorkerClient, object()), CONTEXT, delegated=True, questions=domain())


def test_background_offer_and_hook_are_enabled_together() -> None:
    toolbox = Toolbox(cast(WorkerClient, object()), CONTEXT, jobs=domain())
    assert not any(spec.name.startswith("job_") for spec in toolbox.specs)
    assert toolbox.turn_hooks() == ()


def test_trusted_role_factories_select_tools_and_their_hooks_together() -> None:
    from sandbox_science.orchestrator import capabilities as caps

    main = offered("main")
    task = Toolbox(
        cast(WorkerClient, object()),
        CONTEXT,
        delegated=True,
        capabilities=caps.task(main.capabilities.bundles),
    )
    check = Toolbox(
        cast(WorkerClient, object()),
        CONTEXT,
        delegated=True,
        capabilities=caps.check(main.capabilities.bundles),
    )
    for toolbox in (task, check):
        assert toolbox.plans is toolbox.reviews is toolbox.questions is None
        assert {hook.name for hook in toolbox.turn_hooks()}.isdisjoint(
            {"plans", "reviews", "questions"}
        )
    for definition in (caps.DOCUMENTS, caps.LEDGER, caps.MEMORY):
        task_entries = next(
            b.entries for b in task.capabilities.bundles if b.name == definition.name
        )
        check_entries = next(
            b.entries for b in check.capabilities.bundles if b.name == definition.name
        )
        assert check_entries == tuple(e for e in task_entries if e.effect == "read")
        assert check_entries
        assert len(check_entries) < len(task_entries)


async def test_bundled_resources_close_after_partial_setup_in_reverse_order() -> None:
    from contextlib import AsyncExitStack

    from sandbox_science.orchestrator import capabilities as caps

    closed = []

    async def review_close():
        closed.append("reviews")

    async def artifact_close():
        closed.append("artifacts")

    def failed_setup(stack):
        caps.images(domain(close=lambda: closed.append("images")), stack.push_async_callback)
        caps.reviews(domain(AUTHOR_SPECS, close=review_close), stack.push_async_callback)
        caps.artifacts(domain(aclose=artifact_close), stack.push_async_callback)
        raise RuntimeError("later setup failed")

    async with AsyncExitStack() as stack:
        with pytest.raises(RuntimeError, match="later setup failed"):
            failed_setup(stack)
        assert closed == []
    assert closed == ["artifacts", "reviews", "images"]


async def test_review_cleanup_is_owned_before_entry_binding_can_fail() -> None:
    from contextlib import AsyncExitStack

    from sandbox_science.orchestrator import capabilities as caps

    closed = []

    class BrokenReview:
        @property
        def specs(self):
            raise RuntimeError("entry setup failed")

        async def close(self):
            closed.append("reviews")

    async with AsyncExitStack() as stack:
        with pytest.raises(RuntimeError, match="entry setup failed"):
            caps.reviews(cast(Any, BrokenReview()), stack.push_async_callback)
    assert closed == ["reviews"]


@pytest.mark.parametrize(
    ("attribute", "name", "source"),
    [
        ("documents", "requests", "tool"),
        ("jobs", "jobs", "jobs"),
        ("delegations", "delegations", None),
        ("mailbox", "agent_messages", None),
        ("resources", "resources", None),
        ("questions", "questions", None),
        ("plans", "plans", "tools"),
        ("reviews", "reviews", "controller"),
        ("math_checks", "math", "controller"),
        ("recorder", "files", "recorder"),
    ],
)
def test_public_controller_changes_affect_fresh_hooks(attribute, name, source) -> None:
    toolbox = Toolbox(cast(WorkerClient, object()), CONTEXT, output_chars=8000)
    specs = toolbox.specs
    assert toolbox.turn_hooks() == ()
    for replacement in (domain(name=name), domain(name=name)):
        setattr(toolbox, attribute, replacement)
        first, second = toolbox.turn_hooks(), toolbox.turn_hooks()
        assert [hook.name for hook in first] == [name]
        if source is None:
            assert first[0] is second[0] is replacement
        else:
            assert getattr(first[0], source) is replacement
            assert first[0] is not second[0]
        if name == "jobs":
            assert isinstance(first[0], JobNotices)
            assert first[0].max_chars == 2000
    setattr(toolbox, attribute, None)
    assert toolbox.turn_hooks() == ()
    assert toolbox.specs == specs


@pytest.mark.parametrize("role", ["task", "check"])
def test_absent_hook_slots_cannot_restore_main_only_child_capabilities(role: str) -> None:
    from sandbox_science.orchestrator import capabilities as caps

    selected = getattr(caps, role)(())
    toolbox = Toolbox(cast(WorkerClient, object()), CONTEXT, delegated=True, capabilities=selected)
    toolbox.reviews = domain()
    toolbox.plans = domain()
    toolbox.questions = domain(name="questions")
    assert toolbox.turn_hooks() == ()


@pytest.mark.parametrize("initial", [False, True])
def test_job_notice_bound_uses_current_output_chars(initial: bool) -> None:
    toolbox = Toolbox(
        cast(WorkerClient, object()),
        CONTEXT,
        output_chars=8000,
        features=("jobs",),
        jobs=domain() if initial else None,
    )
    if not initial:
        toolbox.jobs = domain()
    (first,) = toolbox.turn_hooks()
    assert isinstance(first, JobNotices)
    assert first.max_chars == 2000
    toolbox.output_chars = 1200
    (second,) = toolbox.turn_hooks()
    assert isinstance(second, JobNotices)
    assert second.max_chars == 300
    assert first is not second
